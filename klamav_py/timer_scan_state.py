"""
Stato dell'ultima scansione del timer di sistema: un marcatore di inizio e
fine che la GUI legge all'avvio per avvertire quando la scansione è stata
interrotta.

Il caso reale (AGENTS.md, annotazione della 0.1.17): il timer scansiona di
notte, il PC viene spento a metà scansione e systemd uccide il processo.
klamav-scan-notify non dice nulla (un'uscita per segnale non è un errore da
notificare, e durante lo spegnimento systemd non la accoderebbe comunque) e
Persistent=true non ripete l'attivazione, che risulta avvenuta: la home
resta controllata in parte senza che nessuno lo sappia, e un rilevamento di
quella notte non arriva mai all'utente. I rilevamenti «solo segnalazione»
sopravvivono già nelle segnalazioni da valutare (pending_reports.py,
0.1.18); il marcatore rende visibile il resto.

La scansione scrive l'inizio subito prima della traversata, dopo tutte le
validazioni bloccanti (una scansione mai partita non lascia marcatori), e la
fine nel finally: le uscite con codice 0, 1 e 2 passano dal finally e non
hanno bisogno di questo avviso (notifica già garantita da OnFailure); resta
senza fine solo la morte per segnale. Il marcatore si scrive solo dentro
klamav-scan.service, riconosciuto dal cgroup del processo
(in_timer_service): un'interruzione manuale dalla shell è visibile nel
terminale e non deve produrre avvisi al prossimo avvio della GUI. Non
INVOCATION_ID, come nella prima versione: in una sessione Plasma la
ereditano Konsole e le sue shell, quindi ogni scansione manuale scriveva il
marcatore, e chiudendo la finestra a metà lasciava un falso «scansione
programmata interrotta», o, completata, cancellava l'avviso di una vera
interruzione del timer.

Le scritture sono best-effort: chi le chiama non deve fallire per questo
(cmd_scan ingoia gli OSError), e un file non plausibile si ignora e basta —
non contiene dati dell'utente, la prossima scansione lo riscrive. Niente
lock: l'unica scansione del timer non si sovrappone a se stessa (unit
oneshot) e la GUI non scrive. Stessa disciplina dei permessi degli altri
file di stato (private_files.py: directory 0700, file 0600, atomico), senza
Qt.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .json_registry import is_text, is_time
from .private_files import ensure_private_dir, write_private_text
from .systemd_dropin import UNIT_NAME

STATE_NAME = "timer-scan.json"
# Cgroup del processo: per una unit di systemd il percorso termina con il
# nome della unit (…/app.slice/klamav-scan.service). Letto a ogni chiamata
# (in_timer_service), così i test possono sostituirlo.
CGROUP_FILE = Path("/proc/self/cgroup")
# Il file è di poche decine di byte: qualunque cosa più grande non è nostro.
MAX_STATE_BYTES = 64 * 1024


@dataclass(frozen=True)
class TimerScanState:
    """started_at e finished_at in secondi epoch; finished_at None finché la
    scansione non arriva alla fine."""

    started_at: float
    target: str
    finished_at: float | None


def in_timer_service(cgroup_file: Path | None = None) -> bool:
    """True se il processo gira dentro klamav-scan.service, cioè è la
    scansione del timer di sistema (o un suo avvio a mano con systemctl).
    Ogni riga del file (cgroup v2: «0::/percorso»; v1 ibrido:
    «N:controller:/percorso») si confronta sull'ultima componente del
    percorso. False se il file non si legge: niente marcatore, come prima
    di questa funzione."""
    try:
        text = (cgroup_file or CGROUP_FILE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[2].rstrip("/").rsplit("/", 1)[-1] == UNIT_NAME:
            return True
    return False


def default_state_path() -> Path:
    """Accanto agli altri file di stato. Letto a ogni chiamata, non
    all'import: HOME può cambiare (test, sudo -u)."""
    return Path.home() / ".local/share/klamav-py" / STATE_NAME


def interrupted_notice(state: TimerScanState) -> str:
    """Avviso unico per notifica della tray, etichetta della pagina
    Pianificazione e stderr (vedi MainWindow)."""
    when = time.strftime("%d/%m/%Y alle %H:%M", time.localtime(state.started_at))
    return (
        "L'ultima scansione programmata di sistema, avviata "
        f"il {when}, non risulta completata: il computer potrebbe essersi "
        "spento durante la scansione. I rilevamenti trovati fino a lì restano "
        "nelle pagine Segnalazioni e Quarantena; il dettaglio è nel journal "
        "(journalctl --user -u klamav-scan.service)."
    )


def _to_json(state: TimerScanState) -> str:
    return json.dumps(
        {
            "version": 1,
            "started_at": state.started_at,
            "target": state.target,
            "finished_at": state.finished_at,
        },
        indent=2,
    )


def _read_raw(path: Path) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_STATE_BYTES:
            return None
        data = fh.read(MAX_STATE_BYTES + 1)
    if len(data) > MAX_STATE_BYTES:
        return None
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _from_raw(raw: dict) -> TimerScanState | None:
    if raw.get("version") != 1:
        return None
    started_at = raw.get("started_at")
    target = raw.get("target")
    finished_at = raw.get("finished_at")
    if not is_time(started_at) or not is_text(target):
        return None
    if finished_at is not None and (not is_time(finished_at) or finished_at < started_at):
        return None
    return TimerScanState(started_at, target, finished_at)


def read_state(path: Path | None = None) -> TimerScanState | None:
    """Lo stato dell'ultima scansione del timer; None se il file manca o non
    è plausibile, e il chiamante lo tratta come «niente da avvisare»."""
    raw = _read_raw(path or default_state_path())
    return None if raw is None else _from_raw(raw)


def record_start(target: str, *, now: float | None = None, path: Path | None = None) -> None:
    """Scrive l'inizio della scansione: sovrascrive ogni stato precedente,
    quindi una nuova scansione che finisce cancella da sola l'avviso della
    precedente."""
    state = TimerScanState(
        now if now is not None else time.time(), str(target), None
    )
    path = path or default_state_path()
    ensure_private_dir(path.parent)
    write_private_text(path, _to_json(state))


def record_end(*, now: float | None = None, path: Path | None = None) -> None:
    """Chiude lo stato aperto da record_start. Se il file non c'è, non è
    plausibile o è già chiuso, non fa nulla: senza un inizio nostro non c'è
    niente da chiudere, e non si crea un file dal nulla."""
    path = path or default_state_path()
    state = read_state(path)
    if state is None or state.finished_at is not None:
        return
    write_private_text(
        path,
        _to_json(TimerScanState(
            state.started_at, state.target, now if now is not None else time.time()
        )),
    )
