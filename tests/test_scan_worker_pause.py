"""
Pausa/Ripresa/Stop di ScanWorker in modo DETERMINISTICO: il client
finto si ferma su una barriera prima di ogni file, così pause() e
stop() possono essere richiesti in punti esatti del flusso invece di
correre contro il tempo (niente sleep-allineati alla fortuna).

Due note di protocollo, entrambe apprese dai primi fallimenti:

1. Il fake yielda risultati FOUND (infetti) perché il worker, per
   design (throttling anti-sfarfallio), NON emette result_ready per i
   file puliti — asserire su result_ready con risultati OK produrrebbe
   attese che non scadono mai. auto_quarantine=False nei test, quindi
   nessuno spostamento: ogni file infetto produce esattamente una riga
   e finished_scan riporta (n, n, 0, 0).

2. Tutte le connessioni ai collezionisti usano Qt.DirectConnection
   ESPPLICITO: una connect() a una funzione Python pura (non a uno
   slot di QObject) è AutoConnection, e un'emit dal worker thread
   viene ACCODATA al thread del receiver (il main). Il test non gira
   mai l'event loop (è bloccato in wait_until/time.sleep), quindi gli
   eventi queued non verrebbero mai consegnati e ogni asserzione su
   liste collegate in AutoConnection fallirebbe per sempre.
   DirectConnection esegue la callback nel thread emittente: append
   immediato, deterministico, indipendente dall'event loop.

Choreografia delle barriere: la release va fatta DOPO aver osservato
che il worker è fermo sulla barriera N (waiting_at_barrier == N) —
settarla in anticipo è una race: viene consumata dalla barriera
precedente e il worker resta bloccato su quella successiva.
"""

import os
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from klamav_py.clamd_client import ScanResult
from klamav_py.gui import scan_worker as sw_module
from klamav_py.gui.scan_worker import ScanWorker


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_until(condition, timeout=5.0, interval=0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


class BarrierClient:
    """
    Client finto: scansiona una lista fissa di file fermandosi su una
    barriera PRIMA di yieldare ogni risultato. waiting_at_barrier dice
    al test dove il generatore è fermo. Implementa reset_session() e la
    firma di scan_stream usata dal worker (kwargs nuove del
    pre-check/esclusioni accettate e ignorate).
    """

    def __init__(self, files, unix_socket=None):
        self.files = [Path(f) for f in files]
        self._release = threading.Event()
        self.waiting_at_barrier = 0
        self.reset_session_calls = 0

    def reset_session(self):
        self.reset_session_calls += 1

    def scan_stream(self, target, on_file_start=None, exclude_dirs=None, max_stream_size=None):
        for i, f in enumerate(self.files, start=1):
            self.waiting_at_barrier = i
            self._release.wait(timeout=5)
            self._release.clear()
            yield ScanResult(
                path=str(f), status="FOUND", signature="Eicar-Test-Signature"
            )


def make_worker(files):
    created = []

    def factory(unix_socket=None):
        client = BarrierClient(files, unix_socket=unix_socket)
        created.append(client)
        return client

    worker = ScanWorker(
        socket_path="/finto/clamd.ctl",
        target=Path("/finto/target"),
        quarantine_dir=None,
        auto_quarantine=False,
        client_factory=factory,
    )
    return worker, created


def collect(worker, with_pause_flags=True):
    """Collega i collezionisti con DirectConnection (vedi nota 2 in
    testa al file) e ritorna le liste di raccolta."""
    results, paused_flag, finished = [], [], []
    worker.result_ready.connect(results.append, Qt.DirectConnection)
    worker.finished_scan.connect(lambda *a: finished.append(a), Qt.DirectConnection)
    if with_pause_flags:
        worker.paused.connect(lambda: paused_flag.append(True), Qt.DirectConnection)
    return results, paused_flag, finished


def test_pause_stops_at_file_boundary(qapp, tmp_path):
    files = [tmp_path / f"f{i}.txt" for i in range(4)]
    worker, created = make_worker(files)
    results, paused_flag, finished = collect(worker)

    worker.start()
    assert wait_until(lambda: len(created) == 1), "il client finto non è stato creato"
    client = created[0]

    # File 1: aspetta che il worker sia fermo sulla barriera 1, rilascia,
    # attendi il risultato.
    assert wait_until(lambda: client.waiting_at_barrier == 1)
    client._release.set()
    assert wait_until(lambda: len(results) == 1)

    # File 2: richiesta di pausa MENTRE il worker è già in attesa della
    # barriera 2 — il file in volo viene comunque completato, poi il
    # worker si ferma.
    assert wait_until(lambda: client.waiting_at_barrier == 2)
    worker.pause()
    client._release.set()
    assert wait_until(lambda: len(results) == 2)
    assert wait_until(lambda: bool(paused_flag)), "il worker non è mai entrato in pausa"

    # In pausa: anche sbloccando la barriera il generatore non avanza —
    # il worker non chiama next() finché la pausa è attiva.
    client._release.set()
    time.sleep(0.3)
    assert len(results) == 2, "la scansione è avanzata durante la pausa"

    worker.resume()
    # Il worker risvegliato chiama next(): il generatore trova la
    # barriera ancora settata e produce subito il file 3.
    assert wait_until(lambda: len(results) == 3)

    # File 4: esplicito, con la solita choreografia.
    assert wait_until(lambda: client.waiting_at_barrier == 4)
    client._release.set()
    assert wait_until(lambda: len(results) == 4)

    worker.wait(5000)
    assert wait_until(lambda: bool(finished))
    assert finished[0] == (4, 4, 0, 0)  # (scansionati, infetti, errori, too_large)


def test_stop_while_paused(qapp, tmp_path):
    files = [tmp_path / f"g{i}.txt" for i in range(3)]
    worker, created = make_worker(files)
    results, paused_flag, finished = collect(worker)

    worker.start()
    assert wait_until(lambda: len(created) == 1)
    client = created[0]

    assert wait_until(lambda: client.waiting_at_barrier == 1)
    client._release.set()
    assert wait_until(lambda: len(results) == 1)
    assert wait_until(lambda: client.waiting_at_barrier == 2)

    worker.pause()
    client._release.set()  # completa il file 2, poi pausa effettiva
    assert wait_until(lambda: len(results) == 2)
    assert wait_until(lambda: bool(paused_flag))

    # Interrompi DURANTE la pausa: sveglia il worker, che esce pulito
    # con i conteggi parziali (non deve restare appeso né riprendere).
    worker.stop()
    worker.wait(5000)

    assert bool(finished), "finished_scan non emesso dopo stop in pausa"
    assert finished[0] == (2, 2, 0, 0)
    assert len(results) == 2
    assert not worker.isRunning()


def test_resume_after_long_pause_recreates_session(qapp, tmp_path, monkeypatch):
    # Soglia portata a 0: QUALSIASI pausa (anche di millisecondi) deve
    # ricreare la sessione. Equivale a simulare una pausa oltre
    # l'IdleTimeout di clamd senza aspettare 25 secondi.
    monkeypatch.setattr(sw_module, "CLAMD_IDLE_SAFETY_SECONDS", 0.0)

    files = [tmp_path / f"h{i}.txt" for i in range(2)]
    worker, created = make_worker(files)
    results, paused_flag, finished = collect(worker)

    worker.start()
    assert wait_until(lambda: len(created) == 1)
    client = created[0]
    assert client.reset_session_calls == 0

    assert wait_until(lambda: client.waiting_at_barrier == 1)
    client._release.set()
    assert wait_until(lambda: len(results) == 1)
    assert wait_until(lambda: client.waiting_at_barrier == 2)

    worker.pause()
    client._release.set()
    assert wait_until(lambda: len(results) == 2)
    assert wait_until(lambda: bool(paused_flag))

    worker.resume()
    worker.wait(5000)
    assert wait_until(lambda: bool(finished))
    assert client.reset_session_calls >= 1, (
        "la sessione non è stata ricreata dopo una pausa oltre soglia"
    )


def test_no_session_reset_without_pause(qapp, tmp_path):
    files = [tmp_path / f"i{i}.txt" for i in range(2)]
    worker, created = make_worker(files)
    results, _, finished = collect(worker, with_pause_flags=False)

    worker.start()
    assert wait_until(lambda: len(created) == 1)
    client = created[0]

    # Choreografia completa, barriera per barriera: senza di questa,
    # una release "in anticipo" viene consumata dalla barriera 1 e il
    # worker resta bloccato sulla 2 fino al timeout (la race che i
    # commenti sopra documentano).
    assert wait_until(lambda: client.waiting_at_barrier == 1)
    client._release.set()
    assert wait_until(lambda: len(results) == 1)
    assert wait_until(lambda: client.waiting_at_barrier == 2)
    client._release.set()

    worker.wait(5000)
    assert wait_until(lambda: bool(finished))
    assert finished[0] == (2, 2, 0, 0)
    # Senza pause() il ramo reset non deve mai attivarsi (soglia reale:
    # 25s; il flusso completo dura millisecondi).
    assert client.reset_session_calls == 0
