"""
Finestra principale PySide6 (UI Moderna Stile KDE Plasma).
Aggiunto supporto Single Instance (IPC) e fix ridimensionamento finestra.

Branding: il nome visualizzato ovunque è APP_NAME ("KlamAV-Py").
"KlamAV" da solo è il nome del progetto storico scomparso, non di
questo: non usato come nome visualizzato. Sussiste SOLO come namespace
legacy di QSettings nella migrazione one-shot (_migrate_legacy_settings).
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import os
import shutil
import subprocess
import sys
import time
import json
import html
from collections import deque
from dataclasses import dataclass, replace
from typing import Callable

from PySide6.QtCore import Qt, QSize, QSettings, Signal, QTimer, QFileSystemWatcher, QThread
from PySide6.QtGui import QIcon, QColor, QAction, QFont, QPalette, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..acknowledged import (
    HASH_PREFIX_LEN,
    AckRegistry,
    PendingReport,
    RegistryError,
    delete_if_same,
    recovery_notice,
)
from ..clamd_client import (
    DEFAULT_SOCKET, DEFAULT_TCP_PORT, ClamdEndpoint, ScanResult, unreadable_dir_line,
)
from ..quarantine import Quarantine, peek_entries
from ..quarantine_policy import REASON_MAIL_STORE
from ..quarantine_location import decide as decide_quarantine_dir, default_quarantine_dir, root_inside
from ..scan_exclusions import decide as decide_exclusion
from ..scan_totals import ScanTotals
from ..systemd_dropin import (
    DropinConflict, daemon_reload, disable_timer, foreign_overrides, render_dropin,
    sync_dropin, timer_enabled,
)
from ..private_files import (
    ensure_private_dir, ensure_private_file, open_private_for_write, write_private_text,
)
from .scan_worker import ScanWorker
from ..db_freshness import DbInfo, describe, should_update_on_startup
from ..db_update_policy import UpdateAvailability, update_availability
from ..clamd_health import ClamdHealth, Throttle, Transition
from .. import schedule as sched
from ..freshclam_service import Outcome, RestartResult
from .off_thread import run_off_gui_thread
from .freshclam_restart_worker import FreshclamRestartWorker
from .db_info_worker import DbInfoWorker, probe_db_info
from .ping_worker import PingWorker
from .single_instance import IPC_MAX_PAYLOAD_BYTES, IPC_SEPARATOR
from .update_check_worker import UpdateCheckWorker, UpdateInfo

# Fonte unica in quarantine_location: lo stesso percorso è nell'ExecStart
# di klamav-scan.service, e la coerenza è verificata dai test.
DEFAULT_QUARANTINE_DIR = default_quarantine_dir()
DEFAULT_HISTORY_FILE = Path.home() / ".local/share/klamav-py/history.json"
DEFAULT_LOGS_DIR = Path.home() / ".local/share/klamav-py/logs"

# Quanti log di scansioni programmate tenere su disco prima di iniziare
# a cancellare i più vecchi: una scansione oraria produce 24 file/giorno,
# senza rotazione il directory cresce indefinitamente.
MAX_BG_LOG_FILES = 10
# Scansione programmata della GUI: controllo della scadenza ogni minuto
# (orologio reale, vedi schedule.py) e attesa dopo l'avvio prima di
# recuperare una scadenza mancata, per non partire con una scansione di
# tutta la home proprio mentre la sessione si sta caricando.
SCHEDULE_CHECK_MS = 60_000
SCHEDULE_STARTUP_GRACE_S = 300
# Tetti di volume (punto "robustezza sotto carico"): un git clone o
# l'estrazione di un archivio grande generano decine di migliaia di
# eventi in pochi secondi, e una scansione della home con migliaia di
# errori di permessi produceva altrettante righe Qt.
MAX_REALTIME_QUEUE = 5000
REALTIME_DEBOUNCE_S = 3.0
REALTIME_DEBOUNCE_TICK_MS = 500
# Righe non-infette (errori, file troppo grandi) nella lista della pagina
# Scansione. Gli infetti si mostrano SEMPRE, a prescindere dal tetto.
MAX_RESULT_ROWS = 2000

# Nome dell'applicazione, usato OVUNQUE come nome visualizzato (titolo
# finestra, tooltip e notifiche tray, menu, file .desktop generati):
# un'unica costante invece di stringhe letterali sparse, così il
# branding non può più divergere tra i punti (era la causa del bug
# "KlamAV" vs "KlamAV-Py").
APP_NAME = "KlamAV-Py"

# Namespace QSettings PRIMA del rebranding: serve solo alla migrazione
# one-shot per leggere il vecchio ~/.config/KlamAV/KlamAV.conf.
_LEGACY_SETTINGS_ORG = "KlamAV"
_LEGACY_SETTINGS_APP = "KlamAV"

_BUNDLED_ICON_PATH = Path(__file__).parent / "resources" / "klamav-py.svg"


def _icon(*theme_names: str) -> QIcon:
    for name in theme_names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    if _BUNDLED_ICON_PATH.exists():
        return QIcon(str(_BUNDLED_ICON_PATH))
    return QIcon()


def _app_icon() -> QIcon:
    return _icon("klamav-py", "emblem-virus", "security-high", "security-medium")


def _mid_color(widget: QWidget) -> QColor:
    """Colore 'mid' della palette del tema, per testo decorativo.

    QColor("palette(mid)") non è valido: la sintassi palette(...) vale
    solo nei fogli di stile Qt, non nel costruttore di QColor.
    """
    return widget.palette().color(QPalette.Mid)


# Worker in attesa di distruzione: vedi _retire_qthread. Deve essere un
# riferimento Python forte e a livello di modulo, perché la sua unica
# ragione d'essere è sopravvivere all'uscita di scope del chiamante.
_in_ritiro: set[QThread] = set()

# Tempo COMPLESSIVO (non per singolo worker) concesso ai thread per
# terminare quando l'applicazione si chiude: vedi
# MainWindow._shutdown_workers. Con un wait() a tempo fisso per ognuno,
# sei worker bloccati terrebbero l'app appesa sei volte tanto.
_SHUTDOWN_DEADLINE_SECONDS = 3.0

# Intervallo minimo fra due controlli aggiornamenti AUTOMATICI (quelli
# all'avvio). Il pulsante in Impostazioni non è soggetto al limite: se
# l'utente lo preme vuole una risposta subito. L'API GitHub non
# autenticata concede 60 richieste l'ora per indirizzo IP: sei ore
# tengono il traffico a una richiesta per sessione di lavoro anche
# riavviando spesso l'applicazione, senza ritardare in modo sensibile la
# notifica di una versione nuova (il progetto pubblica poche release
# l'anno).
_UPDATE_CHECK_MIN_INTERVAL_SECONDS = 6 * 3600
_LAST_UPDATE_CHECK_KEY = "last_update_check"


def _controllo_aggiornamenti_dovuto(ultimo: float, adesso: float) -> bool:
    """
    True se il controllo automatico va eseguito.

    ultimo <= 0: nessun controllo registrato (prima esecuzione, o chiave
    assente perché la versione precedente non la scriveva).
    Un `ultimo` NEL FUTURO non deve bloccare i controlli per sempre:
    succede se l'orologio di sistema viene spostato indietro, o con un
    file di impostazioni copiato da un'altra macchina.
    """
    if ultimo <= 0:
        return True
    trascorso = adesso - ultimo
    if trascorso < 0:
        return True
    return trascorso >= _UPDATE_CHECK_MIN_INTERVAL_SECONDS


def _retire_qthread(worker: QThread) -> None:
    """
    Rilascio sicuro di un QThread la cui logica run() è finita ma il cui
    thread C++ può non essere ancora completamente terminato.

    Il crash "QThread: Destroyed while thread is still running" (SIGABRT
    via qFatal, osservato su Arch con Python 3.14 + PySide6 6.11 durante
    l'aggiornamento freshclam) scatta quando l'ultimo riferimento Python
    al wrapper cade PRIMA che QThread::finished sia stato consegnato:
    shiboken distrugge l'oggetto C++ mentre il thread è ancora in
    teardown. La finestra di gara è reale perché il segnale custom del
    worker (finished_scan/finished_with) è emesso DENTRO run(), prima
    che run() restituisca il controllo.

    Due casi distinti, con rimedi diversi:

    - Thread GIÀ terminato: deleteLater() basta, perché trasferisce la
      ownership dell'oggetto al C++. Da quel momento la caduta del
      riferimento Python è innocua. È il caso comune quando run() ha
      fatto lavoro lungo (freshclam) e la slot gira molto dopo.

    - Thread ANCORA in teardown: non basta agganciare deleteLater a
      finished. La connessione non trattiene il wrapper Python, quindi
      appena il chiamante esce di scope shiboken distrugge comunque
      l'oggetto C++ e il qFatal scatta lo stesso — la connessione non fa
      in tempo a servire a niente. Serve un riferimento Python forte che
      sopravviva al chiamante: da qui il set _in_ritiro, svuotato dalla
      stessa slot che poi chiama deleteLater().

    Il set è l'unica cosa che tiene in vita il wrapper nella finestra fra
    l'uscita del chiamante e l'arrivo di finished. finished è emesso dal
    thread che sta morendo ma l'oggetto vive nel thread GUI, quindi la
    connessione è queued e _finito() gira nel thread GUI a thread ormai
    terminato: è lì che deleteLater() è sicuro.
    """
    if worker.isFinished():
        worker.deleteLater()
        return

    _in_ritiro.add(worker)

    def _finito() -> None:
        _in_ritiro.discard(worker)
        worker.deleteLater()

    worker.finished.connect(_finito)


# Un singolo percorso di filesystem su Linux è al massimo PATH_MAX (4096
# byte, incluso il terminatore). Qualunque payload IPC più lungo di così
# non può essere un percorso legittimo: è o un errore del chiamante o un
# tentativo di far leggere/processare all'app dati arbitrariamente grandi
# (DoS). Il limite è generoso di proposito (margine per UTF-8 multi-byte)
# senza aprire la porta a payload da megabyte.
# Condiviso con il client (single_instance.encode_targets), che garantisce
# payload strettamente sotto il limite: raggiungerlo significa payload non
# nostro o troncato.
_IPC_MAX_PAYLOAD_BYTES = IPC_MAX_PAYLOAD_BYTES
# PATH_MAX su Linux: nessun percorso legittimo è più lungo. Il payload
# intero è più grande (selezione multipla), ma ogni singola parte resta
# vincolata come quando il payload conteneva un solo percorso.
_IPC_MAX_PATH_BYTES = 4096
# Tempo massimo per ricevere un payload completo: la connessione è locale,
# ma con qualche migliaio di percorsi può arrivare in più letture.
_IPC_READ_DEADLINE_S = 2.0


def _decode_ipc_payload(raw: bytes) -> str | None:
    """
    Decodifica il payload ricevuto dal socket IPC (single-instance),
    separata dagli effetti collaterali Qt/UI per essere testabile in
    isolamento.

    Ritorna None se il payload è vuoto o malformato: in nessun caso
    propaga un'eccezione al chiamante (che gira nell'event loop Qt), né
    prova a "recuperare" un input malformato interpretandolo alla meglio.
    """
    if not raw:
        return None
    try:
        data = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Payload malformato: un client IPC legittimo (la nostra stessa
        # app, da un'altra istanza) invia sempre un percorso UTF-8 valido.
        return None
    return data or None


def _decode_ipc_targets(raw: bytes, truncated: bool = False) -> list[str]:
    """
    Percorsi dal payload IPC: uno o più percorsi separati da NUL (formato
    di single_instance.encode_targets). Un payload senza NUL è il formato
    precedente, con un solo percorso. Ogni parte passa da
    _decode_ipc_payload: le parti malformate si scartano singolarmente.

    truncated=True (letto fino al limite): l'ultima parte può essere un
    percorso tagliato a metà, e un percorso tagliato è un ALTRO percorso
    esistente o no: la si scarta invece di scansionare la cosa sbagliata.
    """
    parts = raw.split(IPC_SEPARATOR)
    if truncated:
        parts = parts[:-1]
    parts = [part for part in parts if len(part) <= _IPC_MAX_PATH_BYTES]
    return [p for p in (_decode_ipc_payload(part) for part in parts) if p]


def _rebuild_kde_service_cache() -> None:
    """Rigenera la cache dei servizi KDE dopo aver installato/rimosso la
    voce di menu Dolphin.

    os.system() passa sempre per /bin/sh, quindi è preferibile
    subprocess.run() con un argv esplicito (nessuna shell, nessuna stringa
    da interpretare) anche quando, come qui, il comando è del tutto
    hardcoded: se in futuro uno di questi nomi diventasse configurabile,
    questa forma non aprirebbe comunque la porta a shell injection.
    kbuildsycoca6 è per KDE Plasma 6, kbuildsycoca5 è il fallback per
    Plasma 5: si prova entrambi, ignorando quello mancante (FileNotFoundError)
    o che fallisce (CalledProcessError) — nessuno dei due è fatale per
    l'operazione di installazione/rimozione già completata sul filesystem.
    """
    for binary in ("kbuildsycoca6", "kbuildsycoca5"):
        try:
            subprocess.run([binary], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (FileNotFoundError, OSError):
            pass


# Controllo periodico di clamd: intervallo e timeout del singolo ping.
# Il timeout resta ben sotto l'intervallo, così un clamd appeso non tiene
# occupato il worker fino al controllo successivo.
CLAMD_HEALTH_INTERVAL_MS = 60_000
CLAMD_PING_TIMEOUT_S = 10.0
# Distanza minima tra due ping forzati da errori del Real-Time.
CLAMD_FORCED_PING_MIN_INTERVAL_S = 10.0


UNREADABLE_DIRS_HINT = (
    "non controllate; se sono attese, per esempio cartelle create con sudo o "
    "bind mount di container, aggiungile alle cartelle escluse"
)


REPORTS_HINT = (
    "Alcuni file sono stati solo segnalati e lasciati al loro posto: dopo averli "
    "verificati, puoi eliminarli o registrarne la presa visione dalla pagina Segnalazioni."
)


def _acknowledged_line(path: str, signature: str) -> str:
    """Riga di log per una segnalazione già valutata (acknowledged.py)."""
    return f"GIÀ VALUTATO — {path} ({signature})"


def _totals_summary(totals: ScanTotals) -> str:
    """Contatori di una scansione programmata per la pagina Pianificazione."""
    text = (f"{totals.scanned} file, {totals.infections} infetti, "
            f"{totals.errors} errori")
    if totals.too_large:
        text += f", {totals.too_large} non verificati"
    if totals.unreadable_dirs:
        text += f", {totals.unreadable_dirs} cartelle non leggibili"
    if totals.acknowledged:
        text += f", {totals.acknowledged} già valutati"
    return text


def _outcome_line(outcome: str, detail: str) -> str | None:
    """Riga di log che completa "INFETTO — ..." con l'esito della
    quarantena automatica. None se non serve una riga in più (quarantena
    riuscita: il file compare già nella pagina Quarantena)."""
    if outcome == "report_only":
        return f"    ↳ NON messo in quarantena — {detail}"
    if outcome == "failed":
        return f"    ↳ quarantena FALLITA — {detail}"
    return None


def _default_tray_tooltip() -> str:
    """Tooltip di riposo della tray, con versione: è il valore a cui
    ogni flusso che modifica il tooltip (scansione manuale, programmata,
    pausa) deve tornare a fine attività."""
    return f"{APP_NAME} {__version__} — Protezione attiva"


def _harden_settings_file(settings: QSettings, create: bool) -> None:
    """0600 sul file di un QSettings; un fallimento non blocca l'avvio."""
    path = Path(settings.fileName())
    try:
        if create:
            # Al primo avvio ~/.config/KlamAV-Py/ non esiste ancora:
            # senza questa riga O_CREAT fallisce con ENOENT e il file
            # verrebbe poi creato da Qt con i permessi di default.
            ensure_private_dir(path.parent)
        ensure_private_file(path, create=create)
    except OSError as exc:
        print(
            f"{APP_NAME}: impossibile restringere i permessi di "
            f"{settings.fileName()}: {exc}",
            file=sys.stderr,
        )


def _migrate_legacy_settings() -> None:
    """
    Migrazione one-shot del file delle impostazioni dopo il rebranding.

    Fino alla 0.1.3-1 org/app QSettings erano "KlamAV"/"KlamAV", quindi
    le impostazioni vivevano in ~/.config/KlamAV/KlamAV.conf. Rinominare
    org/app in "KlamAV-Py" sposta il file: senza migrazione, al primo
    avvio tutte le impostazioni (socket, quarantena, Real-Time,
    pianificazione, autostart) risulterebbero silenziosamente al
    default. Qui, se il nuovo file è vuoto, copiamo tutte le chiavi del
    vecchio; il vecchio file resta su disco, nessun dato distrutto.
    Chiamata all'inizio di MainWindow.__init__, PRIMA della creazione
    delle pagine (che costruiscono i loro QSettings espliciti).
    """
    new = QSettings(APP_NAME, APP_NAME)
    # Permessi del file di configurazione: Qt lo crea 0644, ma contiene
    # le cartelle monitorate dal Real-Time, il target delle scansioni
    # pianificate e il percorso di quarantena. Stringerlo qui, prima di
    # qualunque scrittura: QSaveFile preserva i permessi di un file
    # esistente, quindi 0600 resta tale a ogni sync successivo.
    # Vedi private_files.ensure_private_file.
    _harden_settings_file(new, create=True)

    old = QSettings(_LEGACY_SETTINGS_ORG, _LEGACY_SETTINGS_APP)
    # Il vecchio file contiene le stesse chiavi e sulle installazioni
    # aggiornate dalla 0.1.3 è ancora 0644. create=False: se non esiste
    # non va creato (genererebbe una ~/.config/KlamAV/ vuota). Va
    # ristretto PRIMA dell'early return qui sotto: sulle installazioni
    # che hanno già migrato (conf nuovo popolato da una versione >= 0.1.4)
    # la migrazione non riparte, e il legacy — se ancora su disco —
    # resterebbe 0644 per sempre.
    _harden_settings_file(old, create=False)

    if new.allKeys():
        return  # già migrato, o già configurato: non toccare nulla
    if not old.allKeys():
        return  # nessuna installazione precedente: niente da migrare
    for key in old.allKeys():
        new.setValue(key, old.value(key))
    new.sync()


def _gui_relaunch_command() -> str:
    """
    Comando da usare in un file .desktop (autostart, integrazione
    Dolphin) per rilanciare la GUI.

    Preferisce lo script installato dal pacchetto (`klamav-py-gui`,
    disponibile nel PATH una volta installato via .deb/pip), perché
    non fa nessuna assunzione sulla posizione del sorgente sul disco.
    Ricade su "sys.executable -m klamav_py.gui.app" solo per lo
    sviluppo locale da checkout git con venv attivo, dove lo script
    entry-point non è nel PATH ma il modulo è comunque importabile.
    """
    installed = shutil.which("klamav-py-gui")
    if installed:
        return installed
    return f"{sys.executable} -m klamav_py.gui.app"


KDE_STYLESHEET = """
    QMainWindow { background-color: palette(window); }

    QListWidget#Sidebar {
        background-color: transparent;
        border: none;
        outline: none;
    }
    QListWidget#Sidebar::item {
        padding: 12px 15px;
        border-radius: 6px;
        margin: 2px 5px;
    }
    QListWidget#Sidebar::item:hover {
        background-color: palette(mid);
    }
    QListWidget#Sidebar::item:selected {
        background-color: palette(highlight);
        color: palette(highlightedText);
    }

    QSplitter::handle {
        background-color: palette(mid);
        width: 1px;
    }
    QSplitter::handle:hover {
        background-color: palette(highlight);
    }

    QPushButton {
        padding: 8px 16px;
        border-radius: 6px;
        border: 1px solid palette(mid);
        background-color: palette(button);
        color: palette(buttonText);
    }
    QPushButton:hover {
        background-color: palette(dark);
        border: 1px solid palette(dark);
    }
    QPushButton:pressed {
        background-color: palette(mid);
    }
    QPushButton:disabled {
        color: palette(windowText);
        background-color: palette(window);
    }
    QPushButton#PrimaryButton {
        background-color: palette(highlight);
        color: palette(highlightedText);
        border: none;
        font-weight: bold;
    }
    QPushButton#PrimaryButton:hover {
        background-color: palette(dark);
    }

    QTableWidget {
        border: 1px solid palette(mid);
        border-radius: 8px;
        gridline-color: transparent;
        background-color: palette(base);
        alternate-background-color: palette(window);
    }
    QHeaderView::section {
        background-color: palette(window);
        padding: 8px;
        border: none;
        border-bottom: 1px solid palette(mid);
        font-weight: bold;
    }
    QListWidget#ResultsList, QListWidget#MonitoredDirsList, QListWidget#RealTimeLog,
    QListWidget#ExcludedDirsList {
        border: 1px solid palette(mid);
        border-radius: 8px;
        background-color: palette(base);
        padding: 5px;
    }

    QGroupBox {
        border: 1px solid palette(mid);
        border-radius: 8px;
        margin-top: 16px;
        padding-top: 16px;
        font-weight: bold;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 15px;
        padding: 0 5px;
    }

    QPlainTextEdit#LogConsole {
        border: 1px solid palette(mid);
        border-radius: 8px;
        background-color: palette(base);
        padding: 10px;
    }
"""


def load_endpoint(settings: QSettings, default: ClamdEndpoint | None = None) -> ClamdEndpoint:
    """
    Endpoint di clamd dalle Impostazioni: UNICO punto di lettura delle
    quattro chiavi (clamd_transport, socket_path, tcp_host, tcp_port).

    Nessuna migrazione necessaria: un'installazione precedente non ha
    clamd_transport e viene letta come "unix" con il suo socket_path, cioè
    il comportamento di prima. default fornisce i valori delle chiavi
    assenti (la GUI passa quello di --socket/--tcp, che restano valori
    iniziali). ValueError se i valori salvati non formano un endpoint
    valido (file modificato a mano).
    """
    default = default or ClamdEndpoint()
    raw_port = settings.value("tcp_port", default.tcp_port)
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ValueError(f"porta TCP non valida nelle impostazioni: {raw_port!r}") from None
    return ClamdEndpoint(
        transport=str(settings.value("clamd_transport", default.transport)),
        unix_socket=str(settings.value("socket_path", default.unix_socket)),
        tcp_host=str(settings.value("tcp_host", default.tcp_host) or ""),
        tcp_port=port,
    )


def save_endpoint(settings: QSettings, endpoint: ClamdEndpoint) -> None:
    """Scrive tutte e quattro le chiavi: host e porta restano salvati anche
    tornando al socket Unix, così non vanno riscritti per ripassare a TCP."""
    settings.setValue("clamd_transport", endpoint.transport)
    settings.setValue("socket_path", endpoint.unix_socket)
    settings.setValue("tcp_host", endpoint.tcp_host)
    settings.setValue("tcp_port", endpoint.tcp_port)


SCHEDULE_EXCLUDES_KEY = "schedule_excludes"


def load_schedule_excludes(settings: QSettings) -> list[str]:
    """
    Cartelle escluse dalle scansioni programmate, nella forma salvata
    (scan_exclusions: expanduser, assoluta, NON risolta): UNICO punto di
    lettura della chiave. Una lista sola per pianificazione interna e
    timer di sistema. type=list normalizza i casi di QSettings: chiave
    assente, lista vuota e lista di un solo elemento letta come stringa.
    """
    value = settings.value(SCHEDULE_EXCLUDES_KEY, [], type=list)
    return [str(v) for v in value if str(v).strip()]


def schedule_roots(target: str, home: Path | None = None) -> dict[str, Path]:
    """
    Radici con cui confrontare le esclusioni: la home per il timer di
    sistema (ExecStart: scan %h) e la cartella della pianificazione
    interna. Una cartella interna vuota o relativa non è una radice: il
    salvataggio della pianificazione non la valida, e la scansione la
    segnala come inesistente.
    """
    roots = {"timer di sistema": home or Path.home()}
    if target and target.strip():
        path = Path(target).expanduser()
        if path.is_absolute():
            roots["pianificazione interna"] = path
    return roots


def dropin_followup(changed: bool, tcp: bool) -> tuple[str | None, list]:
    """
    Dopo la scrittura del drop-in: daemon-reload se è cambiato e override
    estranei della unit. Chiama systemctl --user (timeout 10 s): la GUI la
    esegue con run_off_gui_thread. Ritorna (motivo del reload fallito o
    None, override estranei).
    """
    problem = daemon_reload() if changed else None
    return problem, foreign_overrides(tcp=tcp)


def dropin_notes(problem: str | None, overrides: list, *, what: str) -> list[str]:
    """Avvisi non bloccanti del salvataggio, dal risultato di dropin_followup.
    Un override.conf (systemctl --user edit) che ridefinisce ExecStart viene
    dopo il nostro drop-in e vince: solo avviso, il file è dell'utente."""
    notes = []
    if problem:
        notes.append(
            f"La scansione programmata di sistema userà {what} "
            "dal prossimo avvio della sessione "
            f"(systemctl --user daemon-reload non riuscito: {problem})."
        )
    notes.extend(o.describe() for o in overrides)
    return notes


def validate_schedule(
    raws: list[str],
    roots: dict[str, Path],
    target_text: str,
    quarantine_raw: str,
    require_target: bool,
) -> tuple[list[str], list[str]]:
    """
    Validazione del salvataggio della Pianificazione: (esclusioni nella
    forma salvata, problemi bloccanti). Tocca il filesystem: la GUI la
    esegue con run_off_gui_thread.

    Lista e cartella interna si validano insieme, qualunque delle due sia
    cambiata. La quarantena si controlla con la regola inversa di quella
    delle Impostazioni: una cartella interna al suo interno verrebbe
    esclusa per intero dalla scansione. Con la pianificazione interna
    attiva la cartella dev'essere una directory esistente, come per
    `klamav-py scan`: prima un file o un percorso relativo passavano e la
    scansione poi non partiva, o partiva senza escludere nulla.
    """
    stored, problems = [], []
    for raw in raws:
        decision = decide_exclusion(raw, roots=roots)
        if decision.error:
            problems.append(decision.error)
        else:
            stored.append(decision.stored)
    if problems:
        problems.insert(0, "Alcune cartelle escluse non sono valide:")

    target = roots.get("pianificazione interna")
    if require_target:
        if target is None:
            problems.append(
                f"Cartella da scansionare non valida («{target_text}»): serve un "
                "percorso assoluto, per esempio ~/Documenti."
            )
        elif not target.is_dir():
            what = "non è una directory" if target.exists() else "non esiste"
            problems.append(f"La cartella da scansionare «{target}» {what}.")
    if target is not None:
        quarantine = Path(quarantine_raw).expanduser().resolve()
        problem = root_inside(quarantine, {"pianificazione interna": target})
        if problem:
            problems.append(problem)
    return stored, problems


def _quarantine_count(path: Path) -> int:
    """Voci nell'indice di una quarantena esistente; 0 se non c'è o non si
    legge. Solo informativo: non deve mai bloccare il salvataggio, né
    creare la directory se manca, né recuperare operazioni interrotte
    (peek_entries invece del costruttore di Quarantine)."""
    path = path.expanduser()
    if not path.is_dir():
        return 0
    try:
        return len(peek_entries(path))
    except Exception:  # noqa: BLE001 - dato informativo
        return 0


class HistoryManager:
    def __init__(self, file_path: Path = DEFAULT_HISTORY_FILE):
        self.file_path = file_path
        # La directory della cronologia (di default ~/.local/share/klamav-py)
        # contiene anche i log delle scansioni programmate e la quarantena
        # di default: renderla 0700 qui, all'avvio, protegge tutto ciò che
        # ci sta sotto a prescindere dai permessi dei singoli file e della
        # home. Vedi private_files.py. Un fallimento non deve impedire
        # l'avvio dell'antivirus: si segnala e si prosegue.
        try:
            ensure_private_dir(self.file_path.parent)
        except OSError as exc:
            print(
                f"{APP_NAME}: impossibile rendere privata {self.file_path.parent}: {exc}",
                file=sys.stderr,
            )

    def add_entry(
        self,
        scan_type: str,
        target: str,
        totals: ScanTotals | None = None,
        log_file: str | None = None,
    ):
        # I contatori sono i campi di ScanTotals. Le voci scritte da
        # versioni precedenti non hanno i campi più recenti (too_large,
        # unreadable_dirs): si leggono con ScanTotals.from_entry, che li
        # vale 0. None = nessun file controllato (scansione rinviata).
        # log_file, se presente, punta al log dettagliato su disco
        # (scansioni background: il dettaglio infetti/errori non vive
        # in nessuna lista UI, quindi senza questo riferimento sarebbe
        # irrecuperabile).
        entries = self.get_entries()
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": scan_type,
            "target": target,
            **(totals or ScanTotals()).as_entry(),
        }
        if log_file is not None:
            entry["log_file"] = log_file
        entries.append(entry)
        if len(entries) > 1000:
            entries = entries[-1000:]
        try:
            # Scrittura atomica e 0600: vedi private_files.write_private_text.
            write_private_text(self.file_path, json.dumps(entries, indent=4))
        except Exception:
            pass

    def get_entries(self) -> list:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []

    def clear(self):
        try:
            write_private_text(self.file_path, json.dumps([]))
        except Exception:
            pass


class ScanPage(QWidget):
    def __init__(self, endpoint: ClamdEndpoint, quarantine: Quarantine, history: HistoryManager, parent=None) -> None:
        super().__init__(parent)
        self.endpoint = endpoint
        self.quarantine = quarantine
        self.history = history
        self.worker: ScanWorker | None = None
        self._scanned = self._infections = self._errors = self._too_large = 0
        self._scan_start_time: float | None = None

        self.path_edit = QLineEdit(str(Path.home()))
        self._external_targets: list[Path] | None = None
        # textEdited scatta solo per modifiche dell'utente, non per setText.
        self.path_edit.textEdited.connect(self._on_path_edited)
        self.path_edit.setFixedHeight(36)

        browse_button = QPushButton("Sfoglia…")
        browse_button.setFixedHeight(36)
        browse_button.setIcon(QIcon.fromTheme("document-open"))
        browse_button.clicked.connect(self._browse)

        self.start_button = QPushButton("Avvia scansione")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.setFixedHeight(36)
        self.start_button.setIcon(QIcon.fromTheme("media-playback-start"))
        self.start_button.clicked.connect(self._start_scan)

        self.pause_button = QPushButton("Sospendi")
        self.pause_button.setFixedHeight(36)
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))
        self.pause_button.setEnabled(False)
        self.pause_button.clicked.connect(self._toggle_pause)

        self.stop_button = QPushButton("Interrompi")
        self.stop_button.setFixedHeight(36)
        self.stop_button.setIcon(QIcon.fromTheme("media-playback-stop"))
        self.stop_button.clicked.connect(self._stop_scan)
        self.stop_button.setEnabled(False)

        self.auto_quarantine_checkbox = QCheckBox("Metti in quarantena automaticamente i file infetti")
        self.auto_quarantine_checkbox.setChecked(False)

        self.quarantine_selected_button = QPushButton("Metti in quarantena i selezionati")
        self.quarantine_selected_button.setIcon(QIcon.fromTheme("edit-delete"))
        self.quarantine_selected_button.clicked.connect(self._quarantine_selected)

        self.copy_log_button = QPushButton("Copia log")
        self.copy_log_button.setIcon(QIcon.fromTheme("edit-copy"))
        self.copy_log_button.clicked.connect(self._copy_log)

        self.status_label = QLabel("Pronto.")
        self.status_label.setStyleSheet("font-size: 14px; color: palette(mid);")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # FIX SFARFALLIO (BUG-004): il testo completo (con percorso file)
        # viene salvato qui e mostrato troncato nel mezzo con "…". Senza
        # elisione, un percorso molto lungo fa crescere il sizeHint della
        # label e quindi il layout dell'intera pagina ad ogni singolo
        # aggiornamento, che è una delle cause dello sfarfallio/
        # ridimensionamento della finestra durante una scansione.
        self._status_full_text = "Pronto."
        self._status_max_width = 640

        self.counts_label = QLabel("")
        self.counts_label.setStyleSheet("font-size: 12px; color: palette(mid);")

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)

        self.results_list = QListWidget()
        self.results_list.setObjectName("ResultsList")
        self.results_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.results_list.setUniformItemSizes(True)
        self.results_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.results_list.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

        # FIX: Permette alla lista di espandersi verticalmente e ridimensionare la finestra
        self.results_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        path_row = QHBoxLayout()
        path_row.setSpacing(10)
        path_row.addWidget(QLabel("Percorso:"))
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse_button)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)
        buttons_row.addWidget(self.start_button)
        buttons_row.addWidget(self.pause_button)
        buttons_row.addWidget(self.stop_button)
        buttons_row.addStretch()

        results_buttons_row = QHBoxLayout()
        results_buttons_row.setSpacing(10)
        results_buttons_row.addWidget(self.quarantine_selected_button)
        results_buttons_row.addWidget(self.copy_log_button)
        results_buttons_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Scansione Antivirus")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)
        layout.addSpacing(10)

        layout.addLayout(path_row)
        layout.addLayout(buttons_row)
        layout.addWidget(self.auto_quarantine_checkbox)
        layout.addWidget(self.progress)
        layout.addWidget(self.status_label)
        layout.addWidget(self.counts_label)
        layout.addWidget(self.results_list, 1)
        layout.addLayout(results_buttons_row)

    def start_external_scan(self, target) -> None:
        """Metodo richiamato quando l'app riceve file da scansionare
        esternamente (es. Dolphin): un percorso o una lista, la selezione
        multipla passata con %F. Una sola scansione per tutta la selezione."""
        targets = list(target) if isinstance(target, (list, tuple)) else [target]
        targets = [Path(t) for t in targets if t and Path(t).exists()]
        if not targets:
            return
        if len(targets) == 1:
            self._external_targets = None
            self.path_edit.setText(str(targets[0]))
        else:
            self._external_targets = targets
            # Solo descrittivo (cronologia e report lo mostrano così): la
            # lista vera è _external_targets. Una modifica a mano del campo
            # la annulla, vedi _on_path_edited.
            self.path_edit.setText(f"{targets[0]} (+{len(targets) - 1} altri)")
        QTimer.singleShot(500, self._start_scan)

    def _on_path_edited(self, _text: str) -> None:
        self._external_targets = None

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Scegli directory da scansionare", self.path_edit.text())
        if chosen:
            self._external_targets = None
            self.path_edit.setText(chosen)

    def _start_scan(self) -> None:
        external = getattr(self, "_external_targets", None)
        if external:
            targets = [t for t in external if t.exists()]
            if not targets:
                QMessageBox.warning(self, "Percorso non valido", "Nessuno dei file selezionati esiste più.")
                return
            target = targets
        else:
            target = Path(self.path_edit.text())
            if not target.exists():
                QMessageBox.warning(self, "Percorso non valido", f"{target} non esiste.")
                return

        # Guard "una scansione alla volta": una scansione manuale non
        # parte se una programmata è già in corso (e viceversa la
        # programmata salta se c'è una manuale, vedi
        # MainWindow._run_scheduled_scan). Motivi: contesa su clamd
        # (due traversal home-wide in parallelo raddoppiano I/O e
        # memoria del demone) e doppio carico sulla UI. Il Real-Time
        # NON è coperto da questo guard deliberatamente: è la
        # protezione primaria, agisce su file singoli (secondi), e
        # clamd gestisce connessioni concorrenti per design —
        # sospenderlo mentre gira una manuale sarebbe peggio.
        main_window = self.window()
        if getattr(main_window, "bg_worker", None) is not None:
            QMessageBox.information(
                self,
                "Scansione già in corso",
                "Una scansione programmata è in corso in background.\n"
                "Attendi che termini prima di avviarne una manuale.",
            )
            return

        # Guard anti-doppio avvio: start_external_scan rilancia questa
        # funzione con un ritardo di 500ms e può sovrapporsi a un avvio
        # manuale fatto dall'utente nel frattempo.
        if self.worker is not None:
            return

        self.results_list.clear()
        self._omitted_errors = self._omitted_too_large = 0
        self._non_infected_rows = 0
        self._new_reports = 0
        self._aborted = None
        self.progress.setVisible(True)
        self._set_status_text("Scansione in corso…")
        self._scanned = self._infections = self._errors = self._too_large = 0
        self._scan_start_time = time.monotonic()
        self.counts_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.pause_button.setEnabled(True)
        # Stato di partenza del pulsante pausa: la scansione inizia
        # sempre da "in corso", mai da "in pausa".
        self.pause_button.setText("Sospendi")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))

        # Difesa: non dovrebbe mai esserci un worker residuo qui (ogni
        # fine scansione lo azzera, stop incluso), ma se ci fosse,
        # sovrascriverlo lo scaricherebbe col thread magari ancora in
        # teardown — parcheggio invece che rilascio immediato.
        if self.worker is not None:
            _retire_qthread(self.worker)
            self.worker = None

        self.worker = ScanWorker(
            endpoint=self.endpoint,
            target=target,
            quarantine_dir=self.quarantine.dir,
            auto_quarantine=self.auto_quarantine_checkbox.isChecked(),
        )
        self.worker.scanning.connect(self._on_scanning)
        self.worker.progress.connect(self._on_progress)
        self.worker.result_ready.connect(self._on_result)
        self.worker.quarantined.connect(self._on_quarantined)
        self.worker.quarantine_outcome.connect(self._on_quarantine_outcome)
        self.worker.error.connect(self._on_error)
        self.worker.unreadable_dir.connect(self._on_unreadable_dir)
        self.worker.acknowledged.connect(self._on_acknowledged)
        self.worker.report_only_found.connect(self._on_report_only)
        # Prima di finished_scan (stesso emettitore, connessioni queued):
        # _on_finished lo trova già impostato. Senza, una scansione mai
        # partita (radice non valida, esclusione che la contiene, clamd
        # irraggiungibile) finiva «Completata senza problemi».
        self.worker.aborted.connect(self._on_aborted)
        self.worker.finished_scan.connect(self._on_finished)
        # I segnali paused/resumed (non le richieste pause()/resume())
        # comandano lo stato del pulsante: il worker li emette quando
        # entra/esce EFFETTIVAMENTE dalla pausa, cioè al confine tra
        # file — un click su "Sospendi" mentre un file grosso è ancora
        # in streaming non deve far cambiare stato alla UI subito.
        self.worker.paused.connect(self._on_worker_paused)
        self.worker.resumed.connect(self._on_worker_resumed)
        self.worker.start()

    def _toggle_pause(self) -> None:
        if self.worker is None:
            return
        if self.worker.is_pause_requested:
            self.worker.resume()
            # Il testo definitivo ("Sospendi" + status aggiornato) arriva
            # dal segnale resumed del worker, non da qui: qui la resume è
            # solo una richiesta non ancora servita.
            self._set_status_text("Ripresa in corso…")
        else:
            self.worker.pause()
            self._set_status_text("Pausa richiesta… (ha effetto al termine del file corrente)")

    def _on_worker_paused(self) -> None:
        self.pause_button.setText("Riprendi")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-start"))
        self._set_status_text("In pausa — riprende dal file successivo (Interrompi resta attivo)")
        # Visibilità da tray: anche in pausa, passando il mouse
        # sull'icona si deve capire lo stato (come per l'avanzamento).
        main_window = self.window()
        if hasattr(main_window, "tray_icon"):
            main_window.tray_icon.setToolTip(f"{APP_NAME} — Scansione in pausa")

    def _on_worker_resumed(self) -> None:
        self.pause_button.setText("Sospendi")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))
        self._set_status_text("Scansione in corso…")
        # Il tooltip torna "in corso"; il prossimo tick di _on_progress
        # (entro 150ms) lo aggiorna comunque coi contatori.

    def _stop_scan(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self._set_status_text("Interruzione richiesta…")

    def _set_status_text(self, text: str) -> None:
        """
        Imposta il testo di stato troncandolo (con elisione nel mezzo) a
        una larghezza massima fissa, invece di lasciare che un percorso
        lunghissimo faccia crescere senza limiti il sizeHint della label
        (vedi commento nel costruttore, BUG-004).
        """
        self._status_full_text = text
        metrics = self.status_label.fontMetrics()
        elided = metrics.elidedText(text, Qt.ElideMiddle, self._status_max_width)
        self.status_label.setText(elided)
        if elided != text:
            self.status_label.setToolTip(text)
        else:
            self.status_label.setToolTip("")

    def _on_scanning(self, path: str) -> None:
        self._set_status_text(f"Scansione in corso: {path}")

    def _on_progress(self, totals: ScanTotals) -> None:
        # Aggiornamento dei contatori, già filtrato/rallentato lato worker
        # (vedi scan_worker.PROGRESS_THROTTLE_SECONDS): qui ci si limita a
        # mostrare i valori ricevuti, senza fare altri calcoli.
        scanned, infections = totals.scanned, totals.infections
        self._scanned = scanned
        self._infections = infections
        self._errors = totals.errors
        self._too_large = totals.too_large
        text = f"{scanned} scansionati — {infections} infetti — {totals.errors} errori"
        if totals.too_large:
            text += f" — {totals.too_large} non verificati (troppo grandi)"
        if totals.unreadable_dirs:
            text += f" — {totals.unreadable_dirs} cartelle non leggibili"
        self.counts_label.setText(text)

        # Visibilità da tray per la scansione MANUALE: prima aggiornava
        # solo la label della pagina, e a finestra minimizzata in tray il
        # tooltip restava fermo su "Protezione attiva" anche con una
        # scansione home-wide in corso — indistinguibile da "non sta
        # facendo nulla". Stesso comportamento di _on_bg_progress per la
        # programmata (già throttled lato worker a 150ms, quindi il
        # tooltip non sfarfalla).
        main_window = self.window()
        if hasattr(main_window, "tray_icon"):
            main_window.tray_icon.setToolTip(
                f"{APP_NAME} — Scansione in corso: {scanned} file"
                + (f", {infections} infetti" if infections else "")
            )

    def _on_result(self, result: ScanResult) -> None:
        # Il worker filtra già i risultati "puliti": qui arrivano solo
        # infetti, errori e file troppo grandi, quindi ogni result
        # produce sempre una riga.
        if not result.infected:
            # Tetto sulle righe non infette: una scansione della home con
            # decine di migliaia di errori di permessi creava altrettanti
            # item Qt. Gli infetti non sono mai soggetti al tetto.
            if getattr(self, "_non_infected_rows", 0) >= MAX_RESULT_ROWS:
                if result.too_large:
                    self._omitted_too_large += 1
                elif result.status == "ERROR":
                    self._omitted_errors += 1
                return
            self._non_infected_rows = getattr(self, "_non_infected_rows", 0) + 1

        if result.infected:
            item = QListWidgetItem(f"INFETTO — {result.path} ({result.signature})")
            item.setIcon(QIcon.fromTheme("emblem-virus"))
            item.setForeground(QColor("#e4311b"))
            item.setData(Qt.UserRole, {"path": result.path, "signature": result.signature})
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()
        elif result.too_large:
            # Non è un errore: il file supera StreamMaxLength (clamd.conf)
            # e semplicemente non è stato verificato. Colore neutro
            # (non rosso) e icona diversa per non farlo sembrare un
            # malfunzionamento.
            item = QListWidgetItem(f"NON VERIFICATO (troppo grande) — {result.path}")
            item.setIcon(QIcon.fromTheme("dialog-information"))
            item.setForeground(_mid_color(self))
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()
        elif result.status == "ERROR":
            item = QListWidgetItem(f"ERRORE — {result.path}: {result.signature}")
            item.setIcon(QIcon.fromTheme("data-error"))
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()

    def _on_quarantined(self, original_path: str) -> None:
        # BUG-002: la quarantena automatica durante una scansione (worker
        # con auto_quarantine=True) avviene su un thread separato e senza
        # questo segnale la pagina Quarantena non se ne accorgerebbe fino
        # al riavvio dell'app.
        main_window = self.window()
        if hasattr(main_window, "quarantine_page"):
            main_window.quarantine_page.refresh()

    def _on_quarantine_outcome(self, path: str, outcome: str, detail: str) -> None:
        line = _outcome_line(outcome, detail)
        if line is None:
            return
        item = QListWidgetItem(line)
        item.setIcon(QIcon.fromTheme("dialog-warning"))
        self.results_list.addItem(item)
        self.results_list.scrollToBottom()

    def _on_error(self, message: str) -> None:
        item = QListWidgetItem(f"ERRORE SISTEMA — {message}")
        item.setIcon(QIcon.fromTheme("data-error"))
        self.results_list.addItem(item)

    def _on_acknowledged(self, path: str, signature: str) -> None:
        item = QListWidgetItem(_acknowledged_line(path, signature))
        item.setIcon(QIcon.fromTheme("emblem-checked"))
        item.setForeground(_mid_color(self))
        self.results_list.addItem(item)
        self.results_list.scrollToBottom()

    def _on_report_only(self, report: PendingReport) -> None:
        self._new_reports = getattr(self, "_new_reports", 0) + 1
        main_window = self.window()
        if hasattr(main_window, "reports_page"):
            main_window.reports_page.add_report(report)

    def _on_aborted(self, message: str) -> None:
        self._aborted = message

    def _on_unreadable_dir(self, path: str, reason: str) -> None:
        # Fuori dal tetto MAX_RESULT_ROWS: se ne riporta solo la cartella
        # più alta (os.walk non scende in una cartella illeggibile), quindi
        # sono poche, e ognuna è copertura mancante.
        item = QListWidgetItem(unreadable_dir_line(path, reason))
        item.setIcon(QIcon.fromTheme("dialog-warning"))
        self.results_list.addItem(item)
        self.results_list.scrollToBottom()

    def _copy_log(self) -> None:
        """BUG-003: copia negli appunti l'intero contenuto del log/risultati."""
        lines = [self.results_list.item(i).text() for i in range(self.results_list.count())]
        if not lines:
            QMessageBox.information(self, "Log vuoto", "Non c'è ancora nessun risultato da copiare.")
            return
        QApplication.clipboard().setText("\n".join(lines))
        self.status_label.setToolTip("")
        old_text = self.status_label.text()
        self.status_label.setText(f"Log copiato negli appunti ({len(lines)} righe).")
        QTimer.singleShot(2500, lambda: self.status_label.setText(old_text))

    def _quarantine_selected(self) -> None:
        selected = self.results_list.selectedItems()
        if not selected:
            QMessageBox.information(self, "Nessuna selezione", "Seleziona uno o più file infetti dalla lista.")
            return

        moved = 0
        skipped = 0
        for item in selected:
            data = item.data(Qt.UserRole)
            if not data:
                skipped += 1
                continue
            try:
                entry = self.quarantine.quarantine_file(Path(data["path"]), data.get("signature"))
            except Exception as exc:
                QMessageBox.warning(self, "Quarantena fallita", f"{data['path']}: {exc}")
                continue
            moved += 1
            item.setText(f"IN QUARANTENA — {entry.original_path} ({data.get('signature') or ''})")
            item.setForeground(QColor("gray"))
            item.setData(Qt.UserRole, None)

        if moved:
            # BUG-002: la quarantena manuale dalla pagina Scansione non
            # passa dal worker (viene fatta qui, direttamente sul thread
            # GUI), quindi va notificata a parte rispetto a _on_quarantined.
            main_window = self.window()
            if hasattr(main_window, "quarantine_page"):
                main_window.quarantine_page.refresh()

        if skipped and not moved:
            QMessageBox.information(self, "Nessun file infetto selezionato", "La selezione non contiene file infetti da mettere in quarantena.")

    @staticmethod
    def _format_duration(seconds: float) -> str:
        seconds = int(seconds)
        if seconds < 60:
            return f"{seconds}s"
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {secs}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m {secs}s"

    def _on_finished(self, totals: ScanTotals) -> None:
        scanned, infections = totals.scanned, totals.infections
        errors, too_large = totals.errors, totals.too_large
        self.progress.setVisible(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        # Reset del pulsante pausa: nessuna scansione attiva = disabilitato,
        # e il testo torna a "Sospendi" per il prossimo avvio (anche nel
        # caso la scansione sia stata interrotta mentre era in pausa).
        self.pause_button.setEnabled(False)
        self.pause_button.setText("Sospendi")
        self.pause_button.setIcon(QIcon.fromTheme("media-playback-pause"))

        # Il tooltip della tray torna al riposo qualunque sia stato il
        # finale (completata, interrotta, in pausa al momento dello stop).
        main_window = self.window()
        if hasattr(main_window, "_reset_tray_tooltip"):
            main_window._reset_tray_tooltip()

        duration = (
            self._format_duration(time.monotonic() - self._scan_start_time)
            if self._scan_start_time is not None
            else "n/d"
        )

        # Non completata: non eseguita (radice non valida, esclusioni, clamd
        # irraggiungibile all'avvio) o interrotta da un errore bloccante.
        # Stesso testo della pianificazione interna per lo stesso caso.
        aborted, self._aborted = getattr(self, "_aborted", None), None
        if aborted is not None:
            status_text = f"Scansione non completata: {aborted}"
        else:
            status_text = f"Completato: {scanned} file scansionati, {infections} infetti, {errors} errori."
            if too_large:
                status_text += f" {too_large} non verificati (troppo grandi)."
            if totals.unreadable_dirs:
                status_text += f" {totals.unreadable_dirs} cartelle non leggibili."
            if totals.acknowledged:
                status_text += f" {totals.acknowledged} già valutati."
        self._set_status_text(status_text)

        omessi_err = getattr(self, "_omitted_errors", 0)
        omessi_big = getattr(self, "_omitted_too_large", 0)
        if omessi_err or omessi_big:
            parti = []
            if omessi_err:
                parti.append(f"{omessi_err} errori")
            if omessi_big:
                parti.append(f"{omessi_big} file non verificati (troppo grandi)")
            item = QListWidgetItem(
                f"… altri {' e '.join(parti)} non mostrati (oltre {MAX_RESULT_ROWS} righe; "
                "i totali sopra sono completi, gli infetti sono sempre tutti elencati)"
            )
            item.setIcon(QIcon.fromTheme("dialog-information"))
            item.setForeground(_mid_color(self))
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()

        if aborted is not None:
            esito = f"Non completata: {aborted}"
        elif infections > 0:
            esito = "Infezioni rilevate"
        elif errors > 0:
            esito = "Completata con errori"
        else:
            esito = "Completata senza problemi"

        # BUG-001: report esplicito di fine scansione, non solo un
        # aggiornamento silenzioso della status_label.
        report_text = (
            f"Percorso: {self.path_edit.text()}\n"
            f"Durata: {duration}\n"
            f"File scansionati: {scanned}\n"
            f"File infetti: {infections}\n"
            f"Errori: {errors}\n"
            f"Non verificati (troppo grandi): {too_large}\n"
            + (f"Cartelle non leggibili: {totals.unreadable_dirs} "
               f"({UNREADABLE_DIRS_HINT})\n" if totals.unreadable_dirs else "")
            + (f"Segnalazioni già valutate: {totals.acknowledged}\n" if totals.acknowledged else "")
            + f"Esito: {esito}"
            + (f"\n\n{REPORTS_HINT}" if getattr(self, "_new_reports", 0) else "")
        )

        self.history.add_entry(
            "Manuale" if aborted is None else "Manuale (non completata)",
            self.path_edit.text(),
            totals,
        )
        if hasattr(main_window, 'history_page'):
            main_window.history_page.refresh()

        title = "Scansione completata" if aborted is None else "Scansione non completata"
        if hasattr(main_window, 'tray_icon'):
            if aborted is not None:
                icon_type = "dialog-warning"
            else:
                icon_type = "emblem-checked" if infections == 0 else "emblem-virus"
            main_window.tray_icon.showMessage(
                f"{APP_NAME} — {title}", status_text, _icon(icon_type), 6000
            )

        # Il popup esplicito compare solo se la finestra è visibile in
        # quel momento: se l'app è minimizzata in tray, forzare la
        # comparsa di un dialogo riporterebbe in primo piano una finestra
        # che l'utente aveva volutamente nascosto — in quel caso basta e
        # avanza la notifica tray sopra.
        if self.isVisible() and self.window().isVisible():
            report_box = QMessageBox(self)
            warn = infections > 0 or aborted is not None
            report_box.setIcon(QMessageBox.Warning if warn else QMessageBox.Information)
            report_box.setWindowTitle(title)
            report_box.setText(report_text)
            report_box.exec()

        # Rilascio differito, vedi _retire_qthread: l'emit di
        # finished_scan è dentro run(), il thread può non essere ancora
        # completamente terminato quando questa slot gira.
        worker, self.worker = self.worker, None
        if worker is not None:
            _retire_qthread(worker)


class QuarantinePage(QWidget):
    def __init__(self, quarantine: Quarantine, parent=None) -> None:
        super().__init__(parent)
        self.quarantine = quarantine

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["File originale", "Firma", "In quarantena dal"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        refresh_button = QPushButton("Aggiorna")
        refresh_button.setIcon(QIcon.fromTheme("view-refresh"))
        refresh_button.clicked.connect(self.refresh)

        restore_button = QPushButton("Ripristina")
        restore_button.setIcon(QIcon.fromTheme("document-revert"))
        restore_button.clicked.connect(self._restore_selected)

        delete_button = QPushButton("Elimina definitivamente")
        delete_button.setIcon(QIcon.fromTheme("edit-delete"))
        delete_button.clicked.connect(self._delete_selected)

        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(10)
        buttons_row.addWidget(refresh_button)
        buttons_row.addWidget(restore_button)
        buttons_row.addWidget(delete_button)
        buttons_row.addStretch()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("File in Quarantena")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)
        layout.addSpacing(10)

        # Visibile solo dopo un recupero da indice corrotto o con file non
        # indicizzati nella cartella: vedi Quarantine.corrupt_backups() e
        # Quarantine.orphans(). Mai nascosto automaticamente, finché la
        # situazione su disco non cambia.
        self.health_label = QLabel("")
        self.health_label.setTextFormat(Qt.PlainText)
        self.health_label.setWordWrap(True)
        self.health_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.health_label.setStyleSheet("font-size: 12px; color: #d32f2f;")
        self.health_label.setVisible(False)

        layout.addWidget(self.table, 1)
        layout.addWidget(self.health_label)
        layout.addLayout(buttons_row)

        self.refresh()

    def _update_health_label(self) -> None:
        try:
            backups = self.quarantine.corrupt_backups()
            orphans = self.quarantine.orphans()
        except OSError:
            backups, orphans = [], []
        parti = []
        if backups:
            nomi = ", ".join(p.name for p in backups[-3:])
            parti.append(
                f"L'indice della quarantena era danneggiato ed è stato messo da parte "
                f"({nomi}), non cancellato."
            )
        if orphans:
            parti.append(
                (f"{len(orphans)} file nella cartella {self.quarantine.dir} "
                 + ("non compare" if len(orphans) == 1 else "non compaiono")
                 + " nell'elenco: resta isolato in sola lettura"
                 + (" e il percorso originale è registrato nell'indice messo da parte."
                    if backups else "."))
            )
        self.health_label.setText(" ".join(parti))
        self.health_label.setVisible(bool(parti))

    def refresh(self) -> None:
        # list_entries() recupera da solo un indice corrotto: questa slot
        # non può più propagare JSONDecodeError e lasciare la pagina vuota
        # con un traceback su stderr. Resta il caso di un errore di I/O
        # vero (disco, permessi), mostrato invece di sollevato.
        try:
            entries = self.quarantine.list_entries()
        except OSError as exc:
            self.table.setRowCount(0)
            self.health_label.setText(f"Impossibile leggere la quarantena: {exc}")
            self.health_label.setVisible(True)
            return
        self._update_health_label()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            when = datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M")
            self.table.setItem(row, 0, QTableWidgetItem(entry.original_path))
            self.table.setItem(row, 1, QTableWidgetItem(entry.signature or ""))
            self.table.setItem(row, 2, QTableWidgetItem(when))
            self.table.item(row, 0).setData(Qt.UserRole, entry.quarantined_path)

    def _selected_quarantined_path(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.table.item(row, 0).data(Qt.UserRole)

    def _restore_selected(self) -> None:
        qpath = self._selected_quarantined_path()
        if qpath is None:
            return
        try:
            target = self.quarantine.restore(qpath)
        except Exception as exc:
            QMessageBox.warning(self, "Ripristino fallito", str(exc))
            return
        QMessageBox.information(self, "Ripristinato", f"File ripristinato in {target}")
        self.refresh()

    def _delete_selected(self) -> None:
        qpath = self._selected_quarantined_path()
        if qpath is None:
            return
        confirm = QMessageBox.question(
            self, "Conferma eliminazione", "Eliminare definitivamente il file in quarantena?"
        )
        if confirm != QMessageBox.Yes:
            return
        try:
            self.quarantine.delete(qpath)
        except Exception as exc:
            QMessageBox.warning(self, "Eliminazione fallita", str(exc))
            return
        self.refresh()


class ReportsPage(QWidget):
    """
    Segnalazioni non spostate (firme euristiche, archivi di posta: vedi
    quarantine_policy) e prese visione (acknowledged.py).

    Le segnalazioni arrivano dalle scansioni di questa sessione (manuale,
    programmata interna, Real-Time) con due azioni: la presa visione, che
    registra il contenuto riletto subito dopo il rilevamento (non quello
    del file al momento del clic), e l'eliminazione, solo se il percorso è
    ancora lo stesso file regolare. Il caso d'origine è la posta nel
    cestino, dove l'azione giusta è eliminare e prima l'unico modo era
    cercare il file a mano nel maildir.
    """

    def __init__(self, registry: AckRegistry | None = None, parent=None) -> None:
        super().__init__(parent)
        self.registry = registry if registry is not None else AckRegistry()
        self._pending: list[PendingReport] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Segnalazioni")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel(
            "File rilevati ma lasciati al loro posto (firme euristiche e archivi di posta). "
            "Dopo averli verificati puoi eliminarli, oppure registrare la presa visione: le "
            "scansioni successive, anche quella di sistema, non li contano più fra gli infetti "
            "finché il contenuto non cambia."
        )
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Registro messo da parte da questa pagina (registrazione, revoca,
        # ricarica): resta visibile, perché le segnalazioni che tornano
        # nuove sono la conseguenza e senza spiegazione sembrano un errore.
        self.recovery_label = QLabel("")
        self.recovery_label.setWordWrap(True)
        self.recovery_label.setStyleSheet("font-size: 12px; color: #d32f2f;")
        self.recovery_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.recovery_label.setVisible(False)
        layout.addWidget(self.recovery_label)

        self.pending_table = QTableWidget(0, 3)
        self.pending_table.setHorizontalHeaderLabels(["File", "Firma", "Rilevato"])
        self.pending_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.pending_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.pending_table.setTextElideMode(Qt.ElideMiddle)
        self.pending_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.pending_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.pending_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pending_table.verticalHeader().setVisible(False)
        layout.addWidget(self.pending_table, 1)

        self.ack_button = QPushButton("Ho verificato, non segnalare più")
        self.ack_button.setIcon(QIcon.fromTheme("dialog-ok-apply"))
        self.ack_button.clicked.connect(self._acknowledge_selected)
        self.delete_button = QPushButton("Elimina…")
        self.delete_button.setIcon(QIcon.fromTheme("edit-delete"))
        self.delete_button.clicked.connect(self._delete_selected)
        pending_row = QHBoxLayout()
        pending_row.addWidget(self.ack_button)
        pending_row.addWidget(self.delete_button)
        pending_row.addStretch()
        layout.addLayout(pending_row)

        ack_title = QLabel("Prese visione")
        ack_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(ack_title)

        self.ack_table = QTableWidget(0, 5)
        self.ack_table.setHorizontalHeaderLabels(
            ["Impronta", "Firma", "Primo percorso", "Presa visione", "Ultimo riscontro"]
        )
        self.ack_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.ack_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.ack_table.setTextElideMode(Qt.ElideMiddle)
        self.ack_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.ack_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.ack_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.ack_table.verticalHeader().setVisible(False)
        layout.addWidget(self.ack_table, 1)

        refresh_button = QPushButton("Aggiorna")
        refresh_button.setIcon(QIcon.fromTheme("view-refresh"))
        refresh_button.clicked.connect(self.refresh_acknowledged)
        self.revoke_button = QPushButton("Revoca")
        self.revoke_button.setIcon(QIcon.fromTheme("edit-undo"))
        self.revoke_button.clicked.connect(self._revoke_selected)
        ack_row = QHBoxLayout()
        ack_row.addWidget(refresh_button)
        ack_row.addWidget(self.revoke_button)
        ack_row.addStretch()
        layout.addLayout(ack_row)

        self._refresh_pending()
        self.refresh_acknowledged()

    # -- segnalazioni da valutare ------------------------------------------

    def add_report(self, report: PendingReport) -> None:
        """Una riga per file e firma: una nuova scansione dello stesso file
        sostituisce la riga, con l'identità appena riletta."""
        self._pending = [
            r for r in self._pending
            if (r.path, r.signature) != (report.path, report.signature)
        ]
        self._pending.append(report)
        self._refresh_pending()

    def pending(self) -> list[PendingReport]:
        return list(self._pending)

    def _refresh_pending(self) -> None:
        self.pending_table.setRowCount(len(self._pending))
        for row, report in enumerate(self._pending):
            when = datetime.fromtimestamp(report.found_at).strftime("%Y-%m-%d %H:%M")
            item = QTableWidgetItem(report.path)
            if report.identity is None:
                item.setToolTip("Il file non si è potuto rileggere dopo il rilevamento: "
                                "nessuna azione possibile da qui.")
            else:
                item.setToolTip(f"{report.path}\n{report.reason}")
            self.pending_table.setItem(row, 0, item)
            self.pending_table.setItem(row, 1, QTableWidgetItem(report.signature))
            self.pending_table.setItem(row, 2, QTableWidgetItem(when))

    def _selected_reports(self) -> list[PendingReport]:
        rows = sorted({i.row() for i in self.pending_table.selectedIndexes()})
        return [self._pending[r] for r in rows if r < len(self._pending)]

    def _acknowledge_selected(self) -> None:
        title = "Presa visione"
        reports = self._selected_reports()
        if not reports:
            QMessageBox.information(self, title, "Seleziona una o più segnalazioni.")
            return
        done = []
        for report in reports:
            if report.identity is None:
                QMessageBox.warning(
                    self, title,
                    f"{report.path}: il file non si è potuto rileggere dopo il rilevamento, "
                    "la presa visione non è registrabile. Rilancia la scansione.",
                )
                continue
            try:
                self.registry.acknowledge(report.identity.sha256, report.signature, Path(report.path))
            except (RegistryError, OSError) as exc:
                QMessageBox.warning(self, title, f"{report.path}: {exc}")
                continue
            done.append((report.identity.sha256, report.signature))
        # Lo stesso contenuto in altri percorsi è ora valutato anch'esso.
        self._pending = [
            r for r in self._pending
            if r.identity is None or (r.identity.sha256, r.signature) not in done
        ]
        self._refresh_pending()
        self.refresh_acknowledged()

    def _delete_selected(self) -> None:
        title = "Elimina file"
        reports = self._selected_reports()
        if len(reports) != 1:
            QMessageBox.information(self, title, "Seleziona una segnalazione alla volta.")
            return
        (report,) = reports
        if report.identity is None:
            QMessageBox.warning(
                self, title,
                f"{report.path}: il file non si è potuto rileggere dopo il rilevamento, "
                "quindi non si può verificare che sia ancora lo stesso. Non eliminato.",
            )
            return
        text = f"Eliminare definitivamente {report.path}?\n\nNon passa dal cestino."
        if report.reason == REASON_MAIL_STORE:
            text += (
                "\n\nÈ un file di un archivio di posta: se è gestito da Akonadi (KMail), "
                "dopo l'eliminazione può servire «akonadictl fsck»."
            )
        if QMessageBox.question(self, title, text) != QMessageBox.Yes:
            return
        try:
            delete_if_same(Path(report.path), report.identity)
        except (RegistryError, OSError) as exc:
            QMessageBox.warning(self, title, str(exc))
            return
        self._pending = [r for r in self._pending if r is not report]
        self._refresh_pending()

    # -- prese visione -----------------------------------------------------

    def _show_recovery(self) -> None:
        """Avviso del registro messo da parte, con il testo della CLI."""
        if self.registry.last_recovery is None:
            return
        notice = recovery_notice(*self.registry.last_recovery)
        self.recovery_label.setText(notice[0].upper() + notice[1:])
        self.recovery_label.setVisible(True)

    def refresh_acknowledged(self) -> None:
        # Registrazione e revoca finiscono qui: un unico punto per l'avviso.
        try:
            entries = self.registry.entries()
        except OSError as exc:
            entries = []
            QMessageBox.warning(self, "Prese visione", f"Registro non leggibile: {exc}")
        self._show_recovery()
        self._ack_entries = entries
        self.ack_table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            values = (
                e.sha256[:HASH_PREFIX_LEN],
                e.signature,
                e.first_path,
                datetime.fromtimestamp(e.acknowledged_at).strftime("%Y-%m-%d"),
                datetime.fromtimestamp(e.last_seen).strftime("%Y-%m-%d"),
            )
            for col, value in enumerate(values):
                self.ack_table.setItem(row, col, QTableWidgetItem(value))
            self.ack_table.item(row, 0).setToolTip(e.sha256)
            self.ack_table.item(row, 2).setToolTip(e.first_path)

    def _revoke_selected(self) -> None:
        rows = sorted({i.row() for i in self.ack_table.selectedIndexes()})
        keys = [self._ack_entries[r].key for r in rows if r < len(self._ack_entries)]
        if not keys:
            QMessageBox.information(self, "Revoca", "Seleziona una o più prese visione.")
            return
        try:
            self.registry.revoke(keys)
        except OSError as exc:
            QMessageBox.warning(self, "Revoca", f"Registro non scrivibile: {exc}")
        self.refresh_acknowledged()


class UpdatePage(QWidget):
    """
    Aggiornamento delle firme delegato all'unità systemd pacchettizzata di
    freshclam (vedi freshclam_service): nessuno script passa per pkexec e
    freshclam gira come utente clamav con il sandboxing della distribuzione.
    """

    db_info_changed = Signal(object)  # DbInfo | None

    def __init__(self, endpoint_getter: Callable[[], ClamdEndpoint], parent=None) -> None:
        super().__init__(parent)
        self._endpoint_getter = endpoint_getter
        self.worker: FreshclamRestartWorker | None = None
        # Ultima versione riportata da clamd: serve a decidere se il
        # riavvio del freshclam locale aggiorna davvero il suo database
        # (db_update_policy). None finché il probe non risponde.
        self._db_info: DbInfo | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Aggiornamento Database Virus")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel(
            "Aggiorna le definizioni dei virus riavviando il servizio di sistema "
            "freshclam, che le scarica con i propri permessi limitati.\n"
            "Potrebbe essere richiesta la password di amministratore."
        )
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.db_status_label = QLabel("Database firme: verifica in corso…")
        self.db_status_label.setTextFormat(Qt.PlainText)
        self.db_status_label.setWordWrap(True)
        layout.addWidget(self.db_status_label)
        layout.addSpacing(10)

        self.update_button = QPushButton("Aggiorna Database")
        self.update_button.setObjectName("PrimaryButton")
        self.update_button.setFixedHeight(36)
        self.update_button.setIcon(QIcon.fromTheme("system-software-update"))
        self.update_button.clicked.connect(self._start_update)

        btn_layout = QHBoxLayout()
        btn_layout.addWidget(self.update_button)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        # Perché il pulsante è disabilitato, detto dove si agisce (come la
        # label "Real-Time parziale"): clamd remoto o con un altro database.
        self.update_blocked_label = QLabel("")
        self.update_blocked_label.setTextFormat(Qt.PlainText)
        self.update_blocked_label.setWordWrap(True)
        self.update_blocked_label.setStyleSheet("font-size: 13px; color: palette(highlight);")
        self.update_blocked_label.setVisible(False)
        layout.addWidget(self.update_blocked_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(8)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.log_console = QPlainTextEdit()
        self.log_console.setObjectName("LogConsole")
        self.log_console.setReadOnly(True)
        font = QFont("Monospace")
        font.setStyleHint(QFont.TypeWriter)
        self.log_console.setFont(font)
        self.log_console.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_console, 1)

    def set_db_info(self, info: DbInfo | None) -> None:
        self._db_info = info
        self.db_status_label.setText(describe(info))
        self.refresh_availability()

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        # L'endpoint può essere cambiato nelle Impostazioni dopo l'ultimo
        # probe: la regola si ricalcola ogni volta che la pagina si mostra.
        super().showEvent(event)
        self.refresh_availability()

    def refresh_availability(self) -> UpdateAvailability:
        availability = update_availability(self._endpoint_getter(), self._db_info)
        self.update_button.setEnabled(availability.allowed and self.worker is None)
        self.update_blocked_label.setText(availability.reason or "")
        self.update_blocked_label.setVisible(not availability.allowed)
        return availability

    def _start_update(self) -> None:
        if self.worker is not None:
            return
        # Anche qui, non solo sul pulsante: l'aggiornamento all'avvio
        # (startup_update) chiama questo metodo direttamente, e con clamd
        # remoto chiederebbe una password per riavviare un servizio inutile.
        availability = self.refresh_availability()
        if not availability.allowed:
            self.log_console.appendPlainText(f"Aggiornamento non eseguito: {availability.reason}")
            return

        self.log_console.clear()
        self.progress.setVisible(True)
        self.update_button.setEnabled(False)
        self.log_console.appendPlainText("Avvio dell'aggiornamento in corso...\n")

        # L'endpoint si legge qui, nel thread GUI: il probe gira nel
        # worker e non deve toccare QSettings né i widget.
        endpoint = self._endpoint_getter()
        self.worker = FreshclamRestartWorker(lambda: probe_db_info(endpoint))
        self.worker.progress.connect(self._on_output)
        self.worker.finished_with.connect(self._on_finished)
        self.worker.start()

    def _on_output(self, line: str) -> None:
        self.log_console.appendPlainText(line)

    def _on_finished(self, result: RestartResult) -> None:
        self.progress.setVisible(False)
        self.log_console.appendPlainText(f"\n{result.message}")
        if result.db_info is not None:
            self.db_info_changed.emit(result.db_info)

        main_window = self.window()
        notify = result.outcome not in (Outcome.CANCELLED, Outcome.INTERRUPTED)
        if notify and hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            ok = result.outcome in (Outcome.UPDATED, Outcome.UNCHANGED)
            main_window.tray_icon.showMessage(
                f"{APP_NAME} - Aggiornamento", result.message,
                _icon("emblem-checked" if ok else "data-error"), 5000
            )

        # Rilascio differito: self.worker = None qui scaricherebbe il
        # QThread mentre run() sta ancora chiudendo (l'emit che ha
        # invocato questa slot è dentro run()) — vedi _retire_qthread.
        worker, self.worker = self.worker, None
        if worker is not None:
            _retire_qthread(worker)
        self.refresh_availability()


class RealTimePage(QWidget):
    def set_clamd_down(self, down: bool, pending: int = 0) -> None:
        if not down:
            self.clamd_banner.setVisible(False)
            return
        text = ("clamd non risponde: il Real-Time è SOSPESO e i nuovi file "
                "non vengono verificati.")
        if pending:
            text += f" {pending} file in attesa verranno analizzati al ritorno di clamd."
        self.clamd_banner.setText(text)
        self.clamd_banner.setVisible(True)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Monitor Real-Time")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel("Questa sezione mostra in tempo reale i file analizzati nelle cartelle monitorate.\nPer modificare le cartelle da monitorare, vai su Impostazioni.")
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Visibile solo con clamd fermo: vedi MainWindow._apply_clamd_state.
        self.clamd_banner = QLabel("")
        self.clamd_banner.setTextFormat(Qt.PlainText)
        self.clamd_banner.setWordWrap(True)
        self.clamd_banner.setStyleSheet(
            "font-size: 13px; font-weight: bold; color: #d32f2f;"
        )
        self.clamd_banner.setVisible(False)
        layout.addWidget(self.clamd_banner)
        layout.addSpacing(10)

        self.log_list = QListWidget()
        self.log_list.setObjectName("RealTimeLog")
        self.log_list.setUniformItemSizes(True)
        self.log_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.log_list.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.log_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_list, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        clear_btn = QPushButton("Pulisci Log")
        clear_btn.setFixedHeight(36)
        clear_btn.setIcon(QIcon.fromTheme("edit-clear-all"))
        clear_btn.clicked.connect(self.log_list.clear)

        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

    def add_log_entry(self, file_name: str, infected: bool, signature: str = "", status: str = "Analizzato") -> None:
        time_str = datetime.now().strftime("%H:%M:%S")
        if infected:
            # L'esito della quarantena arriva in una riga separata
            # (add_outcome_entry): qui non si può sapere se è riuscita.
            text = f"[{time_str}] MINACCIA RILEVATA: {file_name} ({signature})"
            item = QListWidgetItem(text)
            item.setIcon(QIcon.fromTheme("emblem-virus"))
            item.setForeground(QColor("#e4311b"))
        else:
            # status distingue "Analizzato (Sicuro)" da "Non verificato
            # (troppo grande)": senza questo, un file > StreamMaxLength
            # arrivava qui con l'etichetta "Sicuro" quando in realtà NON
            # è stato verificato — un falso rassicurante, il peggior
            # tipo di messaggio per un antivirus.
            text = f"[{time_str}] {status}: {file_name}"
            item = QListWidgetItem(text)
            if status.startswith("Non verificato"):
                item.setIcon(QIcon.fromTheme("dialog-information"))
                item.setForeground(QColor("gray"))
            else:
                item.setIcon(QIcon.fromTheme("emblem-checked"))
                item.setForeground(QColor("gray"))

        self._insert(item)

    def add_outcome_entry(self, text: str, warning: bool) -> None:
        item = QListWidgetItem(f"[{datetime.now():%H:%M:%S}]     ↳ {text}")
        if warning:
            item.setIcon(QIcon.fromTheme("dialog-warning"))
            item.setForeground(QColor("#e4311b"))
        else:
            item.setIcon(QIcon.fromTheme("emblem-checked"))
            item.setForeground(QColor("gray"))
        self._insert(item)

    def _insert(self, item: QListWidgetItem) -> None:
        self.log_list.insertItem(0, item)
        if self.log_list.count() > 500:
            self.log_list.takeItem(self.log_list.count() - 1)


class HistoryPage(QWidget):
    def __init__(self, history: HistoryManager, parent=None) -> None:
        super().__init__(parent)
        self.history = history

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Cronologia Scansioni")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel("Registro storico delle scansioni manuali e Real-Time eseguite sul sistema.")
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)
        layout.addSpacing(10)

        self.table = QTableWidget(0, 9)
        self.table.setHorizontalHeaderLabels(
            ["Data e Ora", "Tipo", "Percorso", "Scansionati", "Infetti", "Errori", "Non verificati",
             "Cartelle non leggibili", "Già valutati"]
        )
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.table, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        refresh_btn = QPushButton("Aggiorna")
        refresh_btn.setFixedHeight(36)
        refresh_btn.setIcon(QIcon.fromTheme("view-refresh"))
        refresh_btn.clicked.connect(self.refresh)

        clear_btn = QPushButton("Pulisci Cronologia")
        clear_btn.setFixedHeight(36)
        clear_btn.setIcon(QIcon.fromTheme("edit-clear-all"))
        clear_btn.clicked.connect(self._clear_history)

        btn_row.addWidget(refresh_btn)
        btn_row.addWidget(clear_btn)
        layout.addLayout(btn_row)

        self.refresh()

    def refresh(self) -> None:
        entries = self.history.get_entries()
        entries.reverse()
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            self.table.setItem(row, 0, QTableWidgetItem(entry.get("timestamp", "")))
            self.table.setItem(row, 1, QTableWidgetItem(entry.get("type", "")))
            self.table.setItem(row, 2, QTableWidgetItem(entry.get("target", "")))
            self.table.setItem(row, 3, QTableWidgetItem(str(entry.get("scanned", 0))))

            infections = entry.get("infections", 0)
            inf_item = QTableWidgetItem(str(infections))
            if infections > 0:
                inf_item.setForeground(QColor("#e4311b"))
            self.table.setItem(row, 4, inf_item)

            self.table.setItem(row, 5, QTableWidgetItem(str(entry.get("errors", 0))))

            # get() con default 0: le voci scritte prima dell'introduzione
            # del campo too_large non avevano questa chiave.
            self.table.setItem(row, 6, QTableWidgetItem(str(entry.get("too_large", 0))))
            # Idem per unreadable_dirs e acknowledged (0.1.13).
            totals = ScanTotals.from_entry(entry)
            self.table.setItem(row, 7, QTableWidgetItem(str(totals.unreadable_dirs)))
            self.table.setItem(row, 8, QTableWidgetItem(str(totals.acknowledged)))

            # Il log dettagliato (se esiste) è raggiungibile dal tooltip
            # sulla riga: senza questo, il riferimento nel JSON sarebbe
            # conoscibile solo a mano.
            log_file = entry.get("log_file")
            if log_file:
                self.table.item(row, 0).setToolTip(f"Log dettagliato: {log_file}")

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(self, "Conferma", "Cancellare tutta la cronologia delle scansioni?")
        if confirm == QMessageBox.Yes:
            self.history.clear()
            self.refresh()


@dataclass(frozen=True)
class _ScheduleForm:
    """Valori della pagina Pianificazione letti al clic su Salva."""
    enabled: bool
    interval: int
    unit: str
    target: str


class SchedulerPage(QWidget):
    schedule_saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # QSettings sempre con org/app ESPLICITI (mai QSettings() di
        # default): il valore non deve dipendere da come è stato creato
        # QApplication, e deve coincidere con quello della migrazione.
        self.settings = QSettings(APP_NAME, APP_NAME)
        # Valutazioni in corso fuori dal thread della GUI (vedi
        # _refresh_exclusions, _put_exclusion, _save_schedule).
        self._refresh_running = False
        self._refresh_again = False
        self._put_running = False
        self._save_running = False
        self._timer_refresh_running = False
        self._timer_refresh_again = False
        self.timer_state: bool | None = None

        # Scroll area come in SettingsPage: con il gruppo delle cartelle
        # escluse l'altezza minima supera quella della finestra predefinita
        # (900x600), e senza scroll i widget a dimensione fissa si
        # sovrapporrebbero invece di scorrere.
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        outer_layout.addWidget(scroll_area)
        content = QWidget()
        scroll_area.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Pianificazione Scansioni")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel("Imposta una scansione automatica in background. L'app deve rimanere aperta (anche nella system tray) per eseguire le scansioni programmate.")
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Il timer systemd (klamav-scan.timer) e questa pianificazione sono
        # alternativi: con entrambi attivi la home verrebbe scansionata due
        # volte, con due quarantene se le impostazioni divergono. La label
        # dice lo stato reale del timer nel punto dove si configura, come
        # "Real-Time parziale".
        self.system_timer_label = QLabel("")
        self.system_timer_label.setWordWrap(True)
        self.system_timer_label.setStyleSheet("font-size: 13px; color: palette(highlight);")
        self.system_timer_label.setVisible(False)
        layout.addWidget(self.system_timer_label)
        layout.addSpacing(10)

        schedule_group = QGroupBox("Pianificazione Automatica")
        s_layout = QVBoxLayout(schedule_group)

        self.enable_check = QCheckBox("Abilita scansione automatica")
        s_layout.addWidget(self.enable_check)

        time_row = QHBoxLayout()
        time_row.addWidget(QLabel("Esegui scansione ogni:"))

        self.interval_spin = QSpinBox()
        self.interval_spin.setMinimum(1)
        self.interval_spin.setMaximum(168)
        self.interval_spin.setFixedHeight(36)

        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["Ore", "Giorni"])
        self.unit_combo.setFixedHeight(36)

        time_row.addWidget(self.interval_spin)
        time_row.addWidget(self.unit_combo)
        time_row.addStretch()
        s_layout.addLayout(time_row)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Cartella da scansionare:"))

        self.target_edit = QLineEdit(str(Path.home()))
        self.target_edit.setFixedHeight(36)

        browse_btn = QPushButton("Sfoglia…")
        browse_btn.setFixedHeight(36)
        browse_btn.setIcon(QIcon.fromTheme("document-open"))
        browse_btn.clicked.connect(self._browse_dir)

        target_row.addWidget(self.target_edit, 1)
        target_row.addWidget(browse_btn)
        s_layout.addLayout(target_row)

        layout.addWidget(schedule_group)

        # Esclusioni: una lista sola per questa pianificazione e per il
        # timer di sistema (scan_exclusions). Validate contro entrambe le
        # radici; l'esito di ogni voce (icona e tooltip) è ricalcolato
        # quando cambia la cartella da scansionare.
        excl_group = QGroupBox("Cartelle escluse")
        e_layout = QVBoxLayout(excl_group)
        excl_desc = QLabel(
            "Non vengono controllate né da questa pianificazione né dal timer di "
            "sistema (klamav-scan.timer). La cartella di quarantena è sempre esclusa."
        )
        excl_desc.setWordWrap(True)
        excl_desc.setStyleSheet("font-size: 12px; color: palette(mid);")
        e_layout.addWidget(excl_desc)

        self.excl_list = QListWidget()
        self.excl_list.setObjectName("ExcludedDirsList")
        self.excl_list.setFixedHeight(110)
        self.excl_list.setIconSize(QSize(16, 16))
        # Icone dell'esito: tema, con ripiego sulle icone standard di Qt
        # (_icon ripiegherebbe sull'icona dell'app, uno scudo con la spunta:
        # fuorviante accanto a un avviso). Le voci senza problemi hanno
        # un'icona trasparente, così le righe restano alte uguali.
        style = self.style()
        self._excl_icons = {
            "error": QIcon.fromTheme("dialog-error", style.standardIcon(QStyle.SP_MessageBoxCritical)),
            "warning": QIcon.fromTheme("dialog-warning", style.standardIcon(QStyle.SP_MessageBoxWarning)),
        }
        blank = QPixmap(16, 16)
        blank.fill(Qt.transparent)
        self._excl_icons["ok"] = QIcon(blank)
        self.excl_list.itemSelectionChanged.connect(self._update_exclusion_buttons)
        self.excl_list.itemDoubleClicked.connect(lambda _item: self._edit_exclusion())
        e_layout.addWidget(self.excl_list)

        excl_buttons = QHBoxLayout()
        add_btn = QPushButton("Aggiungi…")
        add_btn.setIcon(QIcon.fromTheme("list-add"))
        add_btn.setToolTip("Scegli una cartella con il selettore di file")
        add_btn.clicked.connect(self._add_exclusion)
        # Il selettore non basta: nasconde le cartelle nascoste (e dal venv,
        # con PySide6 da pip, può non essere quello di KDE), e non permette
        # di indicare una cartella che non esiste ancora.
        add_typed_btn = QPushButton("Aggiungi percorso…")
        add_typed_btn.setIcon(QIcon.fromTheme("edit-rename"))
        add_typed_btn.setToolTip("Scrivi il percorso: anche cartelle nascoste o non ancora esistenti")
        add_typed_btn.clicked.connect(self._add_typed_exclusion)
        self.excl_edit_btn = QPushButton("Modifica…")
        self.excl_edit_btn.setIcon(QIcon.fromTheme("document-edit"))
        self.excl_edit_btn.clicked.connect(self._edit_exclusion)
        self.excl_remove_btn = QPushButton("Rimuovi")
        self.excl_remove_btn.setIcon(QIcon.fromTheme("list-remove"))
        self.excl_remove_btn.clicked.connect(self._remove_exclusion)
        for btn in (add_btn, add_typed_btn, self.excl_edit_btn, self.excl_remove_btn):
            btn.setFixedHeight(32)
            excl_buttons.addWidget(btn)
        excl_buttons.addStretch()
        e_layout.addLayout(excl_buttons)
        layout.addWidget(excl_group)

        self.target_edit.textChanged.connect(self._refresh_exclusions)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        self.save_btn = QPushButton("Salva Pianificazione")
        self.save_btn.setObjectName("PrimaryButton")
        self.save_btn.setFixedHeight(36)
        self.save_btn.setIcon(QIcon.fromTheme("document-save"))
        self.save_btn.clicked.connect(self._save_schedule)

        buttons_row.addWidget(self.save_btn)
        layout.addStretch()
        layout.addLayout(buttons_row)

        # Stato dell'ultima esecuzione della scansione programmata: senza
        # questa label, "sta scansionando" e "il timer non è mai partito"
        # erano indistinguibili (una scansione background non ha nessuna
        # UI visibile finché non finisce — problema emerso nei test del
        # 28/08 con la scansione da 330k file invisibile per un'ora).
        self.execution_status_label = QLabel("")
        self.execution_status_label.setWordWrap(True)
        self.execution_status_label.setStyleSheet("font-size: 12px; color: palette(mid);")
        layout.addWidget(self.execution_status_label)

        self.next_run_label = QLabel("")
        self.next_run_label.setWordWrap(True)
        self.next_run_label.setStyleSheet("font-size: 12px;")
        layout.addWidget(self.next_run_label)

        self._load_settings()

    def update_progress(self, text: str) -> None:
        self.execution_status_label.setText(text)

    def showEvent(self, event) -> None:  # noqa: N802 - API Qt
        super().showEvent(event)
        self.refresh_system_timer()

    def refresh_system_timer(self) -> None:
        """Aggiorna la label sul timer di sistema. systemctl --user (fino a
        10 s di timeout) e la lettura degli override girano fuori dal thread
        della GUI; lo stato arriva in self.timer_state (None: sconosciuto,
        per esempio senza manager systemd utente)."""
        if self._timer_refresh_running:
            self._timer_refresh_again = True
            return
        self._timer_refresh_running = True
        self._timer_refresh_again = False
        try:
            tcp = load_endpoint(self.settings).is_tcp
        except ValueError:
            tcp = False

        def check():
            state = timer_enabled()
            return state, (foreign_overrides(tcp=tcp) if state is True else [])

        run_off_gui_thread(check, self._timer_state_done)

    def _timer_state_done(self, result) -> None:
        self._timer_refresh_running = False
        if self._timer_refresh_again:
            self.refresh_system_timer()
            return
        state, overrides = (None, []) if isinstance(result, Exception) else result
        self.timer_state = state
        if state is True:
            text = (
                "La scansione programmata di sistema (klamav-scan.timer) è attiva: "
                "controlla ogni giorno l'intera home, anche con KlamAV-Py chiuso. "
                "È alternativa a questa pianificazione."
            )
            if self.settings.value("schedule_enabled", False, type=bool):
                text += " Al momento sono attive entrambe: la home viene scansionata due volte."
            if overrides:
                text += "\n\nAttenzione: " + " ".join(o.describe() for o in overrides)
            self.system_timer_label.setText(text)
        self.system_timer_label.setVisible(state is True)

    def set_next_run(self, text: str) -> None:
        self.next_run_label.setText(text)

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleziona cartella da scansionare", self.target_edit.text())
        if path:
            self.target_edit.setText(path)

    # -- cartelle escluse ------------------------------------------------

    def _exclusion_roots(self) -> dict[str, Path]:
        return schedule_roots(self.target_edit.text())

    def excluded_dirs(self) -> list[str]:
        """Voci della lista, nella forma salvata."""
        return [self.excl_list.item(i).data(Qt.UserRole) for i in range(self.excl_list.count())]

    # La regola (scan_exclusions.decide) fa resolve() e stat() sulle voci e
    # sulle radici: tutte le valutazioni della pagina passano da
    # run_off_gui_thread, perché un percorso su un mount di rete
    # irraggiungibile bloccherebbe la finestra. Una sola valutazione per
    # tipo alla volta: un controllo bloccato non accumula thread a ogni
    # tasto premuto nel campo della cartella.

    def _annotate_exclusion(self, item: QListWidgetItem, decision) -> None:
        """Icona e tooltip con l'esito della regola per le radici attuali:
        una voce valida con la cartella di prima può non esserlo più."""
        if decision.error:
            item.setIcon(self._excl_icons["error"])
            item.setToolTip(decision.error)
        elif decision.warnings:
            item.setIcon(self._excl_icons["warning"])
            item.setToolTip("\n".join(decision.warnings))
        else:
            item.setIcon(self._excl_icons["ok"])
            item.setToolTip("")

    def _refresh_exclusions(self) -> None:
        if self._refresh_running:
            # Ripetuta quando finisce quella in corso, con i dati di allora.
            self._refresh_again = True
            return
        self._refresh_running = True
        self._refresh_again = False
        raws = self.excluded_dirs()
        roots = self._exclusion_roots()
        run_off_gui_thread(
            lambda: {raw: decide_exclusion(raw, roots=roots) for raw in raws},
            self._refresh_done,
        )

    def _refresh_done(self, decisions) -> None:
        self._refresh_running = False
        if self._refresh_again:
            # Cartella o lista cambiate nel frattempo: l'esito è superato.
            self._refresh_exclusions()
            return
        if isinstance(decisions, Exception):
            return
        for i in range(self.excl_list.count()):
            item = self.excl_list.item(i)
            decision = decisions.get(item.data(Qt.UserRole))
            if decision is not None:
                self._annotate_exclusion(item, decision)

    def _update_exclusion_buttons(self) -> None:
        selected = bool(self.excl_list.selectedItems())
        self.excl_edit_btn.setEnabled(selected)
        self.excl_remove_btn.setEnabled(selected)

    def _put_exclusion(self, raw: str, replace: QListWidgetItem | None = None) -> None:
        """Aggiunge (o sostituisce) una voce se la regola la accetta. Gli
        errori bloccano; gli avvisi restano visibili sulla voce. L'esito
        arriva da run_off_gui_thread: finché non arriva, un'altra aggiunta
        è ignorata."""
        if self._put_running:
            return
        self._put_running = True
        roots = self._exclusion_roots()
        run_off_gui_thread(
            lambda: decide_exclusion(raw, roots=roots),
            lambda decision: self._put_done(decision, replace),
        )

    def _put_done(self, decision, replace: QListWidgetItem | None) -> None:
        self._put_running = False
        title = "Cartelle escluse"
        if isinstance(decision, Exception):
            QMessageBox.warning(self, title, f"Impossibile verificare la cartella: {decision}")
            return
        if decision.error:
            QMessageBox.warning(self, title, decision.error)
            return
        if replace is not None and self.excl_list.row(replace) < 0:
            replace = None  # rimossa nel frattempo: diventa un'aggiunta
        for i in range(self.excl_list.count()):
            other = self.excl_list.item(i)
            if other is not replace and other.data(Qt.UserRole) == decision.stored:
                self.excl_list.setCurrentItem(other)
                return
        item = replace or QListWidgetItem()
        item.setText(decision.stored)
        item.setData(Qt.UserRole, decision.stored)
        if replace is None:
            self.excl_list.addItem(item)
        self._annotate_exclusion(item, decision)
        self.excl_list.setCurrentItem(item)

    def _add_exclusion(self) -> None:
        start = self.target_edit.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Seleziona cartella da escludere", start)
        if path:
            self._put_exclusion(path)

    def _add_typed_exclusion(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Cartelle escluse",
            "Cartella da escludere (~ indica la home; anche nascosta o non ancora esistente):",
            QLineEdit.Normal, "",
        )
        if ok and text.strip():
            self._put_exclusion(text)

    def _edit_exclusion(self) -> None:
        # Testo libero, non solo Sfoglia: permette di escludere una
        # cartella che non esiste ancora (con avviso sulla voce).
        item = self.excl_list.currentItem()
        if item is None:
            return
        text, ok = QInputDialog.getText(
            self, "Cartelle escluse", "Cartella da escludere:", QLineEdit.Normal, item.data(Qt.UserRole)
        )
        if ok:
            self._put_exclusion(text, replace=item)

    def _remove_exclusion(self) -> None:
        item = self.excl_list.currentItem()
        if item is not None:
            self.excl_list.takeItem(self.excl_list.row(item))
        self._update_exclusion_buttons()

    def _sync_system_dropin(
        self, excludes: list[str], timer_disabled: bool = False
    ) -> tuple[bool, bool] | None:
        """
        Rigenera il drop-in di klamav-scan.service con le nuove esclusioni e
        con quarantena ed endpoint già salvati nelle Impostazioni (stato
        completo, vedi render_dropin). None, con un avviso, se il
        salvataggio va annullato; altrimenti (drop-in cambiato, endpoint
        TCP), per il daemon-reload e il controllo degli override estranei
        che _save_write esegue fuori dal thread della GUI.
        """
        title = "Pianificazione Scansioni"
        try:
            endpoint = load_endpoint(self.settings)
        except ValueError as exc:
            QMessageBox.warning(
                self, title,
                f"Impostazioni di connessione a clamd non valide ({exc}): correggile "
                "nelle Impostazioni.\n\nLa pianificazione non è stata salvata.",
            )
            return None
        quarantine = Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))).expanduser()
        try:
            changed = sync_dropin(render_dropin(
                quarantine.resolve(), home=Path.home(), endpoint=endpoint, excludes=excludes,
            ))
        except (DropinConflict, OSError) as exc:
            reason = str(exc) if isinstance(exc, DropinConflict) else (
                f"Impossibile aggiornare la scansione programmata di sistema:\n{exc}"
            )
            tail = "La pianificazione non è stata salvata."
            if timer_disabled:
                tail += " Il timer di sistema era già stato disattivato."
            QMessageBox.warning(self, title, f"{reason}\n\n{tail}")
            return None
        return changed, endpoint.is_tcp


    def _load_settings(self) -> None:
        self.enable_check.setChecked(self.settings.value("schedule_enabled", False, type=bool))
        self.interval_spin.setValue(self.settings.value("schedule_interval", 24, type=int))

        unit = self.settings.value("schedule_unit", "Ore")
        idx = self.unit_combo.findText(unit)
        if idx >= 0:
            self.unit_combo.setCurrentIndex(idx)

        self.target_edit.setText(self.settings.value("schedule_target", str(Path.home())))

        self.excl_list.clear()
        for raw in load_schedule_excludes(self.settings):
            item = QListWidgetItem(raw)
            item.setData(Qt.UserRole, raw)
            self.excl_list.addItem(item)
        self._refresh_exclusions()
        self._update_exclusion_buttons()

    def _save_schedule(self) -> None:
        """
        Ordine: validazione (esclusioni, quarantena, cartella interna),
        scelta fra timer e pianificazione interna, drop-in, QSettings.
        Niente viene salvato se un passo fallisce, così GUI e timer non
        divergono.

        La validazione tocca il filesystem e gira fuori dal thread della
        GUI; il salvataggio prosegue in _save_validated con i valori letti
        qui (_ScheduleForm), non con quelli che l'utente cambia nel
        frattempo. Rileggere la casella dopo la validazione salvava la
        pianificazione interna attiva con una cartella mai validata come
        tale e senza la domanda sul timer di sistema.
        """
        if self._save_running:
            return
        self._save_running = True
        self.save_btn.setEnabled(False)
        form = _ScheduleForm(
            enabled=self.enable_check.isChecked(),
            interval=self.interval_spin.value(),
            unit=self.unit_combo.currentText(),
            target=self.target_edit.text(),
        )
        raws = self.excluded_dirs()
        roots = schedule_roots(form.target)
        quarantine_raw = self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))

        def check():
            # Lo stato del timer serve solo se si attiva la pianificazione
            # interna: systemctl --user può impiegare fino a 10 s.
            state = timer_enabled() if form.enabled else None
            return validate_schedule(raws, roots, form.target, quarantine_raw, form.enabled), state

        run_off_gui_thread(check, lambda result: self._save_validated(result, form))

    def _save_end(self) -> None:
        self._save_running = False
        self.save_btn.setEnabled(True)

    def _save_validated(self, result, form: "_ScheduleForm") -> None:
        title = "Pianificazione Scansioni"
        # Prima della domanda sul timer: un salvataggio che fallirebbe per
        # la validazione non deve aver già disattivato klamav-scan.timer.
        if isinstance(result, Exception):
            QMessageBox.warning(
                self, title,
                f"Impossibile verificare la pianificazione: {result}\n\n"
                "La pianificazione non è stata salvata.",
            )
            self._save_end()
            return
        (excludes, problems), timer_state = result
        if problems:
            QMessageBox.warning(
                self, title,
                "\n\n".join(problems) + "\n\nLa pianificazione non è stata salvata.",
            )
            self._save_end()
            return

        if not (form.enabled and timer_state is True):
            self._save_write(excludes, form, timer_disabled=False)
            return
        # Tre esiti, non due: chi apre questa pagina solo per le cartelle
        # escluse (che valgono anche per il timer) deve poter salvare
        # senza scegliere la pianificazione interna.
        answer = QMessageBox.question(
            self,
            title,
            "La scansione programmata di sistema (klamav-scan.timer) è attiva. "
            "Le due pianificazioni sono alternative: con entrambe la home verrebbe "
            "scansionata due volte.\n\n"
            "Sì: disattiva il timer di sistema e usa questa pianificazione.\n"
            "No: mantieni il timer; questa pianificazione resta disattivata.\n\n"
            "In entrambi i casi le cartelle escluse vengono salvate e valgono "
            "per tutte e due.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
            run_off_gui_thread(
                disable_timer,
                lambda problem: self._save_timer_disabled(problem, excludes, form),
            )
        elif answer == QMessageBox.No:
            self._save_write(excludes, replace(form, enabled=False), timer_disabled=False)
        else:
            self._save_end()

    def _save_timer_disabled(self, problem, excludes: list[str], form: "_ScheduleForm") -> None:
        if problem:
            QMessageBox.warning(
                self,
                "Pianificazione Scansioni",
                "Non è stato possibile disattivare klamav-scan.timer, quindi la "
                f"pianificazione non è stata salvata:\n{problem}",
            )
            self._save_end()
            return
        self._save_write(excludes, form, timer_disabled=True)

    def _save_write(self, excludes: list[str], form: "_ScheduleForm", timer_disabled: bool) -> None:
        # Il timer di sistema usa la stessa lista: drop-in prima di QSettings,
        # e se non si può scrivere non si salva NULLA (GUI e timer
        # escluderebbero cartelle diverse senza che nessuno lo sappia).
        synced = self._sync_system_dropin(excludes, timer_disabled=timer_disabled)
        if synced is None:
            self._save_end()
            return
        changed, tcp = synced

        # La casella mostra ciò che è stato salvato, anche se nel frattempo
        # l'utente l'aveva cambiata.
        self.enable_check.setChecked(form.enabled)
        self.settings.setValue("schedule_enabled", form.enabled)
        self.settings.setValue("schedule_interval", form.interval)
        self.settings.setValue("schedule_unit", form.unit)
        self.settings.setValue("schedule_target", form.target)
        self.settings.setValue(SCHEDULE_EXCLUDES_KEY, excludes)

        # Il daemon-reload fallito e gli override estranei sono solo avvisi,
        # come nelle Impostazioni: le impostazioni sono già salvate.
        run_off_gui_thread(lambda: dropin_followup(changed, tcp), self._save_done)

    def _save_done(self, result) -> None:
        title = "Pianificazione Scansioni"
        notes = (
            [f"Impossibile completare l'aggiornamento del timer di sistema: {result}"]
            if isinstance(result, Exception)
            else dropin_notes(*result, what="le nuove esclusioni")
        )
        if notes:
            QMessageBox.warning(
                self, title,
                "Pianificazione salvata, con queste note:\n\n" + "\n\n".join(notes),
            )

        self._save_end()
        self.schedule_saved.emit()
        self.refresh_system_timer()

        main_window = self.window()
        if hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            main_window.tray_icon.showMessage(
                APP_NAME, "Pianificazione salvata con successo.", _icon("emblem-checked"), 3000
            )


class SettingsPage(QWidget):
    settings_saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # QSettings sempre con org/app ESPLICITI (vedi SchedulerPage).
        self.settings = QSettings(APP_NAME, APP_NAME)
        # Avvisi non bloccanti dell'ultimo salvataggio (es. daemon-reload non
        # riuscito): li mostra MainWindow nel messaggio di conferma.
        self.save_notes: list[str] = []
        # Worker del controllo aggiornamenti: None quando nessun controllo
        # è in corso. Vedi _check_updates / _release_update_check_worker.
        self._update_check_worker: UpdateCheckWorker | None = None
        # Salvataggio in attesa del daemon-reload (vedi _save_settings).
        self._save_running = False
        self._dropin_changed = False

        # FIX SOVRAPPOSIZIONE WIDGET: il contenuto della pagina (parecchi
        # widget a dimensione fissa: QLineEdit/QPushButton alti 36px,
        # QListWidget alto 120px, testi lunghi nelle checkbox) ha una
        # dimensione minima naturale piuttosto grande. Qt normalmente non
        # permette di ridimensionare una finestra sotto questo minimo, ma
        # non tutti i window manager rispettano rigidamente questo vincolo
        # durante un ridimensionamento interattivo (trascinando il bordo):
        # se lo ignorano, la finestra può finire più piccola di quanto i
        # widget a dimensione fissa richiedano, e senza uno scroll area il
        # layout li comprime fino a farli sovrapporre invece di restringersi.
        # Mettendo tutto dentro un QScrollArea, se lo spazio disponibile è
        # insufficiente compare una scrollbar invece di una sovrapposizione:
        # non è mai "rotto", nel peggiore dei casi si scorre.
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        outer_layout.addWidget(scroll_area)

        content = QWidget()
        scroll_area.setWidget(content)

        layout = QVBoxLayout(content)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Impostazioni")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)
        layout.addSpacing(10)

        startup_group = QGroupBox("Avvio Sistema")
        startup_layout = QVBoxLayout(startup_group)

        self.autostart_check = QCheckBox("Avvia KlamAV-Py automaticamente all'avvio del sistema")
        startup_layout.addWidget(self.autostart_check)

        self.start_in_tray_check = QCheckBox("Avvia KlamAV-Py nella System Tray (minimizzato)")
        startup_layout.addWidget(self.start_in_tray_check)

        layout.addWidget(startup_group)

        general_group = QGroupBox("Generale")
        general_layout = QVBoxLayout(general_group)

        # Connessione a clamd: il trasporto è una scelta esplicita (mai
        # dedotto da "host valorizzato"), e con TCP la label dice nel punto
        # dove si configura che i file viaggiano in chiaro, come "Real-Time
        # parziale" per le sue limitazioni.
        conn_layout = QHBoxLayout()
        conn_label = QLabel("Connessione a clamd:")
        conn_label.setFixedWidth(130)
        self.transport_combo = QComboBox()
        self.transport_combo.addItem("Socket Unix", "unix")
        self.transport_combo.addItem("TCP", "tcp")
        self.transport_combo.setFixedHeight(36)
        conn_layout.addWidget(conn_label)
        conn_layout.addWidget(self.transport_combo)
        conn_layout.addStretch()
        general_layout.addLayout(conn_layout)

        self.socket_row = QWidget()
        socket_layout = QHBoxLayout(self.socket_row)
        socket_layout.setContentsMargins(0, 0, 0, 0)
        socket_label = QLabel("Socket clamd:")
        socket_label.setFixedWidth(130)
        self.socket_edit = QLineEdit()
        self.socket_edit.setFixedHeight(36)
        socket_browse = QPushButton("Sfoglia…")
        socket_browse.setFixedHeight(36)
        socket_browse.setIcon(QIcon.fromTheme("document-open"))
        socket_browse.clicked.connect(lambda: self._browse_file(self.socket_edit))

        socket_layout.addWidget(socket_label)
        socket_layout.addWidget(self.socket_edit)
        socket_layout.addWidget(socket_browse)
        general_layout.addWidget(self.socket_row)

        self.tcp_row = QWidget()
        tcp_layout = QHBoxLayout(self.tcp_row)
        tcp_layout.setContentsMargins(0, 0, 0, 0)
        tcp_label = QLabel("Host e porta:")
        tcp_label.setFixedWidth(130)
        self.tcp_host_edit = QLineEdit()
        self.tcp_host_edit.setFixedHeight(36)
        self.tcp_host_edit.setPlaceholderText("es. 127.0.0.1, ::1 o clamd.lan")
        self.tcp_port_spin = QSpinBox()
        self.tcp_port_spin.setRange(1, 65535)
        self.tcp_port_spin.setValue(DEFAULT_TCP_PORT)
        self.tcp_port_spin.setFixedHeight(36)
        tcp_layout.addWidget(tcp_label)
        tcp_layout.addWidget(self.tcp_host_edit, 1)
        tcp_layout.addWidget(self.tcp_port_spin)
        general_layout.addWidget(self.tcp_row)

        self.tcp_warning_label = QLabel("")
        self.tcp_warning_label.setWordWrap(True)
        self.tcp_warning_label.setStyleSheet("font-size: 12px; color: palette(highlight);")
        general_layout.addWidget(self.tcp_warning_label)

        self.transport_combo.currentIndexChanged.connect(self._update_transport_rows)
        self.tcp_host_edit.textChanged.connect(self._update_transport_rows)
        self.tcp_port_spin.valueChanged.connect(self._update_transport_rows)

        quar_layout = QHBoxLayout()
        quar_label = QLabel("Cartella quarantena:")
        quar_label.setFixedWidth(130)
        self.quar_edit = QLineEdit()
        self.quar_edit.setFixedHeight(36)
        quar_browse = QPushButton("Sfoglia…")
        quar_browse.setFixedHeight(36)
        quar_browse.setIcon(QIcon.fromTheme("document-open"))
        quar_browse.clicked.connect(lambda: self._browse_dir(self.quar_edit))

        quar_layout.addWidget(quar_label)
        quar_layout.addWidget(self.quar_edit)
        quar_layout.addWidget(quar_browse)
        general_layout.addLayout(quar_layout)

        self.auto_quar_check = QCheckBox("Metti in quarantena automaticamente i file infetti (default: disattivato)")
        general_layout.addWidget(self.auto_quar_check)

        self.startup_update_check = QCheckBox("Aggiorna il database dei virus all'avvio se le firme hanno più di 36 ore")
        general_layout.addWidget(self.startup_update_check)

        self.auto_check_updates = QCheckBox("Controlla aggiornamenti all'avvio")
        general_layout.addWidget(self.auto_check_updates)

        layout.addWidget(general_group)

        rt_group = QGroupBox("Protezione Real-Time")
        rt_layout = QVBoxLayout(rt_group)

        rt_desc = QLabel("Cartelle monitorate per il controllo in tempo reale:")
        rt_desc.setWordWrap(True)
        rt_layout.addWidget(rt_desc)

        self.rt_dirs_list = QListWidget()
        self.rt_dirs_list.setObjectName("MonitoredDirsList")
        self.rt_dirs_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.rt_dirs_list.setFixedHeight(120)
        rt_layout.addWidget(self.rt_dirs_list)

        rt_btns_row = QHBoxLayout()
        rt_add_btn = QPushButton("Aggiungi Cartella")
        rt_add_btn.setFixedHeight(36)
        rt_add_btn.setIcon(QIcon.fromTheme("list-add"))
        rt_add_btn.clicked.connect(self._add_rt_dir)

        rt_rm_btn = QPushButton("Rimuovi Selezionate")
        rt_rm_btn.setFixedHeight(36)
        rt_rm_btn.setIcon(QIcon.fromTheme("list-remove"))
        rt_rm_btn.clicked.connect(self._remove_rt_dir)

        rt_btns_row.addWidget(rt_add_btn)
        rt_btns_row.addWidget(rt_rm_btn)
        rt_btns_row.addStretch()
        rt_layout.addLayout(rt_btns_row)

        # Punto 4 dell'analisi: addPath()/addPaths() possono fallire in
        # silenzio (es. limite fs.inotify.max_user_watches esaurito) e,
        # senza questa label, il Real-Time smetterebbe di coprire una
        # cartella senza che l'utente se ne accorga mai. Aggiornata da
        # MainWindow._update_realtime_status_label().
        self.rt_status_label = QLabel("")
        self.rt_status_label.setWordWrap(True)
        self.rt_status_label.setStyleSheet("font-size: 12px;")
        rt_layout.addWidget(self.rt_status_label)

        layout.addWidget(rt_group)

        dolphin_group = QGroupBox("Integrazione File Manager (Dolphin)")
        dolphin_layout = QVBoxLayout(dolphin_group)
        dolphin_desc = QLabel("Aggiunge la voce \"Scansiona con KlamAV-Py\" al menu del tasto destro su file e cartelle.")
        dolphin_desc.setWordWrap(True)
        dolphin_layout.addWidget(dolphin_desc)

        dolphin_btns_row = QHBoxLayout()
        self.install_dolphin_btn = QPushButton("Installa integrazione")
        self.install_dolphin_btn.setFixedHeight(36)
        self.install_dolphin_btn.setIcon(QIcon.fromTheme("system-installer"))
        self.install_dolphin_btn.clicked.connect(self._install_dolphin)

        self.remove_dolphin_btn = QPushButton("Rimuovi integrazione")
        self.remove_dolphin_btn.setFixedHeight(36)
        self.remove_dolphin_btn.setIcon(QIcon.fromTheme("edit-delete"))
        self.remove_dolphin_btn.clicked.connect(self._remove_dolphin)

        dolphin_btns_row.addWidget(self.install_dolphin_btn)
        dolphin_btns_row.addWidget(self.remove_dolphin_btn)
        dolphin_btns_row.addStretch()
        dolphin_layout.addLayout(dolphin_btns_row)

        layout.addWidget(dolphin_group)

        about_group = QGroupBox("Informazioni")
        about_layout = QVBoxLayout(about_group)
        self.version_label = QLabel(f"Versione corrente: {__version__}")
        self.version_label.setStyleSheet("font-size: 12px; color: palette(mid);")
        about_layout.addWidget(self.version_label)

        self.db_status_label = QLabel("Database firme: verifica in corso…")
        self.db_status_label.setTextFormat(Qt.PlainText)
        self.db_status_label.setWordWrap(True)
        self.db_status_label.setStyleSheet("font-size: 12px;")
        about_layout.addWidget(self.db_status_label)

        self.update_status_label = QLabel("")
        self.update_status_label.setTextFormat(Qt.PlainText)
        self.update_status_label.setWordWrap(True)
        self.update_status_label.setStyleSheet("font-size: 12px;")
        about_layout.addWidget(self.update_status_label)

        self.check_update_btn = QPushButton("Controlla aggiornamenti")
        self.check_update_btn.setFixedHeight(36)
        self.check_update_btn.setIcon(QIcon.fromTheme("system-software-update"))
        self.check_update_btn.clicked.connect(self._check_updates)
        about_layout.addWidget(self.check_update_btn)
        layout.addWidget(about_group)

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        reset_btn = QPushButton("Ripristina Default")
        reset_btn.setFixedHeight(36)
        reset_btn.setIcon(QIcon.fromTheme("edit-undo"))
        reset_btn.clicked.connect(self._reset_defaults)

        save_btn = QPushButton("Salva Impostazioni")
        save_btn.setObjectName("PrimaryButton")
        save_btn.setFixedHeight(36)
        save_btn.setIcon(QIcon.fromTheme("document-save"))
        save_btn.clicked.connect(self._save_settings)
        self.save_btn = save_btn

        buttons_row.addWidget(reset_btn)
        buttons_row.addWidget(save_btn)

        layout.addStretch()
        layout.addLayout(buttons_row)

        self._load_settings()

    def _update_transport_rows(self, *_args) -> None:
        tcp = self.transport_combo.currentData() == "tcp"
        self.socket_row.setVisible(not tcp)
        self.tcp_row.setVisible(tcp)
        self.tcp_warning_label.setVisible(tcp)
        if tcp:
            host = self.tcp_host_edit.text().strip() or "host"
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            self.tcp_warning_label.setText(
                f"I file scansionati vengono inviati in chiaro a {host}:{self.tcp_port_spin.value()}: "
                "usa TCP solo verso localhost o reti fidate. Anche la scansione "
                "programmata di sistema userà la rete per raggiungere clamd."
            )

    def _endpoint_from_form(self) -> ClamdEndpoint:
        """Endpoint dai campi; ValueError con il motivo se non è valido."""
        host = self.tcp_host_edit.text().strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]  # "[::1]" come negli URL: le parentesi non servono qui
        return ClamdEndpoint(
            transport=self.transport_combo.currentData(),
            unix_socket=self.socket_edit.text().strip(),
            tcp_host=host,
            tcp_port=self.tcp_port_spin.value(),
        )

    def _set_endpoint_fields(self, endpoint: ClamdEndpoint) -> None:
        self.transport_combo.setCurrentIndex(self.transport_combo.findData(endpoint.transport))
        self.socket_edit.setText(endpoint.unix_socket)
        self.tcp_host_edit.setText(endpoint.tcp_host)
        self.tcp_port_spin.setValue(endpoint.tcp_port)
        self._update_transport_rows()

    def _set_raw_endpoint_fields(self) -> None:
        index = self.transport_combo.findData(str(self.settings.value("clamd_transport", "unix")))
        self.transport_combo.setCurrentIndex(max(index, 0))
        self.socket_edit.setText(str(self.settings.value("socket_path", DEFAULT_SOCKET)))
        self.tcp_host_edit.setText(str(self.settings.value("tcp_host", "") or ""))
        try:
            self.tcp_port_spin.setValue(int(self.settings.value("tcp_port", DEFAULT_TCP_PORT)))
        except (TypeError, ValueError):
            self.tcp_port_spin.setValue(DEFAULT_TCP_PORT)
        self._update_transport_rows()

    def _browse_file(self, line_edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Seleziona il socket di clamd", "/run/clamav/", "Tutti i file (*)")
        if path: line_edit.setText(path)

    def _browse_dir(self, line_edit: QLineEdit) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleziona cartella", line_edit.text() or str(Path.home()))
        if path: line_edit.setText(path)

    def _add_rt_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleziona cartella da monitorare", str(Path.home()))
        if path:
            existing = [self.rt_dirs_list.item(i).text() for i in range(self.rt_dirs_list.count())]
            if path not in existing: self.rt_dirs_list.addItem(path)

    def _remove_rt_dir(self) -> None:
        for item in self.rt_dirs_list.selectedItems(): self.rt_dirs_list.takeItem(self.rt_dirs_list.row(item))

    def set_db_info(self, info: DbInfo | None) -> None:
        self.db_status_label.setText(describe(info))

    def _load_settings(self) -> None:
        self.autostart_check.setChecked(self.settings.value("autostart_system", False, type=bool))
        self.start_in_tray_check.setChecked(self.settings.value("start_in_tray", False, type=bool))
        try:
            self._set_endpoint_fields(load_endpoint(self.settings))
        except ValueError:
            # Valori salvati non validi (es. "127.0.0.1:3310" come host,
            # accettato dalle versioni precedenti a questo controllo): si
            # mostrano COSÌ COME SONO, perché l'utente veda cosa correggere.
            # Proporre il predefinito li nasconderebbe. Il salvataggio li
            # rivaliderà.
            self._set_raw_endpoint_fields()
        self.quar_edit.setText(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR)))
        self.auto_quar_check.setChecked(self.settings.value("auto_quarantine", False, type=bool))
        self.startup_update_check.setChecked(self.settings.value("startup_update", True, type=bool))
        self.auto_check_updates.setChecked(self.settings.value("auto_check_updates", False, type=bool))

        rt_dirs = self.settings.value("realtime_paths", [])
        if isinstance(rt_dirs, str): rt_dirs = [rt_dirs]
        self.rt_dirs_list.addItems(rt_dirs)

    def _reset_defaults(self) -> None:
        self.autostart_check.setChecked(False)
        self.start_in_tray_check.setChecked(False)
        self._set_endpoint_fields(ClamdEndpoint())
        self.quar_edit.setText(str(DEFAULT_QUARANTINE_DIR))
        self.auto_quar_check.setChecked(False)
        self.startup_update_check.setChecked(True)
        self.auto_check_updates.setChecked(False)
        self.rt_dirs_list.clear()

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(
            self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ) == QMessageBox.Yes

    def _prepare_quarantine_dir(self, endpoint: ClamdEndpoint) -> Path | None:
        """
        Valida la directory di quarantena e allinea la scansione programmata
        di sistema (drop-in di klamav-scan.service) a quarantena ed endpoint
        di clamd. Ritorna il percorso risolto da salvare, o None se il
        salvataggio va annullato.

        Ordine voluto: validazione, conferme, directory, drop-in; QSettings
        solo dopo, nel chiamante. Se il drop-in non si può scrivere le
        Impostazioni NON vengono salvate: GUI e timer userebbero due
        quarantene diverse senza che nessuno lo sappia. Il daemon-reload
        invece è solo un avviso: il suo fallimento è un ritardo, non una
        divergenza.
        """
        title = "Cartella quarantena"
        # Anche la cartella della pianificazione interna: una quarantena che
        # la contiene la escluderebbe per intero dalla scansione programmata.
        decision = decide_quarantine_dir(
            self.quar_edit.text(),
            roots=schedule_roots(self.settings.value("schedule_target", str(Path.home()))),
        )
        problem = decision.error or decision.volatile
        if problem:
            QMessageBox.warning(self, title, problem)
            return None
        path = decision.path

        for warning in decision.warnings:
            if not self._confirm(title, f"{warning}\n\nUsarla comunque?"):
                return None
        if decision.loose_mode is not None and not self._confirm(
            title,
            f"«{path}» ha permessi {decision.loose_mode:o}: come cartella di "
            "quarantena diventerà accessibile solo a te (700). Continuare?",
        ):
            return None

        old = Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))).expanduser()
        if path != old.resolve():
            count = _quarantine_count(old)
            if count and not self._confirm(
                title,
                f"La quarantena attuale in «{old}» contiene {count} file. Restano "
                "lì, ma KlamAV-Py non li mostrerà più finché non torni a quella "
                "cartella. Continuare?",
            ):
                return None

        try:
            ensure_private_dir(path)
        except OSError as exc:  # PermissionError da mkdir, PrivateFileError
            QMessageBox.warning(self, title, f"Impossibile preparare «{path}»:\n{exc}")
            return None

        try:
            # Stato completo: anche le cartelle escluse della Pianificazione,
            # altrimenti questo salvataggio le toglierebbe dal timer.
            changed = sync_dropin(render_dropin(
                path, home=Path.home(), endpoint=endpoint,
                excludes=load_schedule_excludes(self.settings),
            ))
        except DropinConflict as exc:
            QMessageBox.warning(self, title, f"{exc}\n\nLe impostazioni non sono state salvate.")
            return None
        except OSError as exc:
            QMessageBox.warning(
                self,
                title,
                "Impossibile aggiornare la scansione programmata di sistema:\n"
                f"{exc}\n\nLe impostazioni non sono state salvate.",
            )
            return None

        # daemon-reload e override estranei (es. override.conf di
        # "systemctl --user edit"): solo avvisi, verificati da
        # _save_settings fuori dal thread della GUI dopo il salvataggio.
        self._dropin_changed = changed
        self.quar_edit.setText(str(path))
        return path

    def _save_settings(self) -> None:
        if self._save_running:
            return  # verifiche del salvataggio precedente ancora in corso
        self.save_notes = []
        try:
            endpoint = self._endpoint_from_form()
        except ValueError as exc:
            QMessageBox.warning(self, "Connessione a clamd", f"Impostazione non valida: {exc}")
            return
        quarantine_dir = self._prepare_quarantine_dir(endpoint)
        if quarantine_dir is None:
            return

        self.settings.setValue("autostart_system", self.autostart_check.isChecked())
        self.settings.setValue("start_in_tray", self.start_in_tray_check.isChecked())
        save_endpoint(self.settings, endpoint)
        self.settings.setValue("quarantine_dir", str(quarantine_dir))
        self.settings.setValue("auto_quarantine", self.auto_quar_check.isChecked())
        self.settings.setValue("startup_update", self.startup_update_check.isChecked())
        self.settings.setValue("auto_check_updates", self.auto_check_updates.isChecked())

        rt_dirs = [self.rt_dirs_list.item(i).text() for i in range(self.rt_dirs_list.count())]
        self.settings.setValue("realtime_paths", rt_dirs)

        # systemctl --user daemon-reload (timeout 10 s) fuori dal thread della
        # GUI: le impostazioni sono già salvate, l'esito dà solo avvisi.
        self._save_running = True
        self.save_btn.setEnabled(False)
        changed, tcp = self._dropin_changed, endpoint.is_tcp
        run_off_gui_thread(lambda: dropin_followup(changed, tcp), self._save_settings_done)

    def _save_settings_done(self, result) -> None:
        self._save_running = False
        self.save_btn.setEnabled(True)
        if isinstance(result, Exception):
            self.save_notes.append(
                f"Impossibile completare l'aggiornamento del timer di sistema: {result}"
            )
        else:
            self.save_notes.extend(dropin_notes(*result, what="le nuove impostazioni"))

        self.settings_saved.emit()

        main_window = self.window()
        if hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            main_window.tray_icon.showMessage(
                APP_NAME, "Impostazioni salvate con successo.", _icon("emblem-checked"), 3000
            )

    def _check_updates_automatico(self) -> None:
        """
        Controllo all'avvio: salta se ne è stato fatto uno da poco (vedi
        _UPDATE_CHECK_MIN_INTERVAL_SECONDS). Punto di ingresso separato
        dal pulsante proprio perché il limite non deve valere per quello.
        """
        ultimo = self.settings.value(_LAST_UPDATE_CHECK_KEY, 0.0, type=float)
        if not _controllo_aggiornamenti_dovuto(ultimo, time.time()):
            return
        self._check_updates()

    def _check_updates(self) -> None:
        # Due punti di ingresso (pulsante + QTimer all'avvio): senza questa
        # guardia un secondo worker sovrascriverebbe il riferimento al primo
        # ancora in esecuzione.
        if self._update_check_worker is not None and self._update_check_worker.isRunning():
            return

        self.check_update_btn.setEnabled(False)
        self.check_update_btn.setText("Controllo in corso…")
        self.update_status_label.setText("")
        self.update_status_label.setToolTip("")
        self.update_status_label.setStyleSheet("font-size: 12px; color: palette(mid);")

        # Niente parent Qt: il rilascio passa da _retire_qthread come per
        # tutti gli altri worker, così alla distruzione della pagina Qt non
        # distrugge un QThread ancora in esecuzione (timeout di rete 15 s).
        worker = UpdateCheckWorker(__version__)
        self._update_check_worker = worker
        worker.check_finished.connect(self._on_update_check_finished)
        worker.error.connect(self._on_update_check_error)
        worker.start()

    def _release_update_check_worker(self) -> None:
        worker, self._update_check_worker = self._update_check_worker, None
        if worker is not None:
            _retire_qthread(worker)

    def _on_update_check_finished(self, info: UpdateInfo) -> None:
        self._release_update_check_worker()
        # Registrato solo sul buon esito: un controllo fallito (rete
        # assente) non deve consumare la finestra delle sei ore.
        self.settings.setValue(_LAST_UPDATE_CHECK_KEY, time.time())
        self.check_update_btn.setEnabled(True)
        self.check_update_btn.setText("Controlla aggiornamenti")

        if info.has_update:
            self.version_label.setText(
                f"Versione corrente: {info.current_version}  →  Disponibile: {info.latest_version}"
            )
            self.version_label.setStyleSheet("font-size: 12px; font-weight: bold; color: #d32f2f;")

            # I dati arrivano dalla rete: escape sempre, e link cliccabile
            # solo se punta davvero alle release del repository.
            version_html = html.escape(info.latest_version)
            date_suffix = f" rilasciata il {info.published_at}" if info.published_at else ""
            date_html = html.escape(date_suffix)
            if info.release_url_trusted:
                self.update_status_label.setTextFormat(Qt.RichText)
                self.update_status_label.setOpenExternalLinks(True)
                self.update_status_label.setText(
                    f"Nuova versione <a href='{html.escape(info.release_url, quote=True)}'>"
                    f"{version_html}</a>{date_html}."
                )
            else:
                self.update_status_label.setTextFormat(Qt.PlainText)
                self.update_status_label.setOpenExternalLinks(False)
                self.update_status_label.setText(
                    f"Nuova versione {info.latest_version}{date_suffix}."
                )
            self.update_status_label.setStyleSheet("font-size: 12px; color: #d32f2f;")

            notes = info.release_notes
            notes_preview = notes[:300] + ("…" if len(notes) > 300 else "")
            # I tooltip Qt diventano rich text se il contenuto "sembra" HTML:
            # escape + <pre> per mostrarlo sempre come testo.
            self.update_status_label.setToolTip(
                f"<b>Note di rilascio:</b><pre>{html.escape(notes_preview)}</pre>"
            )

            main_window = self.window()
            if hasattr(main_window, "tray_icon") and main_window.tray_icon.isVisible():
                main_window.tray_icon.showMessage(
                    APP_NAME,
                    f"È disponibile la versione {info.latest_version}.",
                    _icon("system-software-update"),
                    5000,
                )
        else:
            self.version_label.setText(f"Versione corrente: {info.current_version} (aggiornata)")
            self.version_label.setStyleSheet("font-size: 12px; color: palette(mid);")
            self.update_status_label.setTextFormat(Qt.PlainText)
            self.update_status_label.setText("Hai già l'ultima versione.")
            self.update_status_label.setToolTip("")
            self.update_status_label.setStyleSheet("font-size: 12px; color: palette(mid);")

    def _on_update_check_error(self, message: str) -> None:
        self._release_update_check_worker()
        self.check_update_btn.setEnabled(True)
        self.check_update_btn.setText("Controlla aggiornamenti")
        self.update_status_label.setTextFormat(Qt.PlainText)
        self.update_status_label.setText(f"Errore: {message}")
        self.update_status_label.setToolTip("")
        self.update_status_label.setStyleSheet("font-size: 12px; color: #d32f2f;")

    def _install_dolphin(self):
        try:
            dirs = [
                Path.home() / ".local/share/kservices5/ServiceMenus",
                Path.home() / ".local/share/kio/servicemenus"
            ]
            file_name = "klamav_scan.desktop"
            exec_cmd = _gui_relaunch_command()

            content = f"""[Desktop Entry]
Type=Service
Actions=scanWithKlamAV
Encoding=UTF-8
MimeType=all/all;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenuPlugin
X-KDE-Priority=TopLevel

[Desktop Action scanWithKlamAV]
Name=Scansiona con KlamAV-Py
Icon=edit-find
Exec={exec_cmd} --scan-target %F
"""
            for d in dirs:
                d.mkdir(parents=True, exist_ok=True)
                file_path = d / file_name
                file_path.write_text(content)
                # 0o755, NON 0o644. Da KFrameworks 5.85 i servicemenu di
                # KIO devono avere il bit di esecuzione: senza, Dolphin
                # li rifiuta con "Non sei autorizzato ad eseguire questo
                # file" e la voce di menu non funziona.
                #
                # Regola OPPOSTA a quella del launcher installato in
                # /usr/share/applications (debian/klamav-py.desktop), che
                # resta 0o644 perché lì il bit di esecuzione non serve e
                # lo spec freedesktop non lo vuole. Le due destinazioni
                # hanno requisiti diversi: 0o644 qui è la regressione
                # introdotta in 0.1.4-1 applicando la regola del
                # launcher al servicemenu.
                os.chmod(file_path, 0o755)

            _rebuild_kde_service_cache()

            QMessageBox.information(self, "Integrazione Dolphin", "Integrazione installata con successo!\n\nChiudi tutte le finestre di Dolphin e riaprile per vedere la voce nel menu.")

        except PermissionError:
            QMessageBox.critical(self, "Errore Permessi",
                "Permesso negato durante la scrittura del file di integrazione.\n\n"
                "Questo succede se hai eseguito l'app con 'sudo' in passato.\n"
                "Per risolvere, apri il terminale ed esegui:\n"
                "sudo chown -R $USER:$USER ~/.local/share/kio ~/.local/share/kservices5"
            )
        except Exception as e:
            QMessageBox.critical(self, "Errore Installazione", f"Si è verificato un errore:\n{str(e)}")

    def _remove_dolphin(self):
        try:
            dirs = [
                Path.home() / ".local/share/kservices5/ServiceMenus",
                Path.home() / ".local/share/kio/servicemenus"
            ]
            file_name = "klamav_scan.desktop"

            for d in dirs:
                f = d / file_name
                if f.exists():
                    f.unlink()

            _rebuild_kde_service_cache()

            QMessageBox.information(self, "Integrazione Dolphin", "Integrazione rimossa con successo.")

        except Exception as e:
            QMessageBox.critical(self, "Errore Rimozione", f"Si è verificato un errore:\n{str(e)}")


class MainWindow(QMainWindow):
    def __init__(self, endpoint: ClamdEndpoint | None = None, quarantine_dir: Path = DEFAULT_QUARANTINE_DIR, scan_target: Path = None) -> None:
        super().__init__()

        # Migrazione one-shot delle impostazioni (vedi docstring della
        # funzione): DEVE precedere la creazione delle pagine, che
        # costruiscono i loro QSettings espliciti e leggerebbero
        # altrimenti il file nuovo ancora vuoto.
        _migrate_legacy_settings()

        # Titolo con versione: è il punto principale in cui la versione
        # è visibile (l'altra occorrenza è il tooltip di riposo della
        # tray e la label in Impostazioni).
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(900, 600)
        self.setMinimumSize(700, 400) # Impedisce di restringere troppo la finestra
        self.setWindowIcon(_app_icon())

        self.setStyleSheet(KDE_STYLESHEET)

        # NOTA: setOrganizationName/setApplicationName NON sono qui —
        # sono responsabilità di app.py (chiamati una volta sola alla
        # creazione di QApplication; prima erano duplicati in due file).
        # Tutti i QSettings di questo modulo sono espliciti, quindi non
        # dipendono da questi valori.
        self.settings = QSettings(APP_NAME, APP_NAME)

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(_app_icon())
        self.tray_icon.setToolTip(_default_tray_tooltip())

        tray_menu = QMenu(self)
        show_action = QAction("Mostra finestra", self)
        show_action.triggered.connect(self._restore_from_tray)
        quit_action = QAction("Esci", self)
        quit_action.triggered.connect(self._request_quit)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        # --socket/--tcp dell'entry point: valori iniziali per le chiavi
        # assenti dalle Impostazioni, come prima --socket.
        self._default_endpoint = endpoint or ClamdEndpoint()
        self._endpoint_problem_noted = False
        saved_quar_dir = Path(self.settings.value("quarantine_dir", str(quarantine_dir)))
        quarantine = Quarantine(saved_quar_dir)

        self.history_manager = HistoryManager()

        # FIX RIDIMENSIONAMENTO: Aggiungiamo uno splitter con policy espandibile
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        self.sidebar.setMinimumWidth(150)
        self.sidebar.setMaximumWidth(350) # Permette di allargarla ma non troppo
        self.sidebar.setIconSize(QSize(20, 20))
        self.sidebar.setUniformItemSizes(True)
        self.sidebar.setSpacing(2)
        self.sidebar.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.sidebar.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        self._add_sidebar_item("Scansione", "edit-find", "document-search")
        self._add_sidebar_item("Cronologia", "view-history", "document-open-recent")
        self._add_sidebar_item("Quarantena", "emblem-virus", "emblem-lock", "user-trash", "edit-delete")
        self._add_sidebar_item("Segnalazioni", "dialog-warning", "emblem-important")
        self._add_sidebar_item("Aggiornamenti", "system-software-update", "view-refresh")
        self._add_sidebar_item("Real-Time", "view-history", "chronometer")
        self._add_sidebar_item("Pianificazione", "view-time-schedule", "task-recurring")
        self._add_sidebar_item("Impostazioni", "configure", "preferences-system")

        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self._change_page)

        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.scan_page = ScanPage(self._clamd_endpoint(), quarantine, self.history_manager)
        self.history_page = HistoryPage(self.history_manager)
        self.quarantine_page = QuarantinePage(quarantine)
        self.reports_page = ReportsPage()
        self.update_page = UpdatePage(self._clamd_endpoint)
        self.realtime_page = RealTimePage()
        self.scheduler_page = SchedulerPage()
        self.settings_page = SettingsPage()

        self.settings_page.settings_saved.connect(self._on_settings_saved)
        self.update_page.db_info_changed.connect(self._apply_db_info)
        self.scheduler_page.schedule_saved.connect(self._on_schedule_saved)

        self.content_stack.addWidget(self.scan_page)
        self.content_stack.addWidget(self.history_page)
        self.content_stack.addWidget(self.quarantine_page)
        self.content_stack.addWidget(self.reports_page)
        self.content_stack.addWidget(self.update_page)
        self.content_stack.addWidget(self.realtime_page)
        self.content_stack.addWidget(self.scheduler_page)
        self.content_stack.addWidget(self.settings_page)

        splitter.addWidget(self.sidebar)
        splitter.addWidget(self.content_stack)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 680])

        self.setCentralWidget(splitter)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # Non più un timer a intervallo: un controllo ogni minuto della
        # scadenza "ultima esecuzione + intervallo" (vedi schedule.py).
        self.schedule_timer = QTimer(self)
        self.schedule_timer.setInterval(SCHEDULE_CHECK_MS)
        self.schedule_timer.timeout.connect(self._check_schedule)
        self._schedule_grace_until = time.monotonic() + SCHEDULE_STARTUP_GRACE_S
        self._schedule_late_noted = False
        self._schedule_skip_noted = False
        # Scansione programmata non completata (ScanWorker.aborted):
        # notifica e cronologia una volta sola finché una scansione non
        # arriva alla fine, non a ogni nuovo tentativo al minuto.
        self._schedule_aborted_noted = False
        self._bg_aborted: str | None = None
        # Segnalazioni «solo segnalazione» nuove della scansione programmata
        # in corso: la notifica finale rimanda alla pagina Segnalazioni.
        self._bg_new_reports = 0
        # Messaggi di error della scansione programmata in corso (escluso il
        # motivo di aborted): scritti nel log, contati nella notifica.
        self._bg_errors = 0
        self._double_schedule_noted = False
        self._double_schedule_checking = False
        self.bg_worker = None
        # Log della scansione programmata scritto man mano (vedi
        # _bg_log_write): memoria costante e niente log perso se la GUI
        # si chiude a metà scansione.
        self._bg_log_fh = None
        self._bg_log_path: Path | None = None
        self._load_schedule()

        self.fs_watcher = QFileSystemWatcher()
        self.fs_watcher.directoryChanged.connect(self._on_dir_changed)
        # Debounce con UN solo timer: prima ogni file modificato creava il
        # proprio QTimer (decine di migliaia durante un git clone).
        # percorso -> istante (monotono) in cui accodarlo.
        self._pending_realtime_scans: dict[str, float] = {}
        self._realtime_debounce_timer = QTimer(self)
        self._realtime_debounce_timer.setInterval(REALTIME_DEBOUNCE_TICK_MS)
        self._realtime_debounce_timer.timeout.connect(self._flush_pending_realtime)
        self._dir_snapshots = {}
        self._realtime_queue: deque[str] = deque()
        self._realtime_queued: set[str] = set()
        self._realtime_dropped = 0
        self._realtime_overflow_notified = False
        # Distinzione importante: _realtime_roots sono le cartelle che
        # l'utente ha configurato, _realtime_configured_paths è la loro
        # espansione ricorsiva (le sottocartelle effettivamente passate
        # a QFileSystemWatcher). La riconciliazione periodica lavora
        # sulla seconda; la label di stato mostra entrambe, perché
        # "attivo su 847/2000 cartelle" quando l'utente ne ha
        # configurate 3 sarebbe più confondente che informativo.
        self._realtime_roots: list[str] = []
        self._realtime_configured_paths: list[str] = []
        self._realtime_watch_failures: list[str] = []
        self._realtime_watch_truncated = False
        self.realtime_worker = None
        self._current_realtime_target = ""
        self._load_realtime()

        # Riconciliazione periodica (punto 4 dell'analisi): se una
        # cartella monitorata viene eliminata e ricreata (es. pulizia
        # cache di un browser), il watch inotify sottostante muore e
        # QFileSystemWatcher non lo segnala né lo ripristina da solo —
        # resterebbe "cieco" su quella cartella finché non si riavvia
        # l'app. Ogni 60s confrontiamo le cartelle effettivamente
        # osservate con quelle configurate e ri-aggiungiamo le mancanti.
        self.realtime_reconcile_timer = QTimer(self)
        self.realtime_reconcile_timer.timeout.connect(self._reconcile_realtime_watches)
        self.realtime_reconcile_timer.start(60_000)

        # Chiusura ordinata dei worker: vedi _shutdown_workers. Agganciata
        # qui, prima che parta qualunque worker (il primo è il ping).
        self._quit_after_update = False
        QApplication.instance().aboutToQuit.connect(self._shutdown_workers)

        # startup_update è condizionato alla freschezza del DB: la decisione
        # arriva in _on_db_info, dopo ping e VERSION. Con clamd giù non si
        # propone nessun prompt pkexec (vedi should_update_on_startup).
        self._startup_update_pending = self.settings.value("startup_update", True, type=bool)
        self._ping_worker: PingWorker | None = None
        self.clamd_health = ClamdHealth()
        self._forced_ping = Throttle(CLAMD_FORCED_PING_MIN_INTERVAL_S)
        # Controllo periodico: senza, clamd che muore a sessione aperta
        # farebbe fallire il Real-Time file per file senza mai dire che
        # l'antivirus è fermo.
        self.clamd_health_timer = QTimer(self)
        self.clamd_health_timer.timeout.connect(self._periodic_clamd_check)
        self.clamd_health_timer.start(CLAMD_HEALTH_INTERVAL_MS)
        self._db_info_worker: DbInfoWorker | None = None
        self._check_clamd(self._clamd_endpoint())

        if self.settings.value("auto_check_updates", False, type=bool):
            QTimer.singleShot(3000, self.settings_page._check_updates_automatico)

        # Se avviata con un target (es. da Dolphin in prima istanza), avvia la scansione
        if scan_target:
            self.scan_page.start_external_scan(scan_target)

    def _reset_tray_tooltip(self) -> None:
        """Riporta il tooltip della tray al riposo: chiamato a fine di
        ogni attività che lo ha modificato (scansione manuale,
        programmata, pausa).

        Con clamd fermo il riposo NON è "Protezione attiva": il tooltip
        dichiara lo stato reale."""
        if getattr(self, "clamd_health", None) is not None and self.clamd_health.is_down:
            self.tray_icon.setToolTip(
                f"{APP_NAME} {__version__} — clamd non risponde: protezione NON attiva"
            )
            return
        self.tray_icon.setToolTip(_default_tray_tooltip())

    def _request_quit(self) -> None:
        """
        Uscita dal menu della tray.

        Se è in corso l'aggiornamento del database l'uscita viene
        RINVIATA alla sua fine. Non è più una questione di integrità: il
        download lo fa l'unità systemd di freshclam, indipendente da questo
        processo, e il worker rispetta requestInterruption(). Il rinvio
        serve a non chiudere il dialogo pkexec sotto le mani dell'utente e
        a mostrargli l'esito.
        """
        worker = self.update_page.worker
        if worker is not None and worker.isRunning():
            if not self._quit_after_update:
                self._quit_after_update = True
                # Connessa dopo UpdatePage._on_finished, quindi eseguita
                # dopo di lei (connessioni queued, ordine preservato):
                # quando gira, il worker è già stato ritirato.
                worker.finished_with.connect(self._quit_when_update_done)
            if self.tray_icon.isVisible():
                self.tray_icon.showMessage(
                    APP_NAME,
                    "Aggiornamento del database in corso: l'applicazione "
                    "si chiuderà appena termina.",
                    _app_icon(),
                    5000,
                )
            return
        QApplication.instance().quit()

    def _quit_when_update_done(self, *_args) -> None:
        QTimer.singleShot(0, QApplication.instance().quit)

    def _shutdown_workers(self) -> None:
        """
        Connessa a QApplication.aboutToQuit: copre OGNI uscita, anche
        quelle che non passano da _request_quit (logout della sessione).

        Quando app.exec() restituisce, Python e Qt distruggono i wrapper
        e la MainWindow con i suoi figli: un QThread ancora in esecuzione
        a quel punto produce il qFatal "QThread: Destroyed while thread is
        still running" (SIGABRT). PingWorker è il caso più diretto, perché
        il parent Qt che lo protegge durante l'esecuzione (vedi
        _on_ping_result) qui lo porta a essere distrutto con la finestra.

        _in_ritiro da solo non basta: contiene solo i worker GIÀ ritirati.
        Quelli ancora attivi stanno negli attributi delle pagine.

        I thread che non terminano entro _SHUTDOWN_DEADLINE_SECONDS sono,
        per costruzione, bloccati su I/O non interrompibile (urlopen,
        ping verso un clamd appeso, file grosso in streaming): il loro
        risultato alla chiusura non serve più. Per loro niente terminate()
        (ucciderebbe il thread a metà di qualunque operazione, lock
        compresi) ma os._exit(), dopo aver salvato le impostazioni: il
        processo esce senza passare dai distruttori, quindi senza abort.
        """
        if getattr(self, "clamd_health_timer", None) is not None:
            self.clamd_health_timer.stop()

        workers: dict[int, QThread] = {}

        for w in (
            self.scan_page.worker,
            getattr(self, "bg_worker", None),
            getattr(self, "realtime_worker", None),
        ):
            if w is not None:
                # stop() sveglia anche un worker in pausa (vedi
                # ScanWorker.stop): esce al confine del file corrente.
                w.stop()
                workers[id(w)] = w

        for w in (
            self.update_page.worker,
            self.settings_page._update_check_worker,
            getattr(self, "_ping_worker", None),
            getattr(self, "_db_info_worker", None),
            *list(_in_ritiro),
        ):
            if w is not None:
                workers[id(w)] = w

        for w in workers.values():
            w.requestInterruption()

        deadline = time.monotonic() + _SHUTDOWN_DEADLINE_SECONDS
        for w in workers.values():
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            w.wait(remaining_ms)

        survivors = [w for w in workers.values() if w.isRunning()]
        if not survivors:
            return

        names = ", ".join(sorted({type(w).__name__ for w in survivors}))
        print(
            f"{APP_NAME}: thread ancora attivi alla chiusura ({names}), "
            "uscita forzata.",
            file=sys.stderr,
        )
        QSettings(APP_NAME, APP_NAME).sync()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)

    def setup_ipc(self, server: QLocalServer):
        """Configura il server IPC per ricevere file da scansionare da altre istanze."""
        self.ipc_server = server
        self.ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self):
        """Chiamato quando una seconda istanza invia un file da scansionare."""
        client = self.ipc_server.nextPendingConnection()
        if client:
            raw, truncated = self._read_ipc_payload(client)
            client.disconnectFromServer()

            targets = _decode_ipc_targets(raw, truncated=truncated)
            if targets:
                self._restore_from_tray() # Mostra la finestra se in tray
                self.sidebar.setCurrentRow(0) # Vai alla pagina di scansione
                self.scan_page.start_external_scan([Path(t) for t in targets])

    @staticmethod
    def _read_ipc_payload(client) -> tuple[bytes, bool]:
        """
        Legge il payload finché il client non chiude, entro un tetto di
        byte e di tempo. Con più percorsi il payload può arrivare in più
        letture: un solo read() dopo waitForReadyRead ne prendeva solo il
        primo pezzo. read(n), non readAll(): il tetto si applica mentre si
        legge, non dopo aver accettato tutto in memoria.

        Ritorna (dati, troncato): troncato se si è arrivati al tetto.

        Nota: in PySide waitForReadyRead non rilascia il GIL, quindi
        durante l'attesa i worker Python (scansioni, Real-Time) sono
        fermi. Il client è un processo locale che scrive tutto e chiude
        in pochi millisecondi; la scadenza di _IPC_READ_DEADLINE_S limita
        il caso patologico di un client che si connette e non scrive.
        """
        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + _IPC_READ_DEADLINE_S
        while total < _IPC_MAX_PAYLOAD_BYTES:
            if client.bytesAvailable() == 0:
                if client.state() != QLocalSocket.ConnectedState:
                    break
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0 or not client.waitForReadyRead(remaining_ms):
                    if client.bytesAvailable() == 0:
                        break
            data = bytes(client.read(_IPC_MAX_PAYLOAD_BYTES - total))
            if not data:
                break
            chunks.append(data)
            total += len(data)
        return b"".join(chunks), total >= _IPC_MAX_PAYLOAD_BYTES

    def _add_sidebar_item(self, text: str, *icon_names: str) -> None:
        item = QListWidgetItem(text)
        item.setIcon(_icon(*icon_names))
        item.setSizeHint(QSize(220, 40))
        item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.sidebar.addItem(item)

    def _change_page(self, index: int) -> None:
        self.content_stack.setCurrentIndex(index)

    def _restore_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._restore_from_tray()

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
        if self.tray_icon.isVisible():
            self.tray_icon.showMessage(
                APP_NAME, "L'applicazione continua a girare nella system tray.", _app_icon(), 3000
            )

    def _on_settings_saved(self) -> None:
        endpoint = self._clamd_endpoint()
        self.scan_page.endpoint = endpoint
        self._start_ping(endpoint)
        self._apply_quarantine_dir()
        self._load_realtime()
        self._load_schedule()

        autostart_enabled = self.settings.value("autostart_system", False, type=bool)
        autostart_ok, autostart_error = self._manage_autostart(autostart_enabled)
        notes = list(self.settings_page.save_notes)

        if autostart_ok and not notes:
            QMessageBox.information(self, "Impostazioni Aggiornate", "Le nuove impostazioni sono state applicate.")
        else:
            if not autostart_ok:
                notes.insert(0, f"Non è stato possibile aggiornare l'avvio automatico:\n{autostart_error}")
            QMessageBox.warning(
                self,
                "Impostazioni Aggiornate (parzialmente)",
                "Le impostazioni sono state salvate, ma:\n\n" + "\n\n".join(notes),
            )

    def _clamd_endpoint(self) -> ClamdEndpoint:
        """
        Endpoint corrente di clamd, letto dalle Impostazioni a ogni uso
        (come la directory di quarantena): tutti i worker lo ricevono da
        qui, nessuno ricostruisce un client per conto suo.

        Con valori salvati non validi (file modificato a mano) si usa
        l'endpoint iniziale di --socket/--tcp, lo si dice una volta per
        sessione, e la pagina Impostazioni propone il predefinito da
        salvare. Non è un ripiego fra trasporti per un errore di
        connessione: quello resta un errore visibile.
        """
        try:
            return load_endpoint(self.settings, self._default_endpoint)
        except ValueError as exc:
            if not self._endpoint_problem_noted:
                self._endpoint_problem_noted = True
                self.tray_icon.showMessage(
                    f"{APP_NAME} - impostazioni di clamd non valide",
                    f"{exc}. Uso {self._default_endpoint.describe()}: correggi la "
                    "connessione in Impostazioni.",
                    _icon("dialog-warning", "data-error"),
                    10000,
                )
            return self._default_endpoint

    def _apply_quarantine_dir(self) -> None:
        """
        Scansione manuale e pagina Quarantena usano un oggetto Quarantine
        creato all'avvio, mentre programmata e Real-Time rileggono la
        directory dalle Impostazioni a ogni esecuzione: senza questo, dopo un
        cambio di cartella le prime due restavano sulla vecchia fino al
        riavvio, e le quarantene divergevano dentro la GUI stessa.
        """
        new_dir = Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR)))
        if new_dir == self.scan_page.quarantine.dir:
            return
        quarantine = Quarantine(new_dir)
        self.scan_page.quarantine = quarantine
        self.quarantine_page.quarantine = quarantine
        self.quarantine_page.refresh()

    def _manage_autostart(self, enabled: bool) -> tuple[bool, str | None]:
        """
        Ritorna (successo, messaggio_errore). Non solleva mai: un errore
        di permessi scrivendo in ~/.config/autostart/ va segnalato
        all'utente da _on_settings_saved, non propagato come traceback.
        """
        try:
            autostart_dir = Path.home() / ".config" / "autostart"
            autostart_dir.mkdir(parents=True, exist_ok=True)
            desktop_file = autostart_dir / "klamav-py.desktop"

            if enabled:
                exec_cmd = _gui_relaunch_command()

                content = f"""[Desktop Entry]
Name={APP_NAME}
Comment=Antivirus frontend for ClamAV
Exec={exec_cmd}
Icon=emblem-virus
Type=Application
Terminal=false
X-GNOME-Autostart-enabled=true
"""
                desktop_file.write_text(content)
            else:
                if desktop_file.exists():
                    desktop_file.unlink()
            return True, None
        except OSError as exc:
            return False, str(exc)

    def _on_schedule_saved(self) -> None:
        self._load_schedule()
        QMessageBox.information(self, "Pianificazione Aggiornata", "La pianificazione è stata aggiornata.")

    def _schedule_interval_s(self) -> float:
        return sched.interval_seconds(
            self.settings.value("schedule_interval", 24, type=int),
            self.settings.value("schedule_unit", "Ore"),
        )

    def _schedule_last_run(self) -> float | None:
        value = self.settings.value("schedule_last_run", None)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _double_schedule_checked(self, state) -> None:
        self._double_schedule_checking = False
        if state is not True or self._double_schedule_noted:
            return
        # Ricontrollato qui: la pianificazione interna può essere stata
        # disattivata mentre systemctl rispondeva.
        if not self.settings.value("schedule_enabled", False, type=bool):
            return
        self._double_schedule_noted = True
        self.tray_icon.showMessage(
            APP_NAME,
            "Sono attive sia la pianificazione interna sia il timer di sistema "
            "klamav-scan.timer: la home verrebbe scansionata due volte. "
            "Vedi la pagina Pianificazione.",
            _app_icon(),
            10000,
        )

    def _load_schedule(self) -> None:
        enabled = self.settings.value("schedule_enabled", False, type=bool)
        if not enabled:
            self.schedule_timer.stop()
            self.scheduler_page.set_next_run("")
            return

        # Entrambe le pianificazioni attive (il timer può essere stato
        # abilitato da terminale dopo quella interna): una volta per
        # sessione, senza bloccare nulla.
        # systemctl --user fuori dal thread della GUI (timeout 10 s).
        if not self._double_schedule_noted and not self._double_schedule_checking:
            self._double_schedule_checking = True
            run_off_gui_thread(timer_enabled, self._double_schedule_checked)

        # Prima attivazione: si fissa la base a "adesso" invece di
        # scansionare subito (l'utente ha appena scelto un intervallo, non
        # chiesto una scansione immediata).
        if self._schedule_last_run() is None:
            self.settings.setValue("schedule_last_run", time.time())
            self.settings.sync()

        self._update_next_run_label()
        if not self.schedule_timer.isActive():
            self.schedule_timer.start()
        # Controllo immediato: una scadenza già passata (PC spento, GUI
        # chiusa) si vede subito, non fra un minuto.
        QTimer.singleShot(0, self._check_schedule)

    def _update_next_run_label(self) -> None:
        self.scheduler_page.set_next_run(
            sched.describe_next(time.time(), self._schedule_last_run(), self._schedule_interval_s())
        )

    def _check_schedule(self) -> None:
        if not self.settings.value("schedule_enabled", False, type=bool):
            return
        if not sched.is_due(time.time(), self._schedule_last_run(), self._schedule_interval_s()):
            return

        remaining = self._schedule_grace_until - time.monotonic()
        if remaining > 0:
            # Scadenza mancata scoperta all'avvio: si recupera, ma dopo
            # qualche minuto e dicendolo, non in silenzio.
            if not self._schedule_late_noted:
                self._schedule_late_noted = True
                self._update_next_run_label()
                self.tray_icon.showMessage(
                    APP_NAME,
                    "La scansione programmata è in ritardo: verrà recuperata "
                    f"tra circa {max(1, round(remaining / 60))} minuti.",
                    _icon("dialog-information"),
                    6000,
                )
            return

        self._run_scheduled_scan()

    def _run_scheduled_scan(self) -> None:
        # Guard "una scansione alla volta": la programmata SALTA (non si
        # accoda) se una manuale è in corso. Accodare significherebbe
        # colli di coda imprevedibili (due traversal home-wide di fila
        # per ore); il salto con registrazione in cronologia è esplicito
        # e verificabile. Il Real-Time non entra nel guard (vedi il
        # commento in ScanPage._start_scan).
        if self.bg_worker is not None:
            return
        if self.scan_page.worker is not None:
            # La scadenza resta valida: il controllo al minuto la riproverà
            # appena la manuale finisce. Cronologia e notifica una volta per
            # scadenza, non a ogni controllo.
            if not self._schedule_skip_noted:
                self._schedule_skip_noted = True
                target_str = self.settings.value("schedule_target", str(Path.home()))
                self.history_manager.add_entry("Programmata (rinviata)", target_str)
                if hasattr(self, "history_page"):
                    self.history_page.refresh()
                self.tray_icon.showMessage(
                    APP_NAME,
                    "Scansione programmata rinviata: un'altra scansione è in corso. "
                    "Partirà appena termina.",
                    _icon("dialog-information"),
                    4000,
                )
            return

        target_str = self.settings.value("schedule_target", str(Path.home()))
        # Esistenza della cartella ed esclusioni (dell'utente e la
        # quarantena) si verificano in ScanWorker.run() (strict_roots), come
        # fa la CLI: una cartella mancante o un'esclusione che nel frattempo
        # contiene la radice fermano la scansione con aborted, che arriva in
        # notifica e in Cronologia una volta sola, invece di farle percorrere
        # zero file. Lì e non qui perché resolve() e stat() su un mount di
        # rete irraggiungibile bloccherebbero la GUI.
        excludes = load_schedule_excludes(self.settings)

        self._schedule_late_noted = False
        self._schedule_skip_noted = False
        self._bg_aborted = None

        # Dopo una scansione non completata il controllo al minuto la
        # ritenta: l'avvio si annuncia solo la prima volta.
        if not self._schedule_aborted_noted:
            self.tray_icon.showMessage(
                APP_NAME, "Avvio scansione automatica in background...", _app_icon(), 3000
            )
        self.scheduler_page.update_progress(f"In corso dal {datetime.now():%H:%M} — avvio…")
        self._bg_log_close()
        self._bg_log_path = None
        self._bg_new_reports = 0
        self._bg_errors = 0
        self.bg_worker = ScanWorker(
            endpoint=self._clamd_endpoint(),
            # Risolta dal worker (strict_roots): le esclusioni sono
            # confrontate in forma canonica, come nella CLI.
            target=Path(target_str),
            strict_roots=True,
            quarantine_dir=Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))),
            auto_quarantine=self.settings.value("auto_quarantine", False, type=bool),
            exclude_dirs=excludes,
        )
        # A differenza della manuale, i risultati della programmata NON
        # sono visibili in nessuna lista UI: senza accumularli qui e
        # scriverli su disco a fine scansione, il dettaglio infetti/
        # errori andrebbe perso per sempre (emerso nei test: tre
        # scansioni programmate con risultati mai ispezionabili).
        self.bg_worker.result_ready.connect(self._on_bg_result)
        self.bg_worker.progress.connect(self._on_bg_progress)
        self.bg_worker.aborted.connect(self._on_bg_aborted)
        # Messaggi che la scansione manuale mostra in lista (problemi del
        # registro delle prese visione, quarantena fallita): prima la
        # programmata non li collegava e non arrivavano da nessuna parte.
        self.bg_worker.error.connect(self._on_bg_error)
        self.bg_worker.unreadable_dir.connect(self._on_bg_unreadable_dir)
        self.bg_worker.acknowledged.connect(self._on_bg_acknowledged)
        self.bg_worker.report_only_found.connect(self._on_bg_report_only)
        self.bg_worker.finished_scan.connect(self._on_bg_finished)
        self.bg_worker.quarantined.connect(self._on_quarantine_changed)
        self.bg_worker.quarantine_outcome.connect(self._on_bg_quarantine_outcome)
        self.bg_worker.start()

    def _on_bg_result(self, result: ScanResult) -> None:
        # Stessi formati di riga del log della pagina Scansione, così
        # "Copia log" dalla GUI e i file di log persistente sono leggibili
        # allo stesso modo.
        if result.infected:
            self._bg_log_write(f"INFETTO — {result.path} ({result.signature})")
        elif result.too_large:
            self._bg_log_write(f"NON VERIFICATO (troppo grande) — {result.path}")
        elif result.status == "ERROR":
            self._bg_log_write(f"ERRORE — {result.path}: {result.signature}")

    def _bg_log_write(self, line: str) -> None:
        """Scrive una riga nel log della scansione programmata, aprendolo
        alla prima riga (una scansione senza nulla da segnalare non lascia
        file vuoti). File 0600 in directory 0700, vedi private_files.py.
        line_buffering: ogni riga arriva su disco subito, così un crash
        della GUI a metà scansione non perde quanto già trovato."""
        if self._bg_log_fh is None:
            if self._bg_log_path is not None:
                return  # apertura già fallita in questa scansione
            try:
                logs_dir = ensure_private_dir(DEFAULT_LOGS_DIR)
                self._bg_log_path = logs_dir / f"scheduled-{datetime.now():%Y%m%d-%H%M%S}.log"
                self._bg_log_fh = open_private_for_write(self._bg_log_path)
                self._bg_log_fh.reconfigure(line_buffering=True)
            except OSError:
                # Meglio niente log che far fallire il flusso; il path
                # resta impostato come marcatore "già tentato".
                self._bg_log_path = self._bg_log_path or DEFAULT_LOGS_DIR
                self._bg_log_fh = None
                return
        try:
            self._bg_log_fh.write(line + "\n")
        except OSError:
            pass

    def _bg_log_close(self) -> Path | None:
        """Chiude il log corrente; restituisce il suo percorso se esiste."""
        fh, self._bg_log_fh = self._bg_log_fh, None
        if fh is None:
            return None
        try:
            fh.close()
        except OSError:
            pass
        return self._bg_log_path

    def _on_bg_quarantine_outcome(self, path: str, outcome: str, detail: str) -> None:
        line = _outcome_line(outcome, detail)
        if line is not None:
            self._bg_log_write(line)

    def _on_bg_aborted(self, message: str) -> None:
        # Arriva prima di finished_scan (connessioni queued, stesso
        # emettitore): _on_bg_finished lo trova già impostato.
        self._bg_aborted = message
        # Solo il primo tentativo scrive un log, quello indicato dalla voce
        # di Cronologia. Un file per ogni tentativo al minuto faceva uscire
        # dalla rotazione (MAX_BG_LOG_FILES) i log delle scansioni vere in
        # dieci minuti, e le loro voci puntavano a file inesistenti.
        if not self._schedule_aborted_noted:
            self._bg_log_write(f"ERRORE — {message}")

    def _on_bg_error(self, message: str) -> None:
        # Il motivo di una scansione non completata arriva prima su aborted
        # (scan_worker): già scritto, una volta per serie di tentativi.
        if message == self._bg_aborted:
            return
        self._bg_errors = getattr(self, "_bg_errors", 0) + 1
        # Stessa riga della lista della pagina Scansione.
        self._bg_log_write(f"ERRORE SISTEMA — {message}")

    def _on_bg_unreadable_dir(self, path: str, reason: str) -> None:
        self._bg_log_write(unreadable_dir_line(path, reason))

    def _on_bg_acknowledged(self, path: str, signature: str) -> None:
        self._bg_log_write(_acknowledged_line(path, signature))

    def _on_bg_report_only(self, report: PendingReport) -> None:
        self._bg_new_reports += 1
        self.reports_page.add_report(report)

    def _on_bg_progress(self, totals: ScanTotals) -> None:
        # Visibilità della scansione background: label in Pianificazione +
        # tooltip della tray (già throttled lato worker a 150ms).
        self.scheduler_page.update_progress(f"In corso — {_totals_summary(totals)}")
        self.tray_icon.setToolTip(
            f"{APP_NAME} — Scansione automatica in corso: {totals.scanned} file…"
        )

    def _on_bg_finished(self, totals: ScanTotals) -> None:
        infections, errors, too_large = totals.infections, totals.errors, totals.too_large
        aborted, self._bg_aborted = self._bg_aborted, None
        # Una scansione non completata non è "completata, 0 infetti": prima
        # clamd irraggiungibile produceva proprio quella notifica.
        first_abort = aborted is not None and not self._schedule_aborted_noted
        if aborted is None:
            self._schedule_aborted_noted = False
            status = f"Scansione automatica completata: {infections} infetti trovati, {errors} errori."
            if too_large:
                status += f" {too_large} file non verificati (troppo grandi)."
            if totals.unreadable_dirs:
                status += (f" {totals.unreadable_dirs} cartelle non leggibili "
                           "(elenco nel log; se sono attese, escludile).")
            if getattr(self, "_bg_new_reports", 0):
                status += " Alcuni file sono stati solo segnalati: vedi la pagina Segnalazioni."
            if getattr(self, "_bg_errors", 0):
                status += (f" {self._bg_errors} problemi durante la scansione "
                           "(dettaglio nel log, vedi Pianificazione).")
            self.tray_icon.showMessage(
                APP_NAME, status, _icon("emblem-virus" if infections > 0 else "emblem-checked"), 5000
            )
        elif first_abort:
            self._schedule_aborted_noted = True
            self.tray_icon.showMessage(
                APP_NAME,
                f"Scansione programmata non completata: {aborted} "
                "Verrà ritentata finché il problema non è risolto.",
                _icon("dialog-warning"),
                8000,
            )
        self._reset_tray_tooltip()

        # Log persistente: il dettaglio di infetti/errori/non-verificati
        # di una scansione background non vive in nessuna lista UI, quindi
        # va su disco — indicizzato dalla voce di cronologia (campo
        # log_file, visibile come tooltip in Cronologia).
        # Il log è stato scritto riga per riga durante la scansione
        # (_bg_log_write): qui si chiude e basta.
        log_path = self._bg_log_close()
        self._bg_log_path = None

        # Rotazione: tieni solo i MAX_BG_LOG_FILES più recenti (i nomi
        # sono ordinabili lessicograficamente per via del formato %Y%m%d).
        try:
            old_logs = sorted(DEFAULT_LOGS_DIR.glob("scheduled-*.log"))
            for old in old_logs[:-MAX_BG_LOG_FILES]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

        # I tentativi successivi di una scansione che non si completa non
        # riempiono la cronologia: basta la prima voce.
        if aborted is None or first_abort:
            target_str = self.settings.value("schedule_target", str(Path.home()))
            self.history_manager.add_entry(
                "Programmata" if aborted is None else "Programmata (non completata)",
                target_str,
                totals,
                log_file=str(log_path) if log_path else None,
            )
            self.history_page.refresh()

        finished_at = datetime.now().strftime("%H:%M")
        summary = f"Ultima esecuzione: {finished_at} — {_totals_summary(totals)}"
        if aborted is not None:
            summary = f"Ultimo tentativo: {finished_at} — non completata: {aborted}"
        if log_path:
            summary += f"\nLog: {log_path}"
        self.scheduler_page.update_progress(summary)

        # L'esecuzione conta solo se arriva alla fine (una scansione
        # interrotta dalla chiusura della GUI viene recuperata al prossimo
        # avvio) e con clamd attivo: con il demone fermo la scansione
        # "finisce" subito senza aver verificato nulla, e va ritentata.
        if aborted is None and not self.clamd_health.is_down:
            self.settings.setValue("schedule_last_run", time.time())
            self.settings.sync()
        self._update_next_run_label()

        # Rilascio differito, vedi _retire_qthread: anche qui l'emit di
        # finished_scan è dentro run(), il thread può non essere ancora
        # completamente terminato quando questa slot gira.
        worker, self.bg_worker = self.bg_worker, None
        if worker is not None:
            _retire_qthread(worker)

    def _on_quarantine_changed(self, original_path: str) -> None:
        """
        BUG-002: la quarantena automatica (pianificata o Real-Time) avviene
        su un QThread separato; senza questo refresh la pagina Quarantena
        non se ne accorgerebbe finché l'app non viene riavviata.
        """
        if hasattr(self, "quarantine_page"):
            self.quarantine_page.refresh()

    def _load_realtime(self) -> None:
        roots = self.settings.value("realtime_paths", [])
        if isinstance(roots, str): roots = [roots]

        self._realtime_roots = list(roots)
        # QFileSystemWatcher NON è ricorsivo: osserva esattamente le
        # cartelle che gli passi. Senza espansione, i file creati nelle
        # sottocartelle di una cartella monitorata non generavano alcun
        # evento e il Real-Time non li vedeva — pur dicendo all'utente
        # di essere attivo su quella cartella.
        paths, truncated = self._expand_recursive(self._realtime_roots)
        self._realtime_configured_paths = paths
        self._realtime_watch_truncated = truncated

        old_paths = self.fs_watcher.directories()
        if old_paths:
            self.fs_watcher.removePaths(old_paths)

        self._realtime_watch_failures = []
        if paths:
            # addPaths() ritorna l'elenco dei path che NON è riuscita ad
            # aggiungere (es. fs.inotify.max_user_watches/max_user_instances
            # esaurito): senza controllare questo valore di ritorno il
            # fallimento è completamente silenzioso — il Real-Time smette
            # di coprire quella cartella e nessuno se ne accorge mai.
            failed = self.fs_watcher.addPaths(paths)
            self._realtime_watch_failures = list(failed)
            for p in paths:
                if p not in failed:
                    self._update_snapshot(p)

        self._update_realtime_status_label()

    # Tetto al numero di cartelle osservate. Ogni cartella consuma un
    # watch inotify: fs.inotify.max_user_watches (default 8192 su molte
    # distribuzioni, condiviso con TUTTI i processi dell'utente — IDE,
    # sincronizzatori cloud, indicizzatori). Espandere ricorsivamente
    # una home senza tetto esaurirebbe la quota e romperebbe anche le
    # altre applicazioni dell'utente, non solo questa.
    # Tarabile: su sistemi con max_user_watches alto (leggi il valore
    # con `cat /proc/sys/fs/inotify/max_user_watches`) si può alzare
    # parecchio. Il default resta prudente perché 8192 è ancora comune
    # e la quota è condivisa con TUTTI i processi dell'utente.
    MAX_WATCH_DIRS = 8000

    # Pseudo-filesystem: contenuto sintetico generato dal kernel, non
    # file veri da sorvegliare, e l'attraversamento può essere
    # patologicamente lento o infinito.
    _PSEUDO_FS_PREFIXES = ("/proc", "/sys", "/dev", "/run")

    @classmethod
    def _expand_recursive(cls, roots: list[str]) -> tuple[list[str], bool]:
        """
        Espande le cartelle configurate nell'elenco completo delle
        sottocartelle da passare a QFileSystemWatcher.

        Ritorna (elenco, troncato). `troncato` è True se si è raggiunto
        MAX_WATCH_DIRS: il chiamante DEVE mostrarlo all'utente, perché
        significa che parte dell'albero non è sorvegliata — un
        Real-Time che si dichiara attivo mentre è cieco su metà delle
        cartelle è peggio di uno spento.

        followlinks=False: un symlink dentro l'albero non viene seguito,
        così una cartella non entra due volte e non si creano cicli.
        Le cartelle nascoste vengono saltate: sono per lo più cache
        applicative che generano eventi in continuazione (il rumore che
        satura il Real-Time senza aggiungere protezione utile).
        """
        out: list[str] = []
        seen: set[str] = set()

        for root in roots:
            if any(root.startswith(p) for p in cls._PSEUDO_FS_PREFIXES):
                continue
            if root not in seen:
                seen.add(root)
                out.append(root)
                if len(out) >= cls.MAX_WATCH_DIRS:
                    return out, True
            try:
                walker = os.walk(root, followlinks=False)
                for dirpath, dirnames, _ in walker:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    for name in dirnames:
                        full = os.path.join(dirpath, name)
                        if full in seen:
                            continue
                        seen.add(full)
                        out.append(full)
                        if len(out) >= cls.MAX_WATCH_DIRS:
                            walker.close()
                            return out, True
            except OSError:
                # Cartella sparita o illeggibile: la radice resta
                # comunque nell'elenco, la riconciliazione periodica
                # riproverà.
                continue

        return out, False

    def _reconcile_realtime_watches(self) -> None:
        """
        Confronta le cartelle effettivamente osservate da fs_watcher con
        quelle configurate, e ri-aggiunge le mancanti. Copre il caso in
        cui una cartella monitorata viene eliminata e ricreata (es. un
        browser che pulisce e ricrea ~/Scaricati): il watch inotify
        sottostante muore silenziosamente e senza questa riconciliazione
        periodica il Real-Time resterebbe cieco su quella cartella fino
        al riavvio dell'app.
        """
        if not self._realtime_roots:
            return

        # Ri-espansione a ogni giro: intercetta le sottocartelle create
        # dopo l'avvio. _on_dir_changed le aggiunge già subito quando
        # compaiono in una cartella osservata, ma questo copre i casi
        # che quell'evento non vede (es. un intero albero spostato
        # dentro con mv, che genera un solo evento sul livello
        # superiore).
        paths, truncated = self._expand_recursive(self._realtime_roots)
        self._realtime_configured_paths = paths
        self._realtime_watch_truncated = truncated

        watched = set(self.fs_watcher.directories())
        missing = [p for p in self._realtime_configured_paths if p not in watched]
        if not missing:
            if self._realtime_watch_failures:
                self._realtime_watch_failures = []
                self._update_realtime_status_label()
            return

        failed = self.fs_watcher.addPaths(missing)
        self._realtime_watch_failures = list(failed)
        for p in missing:
            if p not in failed:
                # scan_existing=True: queste cartelle sono comparse DOPO
                # l'avvio (albero spostato dentro con mv, cartella
                # eliminata e ricreata). I file già presenti sono
                # arrivati con loro e vanno analizzati, non messi in
                # baseline.
                self._update_snapshot(p, scan_existing=True)

        self._update_realtime_status_label()

    def _update_realtime_status_label(self) -> None:
        if not hasattr(self, "settings_page"):
            return

        radici = len(self._realtime_roots)
        if radici == 0:
            self.settings_page.rt_status_label.setText("")
            return

        total = len(self._realtime_configured_paths)
        active = total - len(self._realtime_watch_failures)
        plurale = "cartella" if radici == 1 else "cartelle"

        problemi = []
        if self._realtime_watch_failures:
            # Solo i primi nomi: con l'espansione ricorsiva la lista dei
            # falliti può contenere centinaia di percorsi e renderebbe
            # la label illeggibile.
            campione = ", ".join(self._realtime_watch_failures[:3])
            if len(self._realtime_watch_failures) > 3:
                campione += f", e altre {len(self._realtime_watch_failures) - 3}"
            problemi.append(
                f"{len(self._realtime_watch_failures)} sottocartelle non monitorate "
                f"({campione}) — probabile limite di sistema "
                f"(fs.inotify.max_user_watches/max_user_instances)"
            )
        if self._realtime_dropped:
            problemi.append(
                f"{self._realtime_dropped} file modificati in questa sessione NON verificati "
                f"perché la coda del Real-Time era piena (limite {MAX_REALTIME_QUEUE})"
            )
        if self._realtime_watch_truncated:
            problemi.append(
                f"raggiunto il tetto di {self.MAX_WATCH_DIRS} cartelle sorvegliate: "
                f"le sottocartelle oltre questo limite NON sono protette"
            )

        if not problemi:
            self.settings_page.rt_status_label.setText(
                f"Real-Time attivo su {radici} {plurale} "
                f"({active} sottocartelle incluse, ricorsivo)."
            )
            # Niente color: eredita il colore di testo del tema.
            # palette(mid) è pensato per elementi decorativi e su molti
            # temi Plasma finisce grigio-su-grigio: questa label è
            # l'unico posto dove l'utente vede se il Real-Time è cieco
            # su parte dell'albero, illeggibile equivale ad assente.
            self.settings_page.rt_status_label.setStyleSheet("font-size: 12px;")
        else:
            self.settings_page.rt_status_label.setText(
                f"⚠ Real-Time parziale su {radici} {plurale} "
                f"({active}/{total} sottocartelle attive). " + " | ".join(problemi)
            )
            # Grassetto oltre al colore: il rosso hardcoded ha poco
            # contrasto su temi scuri, il peso del carattere fa passare
            # il segnale comunque.
            self.settings_page.rt_status_label.setStyleSheet(
                "font-size: 12px; font-weight: bold; color: #d32f2f;"
            )

    def _update_snapshot(self, dir_path: str, scan_existing: bool = False) -> None:
        """
        Registra lo stato corrente della cartella come riferimento per
        il confronto successivo.

        scan_existing=False (avvio dell'applicazione): i file già
        presenti diventano la baseline e NON vengono scansionati —
        altrimenti ogni riavvio riscansionerebbe l'intera cartella
        monitorata.

        scan_existing=True (cartella scoperta DOPO l'avvio): i file
        presenti sono arrivati insieme alla cartella e vanno
        scansionati. È il caso di un albero spostato dentro con mv o di
        un archivio estratto: `mv` genera un solo evento sul livello
        superiore, quindi senza questo i file dentro le sottocartelle
        entrerebbero direttamente nella baseline e non verrebbero
        analizzati mai — con l'interfaccia che continua a dichiarare il
        Real-Time attivo su quella cartella.
        """
        snap = {}
        try:
            for entry in os.scandir(dir_path):
                if entry.is_file(follow_symlinks=False):
                    snap[entry.path] = entry.stat().st_mtime
                    if scan_existing:
                        self._schedule_realtime_scan(entry.path)
        except OSError:
            pass
        self._dir_snapshots[dir_path] = snap

    def _on_dir_changed(self, dir_path: str) -> None:
        old_snap = self._dir_snapshots.get(dir_path, {})
        new_snap = {}
        new_subdirs: list[str] = []
        try:
            for entry in os.scandir(dir_path):
                # follow_symlinks=False: un symlink non ha contenuto
                # proprio e il target, se è nell'albero sorvegliato,
                # viene già visto per conto suo. Seguirlo qui
                # significherebbe anche ri-scansionare lo stesso file a
                # ogni tocco di un link.
                if entry.is_file(follow_symlinks=False):
                    new_snap[entry.path] = entry.stat().st_mtime
                elif entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                    new_subdirs.append(entry.path)
        except OSError:
            return

        for fpath, mtime in new_snap.items():
            if fpath not in old_snap or old_snap[fpath] != mtime:
                self._schedule_realtime_scan(fpath)

        self._dir_snapshots[dir_path] = new_snap

        # Sottocartelle appena create: vanno sorvegliate subito, non al
        # prossimo giro di riconciliazione (fino a 60s dopo). Senza
        # questo, una cartella scaricata ed estratta resterebbe scoperta
        # proprio nel momento in cui conta.
        watched = set(self.fs_watcher.directories())
        to_add = [d for d in new_subdirs if d not in watched]
        if to_add and len(watched) < self.MAX_WATCH_DIRS:
            capienza = self.MAX_WATCH_DIRS - len(watched)
            aggiunte = to_add[:capienza]
            failed = self.fs_watcher.addPaths(aggiunte)
            for d in aggiunte:
                if d not in failed:
                    self._realtime_configured_paths.append(d)
                    # scan_existing=True: se la sottocartella è arrivata
                    # già piena (estrazione di un archivio, copia
                    # ricorsiva), i file dentro non hanno generato un
                    # evento proprio e verrebbero altrimenti persi.
                    self._update_snapshot(d, scan_existing=True)
            if len(to_add) > capienza:
                self._realtime_watch_truncated = True
                self._update_realtime_status_label()

    def _schedule_realtime_scan(self, file_path: str) -> None:
        # Una nuova modifica sposta in avanti la scadenza: il file si
        # scansiona 3s dopo l'ULTIMA scrittura, non a metà download.
        if (file_path not in self._pending_realtime_scans
                and len(self._pending_realtime_scans) + len(self._realtime_queue)
                >= MAX_REALTIME_QUEUE):
            self._note_realtime_overflow()
            return
        self._pending_realtime_scans[file_path] = time.monotonic() + REALTIME_DEBOUNCE_S
        if not self._realtime_debounce_timer.isActive():
            self._realtime_debounce_timer.start()

    def _flush_pending_realtime(self) -> None:
        now = time.monotonic()
        due = [p for p, t in self._pending_realtime_scans.items() if t <= now]
        for p in due:
            del self._pending_realtime_scans[p]
            self._queue_realtime_scan(p)
        if not self._pending_realtime_scans:
            self._realtime_debounce_timer.stop()

    def _queue_realtime_scan(self, file_path: str) -> None:
        if not Path(file_path).exists():
            return
        # Già in coda: una seconda copia non aggiunge nulla (la scansione
        # leggerà comunque il contenuto attuale del file).
        if file_path in self._realtime_queued:
            return
        if len(self._realtime_queue) >= MAX_REALTIME_QUEUE:
            self._note_realtime_overflow()
            return
        self._realtime_queue.append(file_path)
        self._realtime_queued.add(file_path)
        self._process_realtime_queue()

    def _note_realtime_overflow(self) -> None:
        """Un file scartato perché la coda è piena: mai in silenzio.

        Contatore di sessione nella label di stato del Real-Time, più una
        notifica e una riga di log per episodio (fino allo svuotamento
        della coda), non per file."""
        self._realtime_dropped += 1
        if not self._realtime_overflow_notified:
            self._realtime_overflow_notified = True
            self.realtime_page.add_outcome_entry(
                f"Coda piena ({MAX_REALTIME_QUEUE} file): i file modificati da ora "
                "NON vengono verificati finché la coda non si svuota", warning=True)
            self.tray_icon.showMessage(
                f"{APP_NAME} - Real-Time sovraccarico",
                "Troppi file modificati in poco tempo: alcuni non verranno verificati. "
                "Considera una scansione manuale della cartella al termine.",
                _icon("dialog-warning"),
                10000,
            )
        self._update_realtime_status_label()

    def _process_realtime_queue(self) -> None:
        if self.realtime_worker is not None:
            return

        # Con clamd fermo ogni file fallirebbe: la coda si conserva e
        # riparte da _apply_clamd_state al ritorno del demone. (La coda
        # non ha ancora un tetto: vedi il punto "tetti di volume".)
        if self.clamd_health.is_down:
            self.realtime_page.set_clamd_down(True, len(self._realtime_queue))
            return

        if not self._realtime_queue:
            # Coda svuotata: un eventuale nuovo sovraccarico è un nuovo
            # episodio e va notificato di nuovo.
            self._realtime_overflow_notified = False
            return

        file_path = self._realtime_queue.popleft()
        self._realtime_queued.discard(file_path)
        self._current_realtime_target = file_path

        self.realtime_page.add_log_entry(Path(file_path).name, False)

        self.realtime_worker = ScanWorker(
            endpoint=self._clamd_endpoint(),
            target=Path(file_path),
            quarantine_dir=Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))),
            auto_quarantine=True
        )
        self.realtime_worker.result_ready.connect(self._on_realtime_result)
        self.realtime_worker.finished_scan.connect(self._on_realtime_finished)
        self.realtime_worker.acknowledged.connect(self._on_realtime_acknowledged)
        self.realtime_worker.report_only_found.connect(self.reports_page.add_report)
        self.realtime_worker.quarantined.connect(self._on_quarantine_changed)
        self.realtime_worker.quarantine_outcome.connect(self._on_realtime_quarantine_outcome)
        self.realtime_worker.start()

    def _on_realtime_result(self, result: ScanResult) -> None:
        if result.infected:
            # La notifica parte da _on_realtime_quarantine_outcome, che
            # arriva subito dopo e sa cosa è successo davvero al file.
            self.realtime_page.add_log_entry(Path(result.path).name, True, result.signature)
        elif result.too_large:
            # Il file è stato accodato dalla pagina Real-Time (prima riga
            # "Analizzato" al momento dell'osservazione) ma non è stato
            # verificato: correggiamo l'etichetta, che altrimenti resterebbe
            # "Analizzato (Sicuro)" per un file di cui clamd non ha
            # esaminato neanche un byte.
            self.realtime_page.add_log_entry(
                Path(result.path).name, False, status="Non verificato (troppo grande)"
            )
        elif result.status == "ERROR":
            # Prima questo caso non era gestito: la riga restava
            # "Analizzato" per un file che clamd non ha mai visto.
            self.realtime_page.add_log_entry(
                Path(result.path).name, False, status="Non verificato (errore)"
            )
            # Può essere clamd appena morto: verifica subito invece di
            # aspettare il controllo periodico (con un tetto di frequenza).
            if self._forced_ping.ready():
                self._start_ping(self._clamd_endpoint())

    def _on_realtime_quarantine_outcome(self, path: str, outcome: str, detail: str) -> None:
        name = Path(path).name
        if outcome == "quarantined":
            title = f"{APP_NAME} - MINACCIA RILEVATA!"
            text = f"{name} è infetto ed è stato messo in quarantena."
            icon = _icon("emblem-virus")
        elif outcome == "report_only":
            title = f"{APP_NAME} - File sospetto"
            text = (f"{name}: rilevato ma NON messo in quarantena ({detail}). "
                    "Verificalo: puoi eliminarlo o registrarne la presa visione dalla "
                    "pagina Segnalazioni.")
            icon = _icon("dialog-warning", "emblem-virus")
        else:
            title = f"{APP_NAME} - MINACCIA RILEVATA!"
            text = f"{name} è infetto ma la quarantena è FALLITA: {detail}"
            icon = _icon("data-error", "emblem-virus")
        self.tray_icon.showMessage(title, text, icon, 10000 if outcome != "quarantined" else 5000)
        if outcome == "quarantined":
            self.realtime_page.add_outcome_entry("messo in quarantena", warning=False)
        elif outcome == "report_only":
            self.realtime_page.add_outcome_entry(f"NON messo in quarantena — {detail}", warning=True)
        else:
            self.realtime_page.add_outcome_entry(f"quarantena FALLITA — {detail}", warning=True)

    def _on_realtime_acknowledged(self, path: str, signature: str) -> None:
        # Nessuna notifica: l'utente l'ha già valutato.
        self.realtime_page.add_log_entry(Path(path).name, False, status="Già valutato")

    def _on_realtime_finished(self, totals: ScanTotals) -> None:
        self.history_manager.add_entry("Real-Time", self._current_realtime_target, totals)
        self.history_page.refresh()

        # Rilascio differito, vedi _retire_qthread; qui il rilascio è
        # ancora più critico perché _process_realtime_queue() può creare
        # SUBITO il worker del file successivo: il vecchio andrebbe
        # distrutto proprio mentre il nuovo parte.
        worker, self.realtime_worker = self.realtime_worker, None
        if worker is not None:
            _retire_qthread(worker)
        self._process_realtime_queue()

    def _check_clamd(self, endpoint: ClamdEndpoint) -> None:
        """
        Verifica che clamd risponda, ma senza mai bloccare l'avvio della
        finestra: il ping gira in un QThread separato (PingWorker) e
        l'eventuale avviso arriva in modo asincrono. Un ping sincrono qui
        potrebbe restare appeso fino a 30s se il socket esiste ma clamd
        non risponde.
        """
        self._start_ping(endpoint, startup=True)

    def _periodic_clamd_check(self) -> None:
        self._start_ping(self._clamd_endpoint())

    def _start_ping(self, endpoint: ClamdEndpoint, startup: bool = False) -> None:
        # Un ping alla volta: se il precedente è ancora in corso (clamd
        # lento o appeso) il suo esito arriverà comunque.
        if self._ping_worker is not None:
            return
        # Il ping di avvio mantiene il timeout di default del client, come
        # prima; quelli periodici e forzati usano un timeout breve.
        timeout = None if startup else CLAMD_PING_TIMEOUT_S
        worker = PingWorker(endpoint, self, timeout=timeout)
        # Con un ping al minuto i worker con parent si accumulerebbero come
        # figli della finestra per tutta la sessione: deleteLater a thread
        # terminato li distrugge. È sicuro perché la ownership è del C++
        # (parent) e finished arriva dopo la fine di run().
        worker.finished.connect(worker.deleteLater)
        worker.result_ready.connect(
            lambda alive: self._on_ping_result(endpoint, alive, startup)
        )
        self._ping_worker = worker
        worker.start()

    def _on_ping_result(self, endpoint: ClamdEndpoint, alive: bool, startup: bool = False) -> None:
        # A differenza degli altri worker questo NON passa da
        # _retire_qthread, ed è deliberato: PingWorker è l'unico creato
        # con un parent Qt (vedi _start_ping, `PingWorker(endpoint,
        # self, ...)`). Con un parent la ownership dell'oggetto passa al
        # C++, quindi la caduta del riferimento Python qui sotto non
        # distrugge l'oggetto sottostante e il qFatal "Destroyed while
        # thread is still running" non può scattare.
        #
        # Il corollario: se qualcuno togliesse quel `self`, o copiasse
        # questo schema per un worker nuovo senza parent, il crash
        # tornerebbe silenziosamente. tests/test_qthread_retire.py
        # verifica che il parent resti.
        #
        # Azzerato PRIMA del QMessageBox: la finestra modale gira un event
        # loop annidato, e nel frattempo il timer periodico deve poter
        # ripartire invece di trovare un ping "ancora in corso".
        self._ping_worker = None

        transition = self.clamd_health.observe(alive, immediate=startup)
        self._apply_clamd_state()

        if startup:
            if not alive:
                self._startup_update_pending = False
                self._apply_db_info(None)
                QMessageBox.warning(
                    self,
                    "clamd non raggiungibile",
                    f"Non riesco a contattare clamd su {endpoint.describe()}.\n"
                    + (
                        "Verifica che clamd sia in ascolto su quell'indirizzo e che "
                        "la rete lo consenta."
                        if endpoint.is_tcp
                        else "Verifica che il servizio clamav-daemon sia attivo."
                    ),
                )
            else:
                self._probe_db_info(endpoint)
            return

        if transition is Transition.WENT_DOWN:
            self._apply_db_info(None)
            self.tray_icon.showMessage(
                f"{APP_NAME} - clamd non risponde",
                "L'antivirus è fermo: il Real-Time è sospeso finché clamd "
                "non torna raggiungibile. Verifica il servizio clamav-daemon.",
                _icon("dialog-warning", "data-error"),
                10000,
            )
        elif transition is Transition.CAME_BACK:
            self.tray_icon.showMessage(
                f"{APP_NAME} - clamd di nuovo attivo",
                "La protezione è ripristinata; i file in attesa vengono analizzati ora.",
                _icon("emblem-checked"),
                5000,
            )
            self._probe_db_info(endpoint)
            self._process_realtime_queue()

    def _apply_clamd_state(self) -> None:
        """Icona e tooltip della tray, banner del Real-Time: persistenti
        finché lo stato non cambia, a differenza delle notifiche."""
        down = self.clamd_health.is_down
        self.tray_icon.setIcon(_icon("dialog-warning", "data-error") if down else _app_icon())
        self._reset_tray_tooltip()
        self.realtime_page.set_clamd_down(down, len(self._realtime_queue) if down else 0)

    def _probe_db_info(self, endpoint: ClamdEndpoint) -> None:
        if self._db_info_worker is not None:
            return
        self._db_info_worker = DbInfoWorker(endpoint)
        self._db_info_worker.result_ready.connect(self._on_db_info)
        self._db_info_worker.start()

    def _on_db_info(self, info: DbInfo | None) -> None:
        self._apply_db_info(info)

        enabled, self._startup_update_pending = self._startup_update_pending, False
        if should_update_on_startup(info, enabled):
            self.update_page._start_update()

        # Rilascio differito, vedi _retire_qthread.
        worker, self._db_info_worker = self._db_info_worker, None
        if worker is not None:
            _retire_qthread(worker)

    def _apply_db_info(self, info: DbInfo | None) -> None:
        self.update_page.set_db_info(info)
        self.settings_page.set_db_info(info)
