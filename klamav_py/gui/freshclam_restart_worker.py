"""Worker GUI: riavvia l'unità freshclam e osserva l'esito tramite clamd.

Il codice di uscita di `systemctl restart` dice solo che il demone è partito.
L'esito reale dell'aggiornamento si legge dalla versione del DB che clamd
riporta prima e dopo; il journal serve solo come "console" di contorno.

Ciclo di vita: nessun parent Qt, da ritirare con _retire_qthread(). Tutte le
attese controllano isInterruptionRequested(), quindi il requestInterruption()
+ wait() di aboutToQuit lo chiude in tempi brevi.
"""
from __future__ import annotations

import subprocess
import time
from typing import Callable

from PySide6.QtCore import QThread, Signal

from klamav_py import freshclam_service as fs
from klamav_py.db_freshness import DbInfo
from klamav_py.freshclam_service import Outcome, RestartResult

DbProbe = Callable[[], "DbInfo | None"]


class FreshclamRestartWorker(QThread):
    progress = Signal(str)
    finished_with = Signal(object)  # RestartResult

    def __init__(self, db_probe: DbProbe, *, wait_timeout: float = 90.0,
                 poll_interval: float = 3.0, parent=None) -> None:
        super().__init__(parent)
        self._db_probe = db_probe
        self._wait_timeout = wait_timeout
        self._poll_interval = poll_interval

    def run(self) -> None:
        self.finished_with.emit(self._run())

    # -- helpers ---------------------------------------------------------

    def _probe(self) -> DbInfo | None:
        try:
            return self._db_probe()
        except Exception:  # clamd giù o risposta anomala: "sconosciuto"
            return None

    def _sleep(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.isInterruptionRequested():
                return False
            time.sleep(0.1)
        return True

    def _run_privileged(self, argv: list[str]) -> tuple[int | None, str]:
        """Esegue pkexec; None come rc significa interruzione."""
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        while True:
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if self.isInterruptionRequested():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return None, ""
        err = proc.stderr.read() if proc.stderr else ""
        return proc.returncode, err.strip()

    # -- flusso principale ----------------------------------------------

    def _run(self) -> RestartResult:
        unit = fs.resolve_unit()
        if unit is None:
            return RestartResult(Outcome.FAILED,
                                 "Nessuna unità freshclam installata "
                                 "(clamav-freshclam / freshclam).")
        argv = fs.build_restart_argv(unit)
        if argv is None:
            return RestartResult(Outcome.FAILED,
                                 "pkexec o systemctl non trovati in un "
                                 "percorso di sistema fidato.")

        before = self._probe()
        was_active = fs.is_active(unit)
        started = time.time()
        self.progress.emit(f"Riavvio di {unit} (richiede autorizzazione)…")

        try:
            rc, err = self._run_privileged(argv)
        except OSError as exc:
            return RestartResult(Outcome.FAILED, f"Avvio di pkexec fallito: {exc}")
        if rc is None:
            return RestartResult(Outcome.INTERRUPTED, "Interrotto alla chiusura.")
        if rc == fs.PKEXEC_DISMISSED:
            return RestartResult(Outcome.CANCELLED, "Autenticazione annullata.")
        if rc == fs.PKEXEC_NOT_AUTHORIZED:
            return RestartResult(Outcome.DENIED, "Autorizzazione negata.")
        if rc != 0:
            return RestartResult(Outcome.FAILED,
                                 f"systemctl restart {unit} fallito ({rc}): {err}")

        note = "" if was_active else (
            f" Nota: {unit} non era attivo e ora resta in esecuzione.")
        self.progress.emit("Unità riavviata, in attesa del nuovo database…")

        seen = 0
        latest = before
        deadline = time.monotonic() + self._wait_timeout
        while time.monotonic() < deadline:
            lines = fs.journal_lines(unit, started)
            for line in lines[seen:]:
                self.progress.emit(line)
            seen = max(seen, len(lines))

            if fs.is_failed(unit):
                return RestartResult(Outcome.FAILED,
                                     f"{unit} è in stato failed. "
                                     f"Dettagli: journalctl -u {unit}", latest)

            info = self._probe()
            if info is not None:
                latest = info
                if before is not None and info.version != before.version:
                    return RestartResult(
                        Outcome.UPDATED,
                        f"Database aggiornato: {before.version} → "
                        f"{info.version}.{note}", info)

            if not self._sleep(self._poll_interval):
                return RestartResult(Outcome.INTERRUPTED,
                                     "Interrotto alla chiusura.", latest)

        return RestartResult(
            Outcome.UNCHANGED,
            f"Nessun nuovo database caricato da clamd entro "
            f"{int(self._wait_timeout)} s: probabilmente era già aggiornato. "
            f"Dettagli: journalctl -u {unit}.{note}", latest)
