"""
Worker eseguito in un QThread separato: la scansione (specie via
INSTREAM su directory grandi) può durare a lungo e non deve mai girare
nel thread della UI, altrimenti la finestra si blocca.

Comunica con la UI solo tramite segnali Qt, mai chiamando direttamente
widget da un altro thread.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Signal

from ..clamd_client import ClamdClient, ClamdError
from ..quarantine import Quarantine

# Intervallo minimo (secondi) tra due aggiornamenti di stato inviati alla
# UI. Su scansioni veloci (centinaia/migliaia di file al secondo) emettere
# un segnale Qt per OGNI file sommerge la coda eventi del thread GUI:
# ogni update di una QLabel il cui testo cambia (percorso file, contatori)
# forza Qt a ricalcolare il layout, e con decine di migliaia di
# ricalcoli al secondo il risultato visibile è uno sfarfallio/
# ridimensionamento continuo della finestra. Il conteggio interno resta
# comunque preciso al 100% indipendentemente dal throttling: cambia solo
# la frequenza con cui viene *mostrato*.
PROGRESS_THROTTLE_SECONDS = 0.15

# Se una pausa dura più di questi secondi, alla ripresa la sessione
# IDSESSION di clamd viene ricreata proattivamente (reset_session in
# clamd_client.py): clamd chiude da solo le sessioni lasciate inattive
# oltre IdleTimeout (default 30s in clamd.conf), quindi il primo file
# scansionato su una sessione chiusa durante una pausa lunga produrrebbe
# un errore finto — recuperato dalla ricostruzione automatica della
# sessione, ma intanto contato come errore e mostrato in lista per
# un file che in realtà sta bene. 25s lascia un margine sotto il
# default di clamd.
CLAMD_IDLE_SAFETY_SECONDS = 25.0


class ScanWorker(QThread):
    result_ready = Signal(object)  # ScanResult, solo per infetti/errori/troppo-grandi
    scanning = Signal(str)  # path del file che si sta iniziando a scansionare (throttled)
    progress = Signal(int, int, int, int)  # (scansionati, infezioni, errori, troppo_grandi), throttled
    quarantined = Signal(str)  # path originale del file appena messo in quarantena
    error = Signal(str)
    finished_scan = Signal(int, int, int, int)  # (scansionati, infezioni, errori, troppo_grandi)
    # Segnali di pausa: emessi dal worker quando entra/esce EFFETTIVAMENTE
    # dalla pausa. Un pause() richiesto mentre un file grosso è ancora in
    # streaming ha effetto solo al confine tra due file: lo stato "In
    # pausa" nella UI deve seguire questi segnali, non il click sul
    # pulsante (che è una richiesta, non un dato acquisito).
    paused = Signal()
    resumed = Signal()

    def __init__(
        self,
        socket_path: str,
        target: Path,
        quarantine_dir: Optional[Path] = None,
        auto_quarantine: bool = False,
        client_factory: Optional[Callable[..., Any]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.socket_path = socket_path
        self.target = target
        self.quarantine_dir = quarantine_dir
        self.auto_quarantine = auto_quarantine
        # Factory iniettabile per i test (finto client senza clamd reale):
        # None = produzione, ClamdClient vero. I test della pausa hanno
        # bisogno di un client che produca risultati a ritmo controllato,
        # altrimenti sarebbero dipendenti da un demone esterno.
        self._client_factory = client_factory
        self._stop_requested = False
        # Stato di pausa. Letti/scritti da thread diversi (UI e worker):
        # l'assegnazione di un bool è atomica sotto GIL e threading.Event
        # è thread-safe, non serve altro.
        self._pause_requested = False
        self._wake = threading.Event()

    def stop(self) -> None:
        self._stop_requested = True
        # Se il worker è fermo in pausa, sveglialo subito: il loop di
        # attesa ricontrolla _stop_requested ed esce senza riprendere.
        self._wake.set()

    def pause(self) -> None:
        """Richiede la pausa. Ha effetto al confine tra due file: il file
        in corso di streaming viene completato, poi il worker si ferma
        (granularità voluta: niente metà-file in stato ambiguo)."""
        self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False
        self._wake.set()

    @property
    def is_pause_requested(self) -> bool:
        return self._pause_requested

    def _wait_while_paused(self) -> float:
        """Blocca il worker finché è richiesta la pausa.

        Ritorna la durata della pausa in secondi (0.0 se non era in
        pausa): serve al chiamante per decidere se ricreare la sessione
        clamd alla ripresa (vedi CLAMD_IDLE_SAFETY_SECONDS).
        """
        if not self._pause_requested:
            return 0.0
        started = time.monotonic()
        self.paused.emit()
        while self._pause_requested and not self._stop_requested:
            # Timeout corto: il worker ricontrolla le condizioni ogni
            # mezzo secondo invece di dormire indefinitamente su un
            # Event che nessuno setta (difesa contro ogni futuro bug
            # che "perda" una resume()).
            self._wake.wait(timeout=0.5)
            self._wake.clear()
        duration = time.monotonic() - started
        if not self._stop_requested:
            self.resumed.emit()
        return duration

    def run(self) -> None:
        factory = self._client_factory or ClamdClient
        client = factory(unix_socket=self.socket_path)
        quarantine = Quarantine(self.quarantine_dir) if self.quarantine_dir else None

        # I file dentro la directory di quarantena sono i dati stessi
        # dell'applicazione (infetti già gestiti + index.json): se il
        # percorso scansionato li copre (es. scansione di tutta la home),
        # verrebbero ri-rilevati a ogni esecuzione, gonfiando per sempre
        # infetti/errori con "fantasmi" già gestiti. Questo filtro scarta
        # i risultati provenienti dalla quarantena PRIMA di ogni conteggio;
        # l'esclusione a monte (per non leggerli proprio) appartiene alla
        # traversata in clamd_client.scan_stream.
        quarantine_root = self.quarantine_dir.resolve() if self.quarantine_dir else None

        def inside_quarantine(p: Path) -> bool:
            if quarantine_root is None:
                return False
            try:
                return p.resolve().is_relative_to(quarantine_root)
            except OSError:
                # Es. file sparito nel frattempo: non è un motivo per
                # interrompere la scansione, semplicemente non filtrare.
                return False

        scanned = 0
        infections = 0
        errors = 0
        too_large = 0

        # Gate temporale condiviso tra l'aggiornamento "sto scansionando
        # X" e l'aggiornamento dei contatori: un'unica cadenza per
        # entrambi, così la UI riceve un "tick" di stato coerente invece
        # di due flussi di segnali indipendenti che si accavallano.
        last_emit = 0.0

        def on_file_start(p: Path) -> None:
            nonlocal last_emit
            if inside_quarantine(p):
                return
            now = time.monotonic()
            if now - last_emit >= PROGRESS_THROTTLE_SECONDS:
                self.scanning.emit(str(p))
                last_emit = now

        try:
            # Pattern while/next invece del for: la pausa va verificata
            # TRA due next(), cioè PRIMA che il generatore scansioni il
            # file successivo. Con un for il check cadrebbe dopo aver
            # ricevuto il risultato, troppo tardi: il file successivo
            # sarebbe comunque stato scansionato durante la pausa.
            iterator = client.scan_stream(self.target, on_file_start=on_file_start)
            while True:
                paused_for = self._wait_while_paused()
                if paused_for > CLAMD_IDLE_SAFETY_SECONDS:
                    # Sessione quasi certamente chiusa da clamd durante
                    # la pausa (IdleTimeout): ricreala PRIMA del prossimo
                    # file, così alla ripresa non arriva un errore finto
                    # sul primo file. getattr difensivo: finché
                    # clamd_client non espone reset_session() questo non
                    # fa nulla (la ricostruzione automatica su errore
                    # copre comunque il caso, pagandola in un falso
                    # errore in lista).
                    reset_session = getattr(client, "reset_session", None)
                    if callable(reset_session):
                        reset_session()
                if self._stop_requested:
                    break
                try:
                    result = next(iterator)
                except StopIteration:
                    break

                # File dentro la quarantena: scartati prima di ogni
                # conteggio. Non sono dati dell'utente, sono i nostri:
                # non devono comparire né nei risultati né nei totali,
                # e la matematica scanned = clean + infected + errors
                # + too_large resta coerente.
                if inside_quarantine(Path(result.path)):
                    continue

                scanned += 1

                if result.too_large:
                    # Non è un malfunzionamento: il file supera
                    # StreamMaxLength (clamd.conf) e semplicemente non è
                    # stato verificato — va tenuto distinto dagli errori
                    # veri, altrimenti l'utente pensa che qualcosa si sia
                    # rotto quando in realtà è solo un file grande.
                    too_large += 1
                elif result.status == "ERROR":
                    errors += 1

                if result.infected and self.auto_quarantine and quarantine is not None:
                    try:
                        # Sposta il file in quarantena
                        quarantine.quarantine_file(Path(result.path), result.signature)
                        # NON SOVRASCRIVIAMO result.path!
                        # L'utente nella GUI deve vedere dov'era il file originale,
                        # non il percorso nascosto della quarantena.
                        self.quarantined.emit(result.path)
                    except Exception as exc:  # noqa: BLE001
                        self.error.emit(f"Quarantena fallita per {result.path}: {exc}")

                if result.infected:
                    infections += 1

                if result.infected or result.status == "ERROR" or result.too_large:
                    # I file "puliti" (la stragrande maggioranza in una
                    # scansione tipica) non producono nessuna riga in
                    # lista: emettere un segnale per ognuno di essi è
                    # overhead puro, oltre che una delle cause dello
                    # sfarfallio della finestra durante scansioni grandi.
                    self.result_ready.emit(result)

                now = time.monotonic()
                if now - last_emit >= PROGRESS_THROTTLE_SECONDS:
                    self.progress.emit(scanned, infections, errors, too_large)
                    last_emit = now

        except (ClamdError, OSError) as exc:
            self.error.emit(f"Errore di comunicazione con clamd: {exc}")

        # Emissione finale non soggetta a throttling: garantisce che i
        # contatori mostrati combacino sempre col totale reale, anche se
        # l'ultimo tick periodico risale a prima dell'ultimo file scansionato.
        self.progress.emit(scanned, infections, errors, too_large)
        self.finished_scan.emit(scanned, infections, errors, too_large)
