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

from PySide6.QtCore import Qt, QSize, QSettings, Signal, QTimer, QFileSystemWatcher, QThread
from PySide6.QtGui import QIcon, QColor, QAction, QFont
from PySide6.QtNetwork import QLocalServer
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
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..clamd_client import ScanResult
from ..quarantine import Quarantine
from .scan_worker import ScanWorker
from .update_worker import UpdateWorker
from .ping_worker import PingWorker

DEFAULT_SOCKET = "/run/clamav/clamd.ctl"
DEFAULT_QUARANTINE_DIR = Path.home() / ".local/share/klamav-py/quarantine"
DEFAULT_HISTORY_FILE = Path.home() / ".local/share/klamav-py/history.json"
DEFAULT_LOGS_DIR = Path.home() / ".local/share/klamav-py/logs"

# Quanti log di scansioni programmate tenere su disco prima di iniziare
# a cancellare i più vecchi: una scansione oraria produce 24 file/giorno,
# senza rotazione il directory cresce indefinitamente.
MAX_BG_LOG_FILES = 10

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


# Worker in attesa di distruzione: vedi _retire_qthread. Deve essere un
# riferimento Python forte e a livello di modulo, perché la sua unica
# ragione d'essere è sopravvivere all'uscita di scope del chiamante.
_in_ritiro: set[QThread] = set()


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
    worker (finished_scan/finished_update) è emesso DENTRO run(), prima
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
_IPC_MAX_PAYLOAD_BYTES = 4096


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


def _default_tray_tooltip() -> str:
    """Tooltip di riposo della tray, con versione: è il valore a cui
    ogni flusso che modifica il tooltip (scansione manuale, programmata,
    pausa) deve tornare a fine attività."""
    return f"{APP_NAME} {__version__} — Protezione attiva"


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
    if new.allKeys():
        return  # già migrato, o già configurato: non toccare nulla
    old = QSettings(_LEGACY_SETTINGS_ORG, _LEGACY_SETTINGS_APP)
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
    QListWidget#ResultsList, QListWidget#MonitoredDirsList, QListWidget#RealTimeLog {
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


class HistoryManager:
    def __init__(self, file_path: Path = DEFAULT_HISTORY_FILE):
        self.file_path = file_path
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def add_entry(
        self,
        scan_type: str,
        target: str,
        scanned: int,
        infections: int,
        errors: int,
        too_large: int = 0,
        log_file: str | None = None,
    ):
        # too_large ha default 0 per compatibilità con le voci scritte
        # dalle versioni precedenti (che non avevano il campo): la
        # matematica scanned = clean + infections + errors + too_large
        # resta leggibile anche per lo storico, dove il campo mancante
        # significa "non controllato, scan precedente alla feature".
        # log_file, se presente, punta al log dettagliato su disco
        # (scansioni background: il dettaglio infetti/errori non vive
        # in nessuna lista UI, quindi senza questo riferimento sarebbe
        # irrecuperabile).
        entries = self.get_entries()
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": scan_type,
            "target": target,
            "scanned": scanned,
            "infections": infections,
            "errors": errors,
            "too_large": too_large,
        }
        if log_file is not None:
            entry["log_file"] = log_file
        entries.append(entry)
        if len(entries) > 1000:
            entries = entries[-1000:]
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=4)
        except Exception:
            pass

    def get_entries(self) -> list:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return []

    def clear(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump([], f)
        except Exception:
            pass


class ScanPage(QWidget):
    def __init__(self, socket_path: str, quarantine: Quarantine, history: HistoryManager, parent=None) -> None:
        super().__init__(parent)
        self.socket_path = socket_path
        self.quarantine = quarantine
        self.history = history
        self.worker: ScanWorker | None = None
        self._scanned = self._infections = self._errors = self._too_large = 0
        self._scan_start_time: float | None = None

        self.path_edit = QLineEdit(str(Path.home()))
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

    def start_external_scan(self, target: Path):
        """Metodo richiamato quando l'app riceve un file da scansionare esternamente (es. Dolphin)."""
        if target and target.exists():
            self.path_edit.setText(str(target))
            QTimer.singleShot(500, self._start_scan)

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Scegli directory da scansionare", self.path_edit.text())
        if chosen:
            self.path_edit.setText(chosen)

    def _start_scan(self) -> None:
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

        self.results_list.clear()
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
            socket_path=self.socket_path,
            target=target,
            quarantine_dir=self.quarantine.dir,
            auto_quarantine=self.auto_quarantine_checkbox.isChecked(),
        )
        self.worker.scanning.connect(self._on_scanning)
        self.worker.progress.connect(self._on_progress)
        self.worker.result_ready.connect(self._on_result)
        self.worker.quarantined.connect(self._on_quarantined)
        self.worker.error.connect(self._on_error)
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

    def _on_progress(self, scanned: int, infections: int, errors: int, too_large: int) -> None:
        # Aggiornamento dei contatori, già filtrato/rallentato lato worker
        # (vedi scan_worker.PROGRESS_THROTTLE_SECONDS): qui ci si limita a
        # mostrare i valori ricevuti, senza fare altri calcoli.
        self._scanned = scanned
        self._infections = infections
        self._errors = errors
        self._too_large = too_large
        text = f"{scanned} scansionati — {infections} infetti — {errors} errori"
        if too_large:
            text += f" — {too_large} non verificati (troppo grandi)"
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
            item.setForeground(QColor("palette(mid)"))
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

    def _on_error(self, message: str) -> None:
        item = QListWidgetItem(f"ERRORE SISTEMA — {message}")
        item.setIcon(QIcon.fromTheme("data-error"))
        self.results_list.addItem(item)

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

    def _on_finished(self, scanned: int, infections: int, errors: int, too_large: int = 0) -> None:
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

        status_text = f"Completato: {scanned} file scansionati, {infections} infetti, {errors} errori."
        if too_large:
            status_text += f" {too_large} non verificati (troppo grandi)."
        self._set_status_text(status_text)

        if infections > 0:
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
            f"Esito: {esito}"
        )

        self.history.add_entry(
            "Manuale", self.path_edit.text(), scanned, infections, errors, too_large=too_large
        )
        if hasattr(main_window, 'history_page'):
            main_window.history_page.refresh()

        if hasattr(main_window, 'tray_icon'):
            icon_type = "emblem-checked" if infections == 0 else "emblem-virus"
            main_window.tray_icon.showMessage(
                f"{APP_NAME} — Scansione completata", status_text, _icon(icon_type), 6000
            )

        # Il popup esplicito compare solo se la finestra è visibile in
        # quel momento: se l'app è minimizzata in tray, forzare la
        # comparsa di un dialogo riporterebbe in primo piano una finestra
        # che l'utente aveva volutamente nascosto — in quel caso basta e
        # avanza la notifica tray sopra.
        if self.isVisible() and self.window().isVisible():
            report_box = QMessageBox(self)
            report_box.setIcon(QMessageBox.Warning if infections > 0 else QMessageBox.Information)
            report_box.setWindowTitle("Scansione completata")
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

        layout.addWidget(self.table, 1)
        layout.addLayout(buttons_row)

        self.refresh()

    def refresh(self) -> None:
        entries = self.quarantine.list_entries()
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


class UpdatePage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.worker: UpdateWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Aggiornamento Database Virus")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel("Scarica le ultime definizioni dei virus tramite 'freshclam'.\nPotrebbe essere richiesta la password di amministratore.")
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)
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

    def _start_update(self) -> None:
        if self.worker is not None:
            return

        self.log_console.clear()
        self.progress.setVisible(True)
        self.update_button.setEnabled(False)
        self.log_console.appendPlainText("Avvio dell'aggiornamento in corso...\n")

        self.worker = UpdateWorker()
        self.worker.output_line.connect(self._on_output)
        self.worker.finished_update.connect(self._on_finished)
        self.worker.start()

    def _on_output(self, line: str) -> None:
        self.log_console.appendPlainText(line)

    def _on_finished(self, success: bool, message: str) -> None:
        self.progress.setVisible(False)
        self.update_button.setEnabled(True)
        self.log_console.appendPlainText(f"\n{message}")

        main_window = self.window()
        if hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            icon_type = "emblem-checked" if success else "data-error"
            main_window.tray_icon.showMessage(
                f"{APP_NAME} - Aggiornamento", message, _icon(icon_type), 5000
            )

        # Rilascio differito: self.worker = None qui scaricherebbe il
        # QThread mentre run() sta ancora chiudendo (l'emit che ha
        # invocato questa slot è dentro run()) — vedi _retire_qthread.
        worker, self.worker = self.worker, None
        if worker is not None:
            _retire_qthread(worker)


class RealTimePage(QWidget):
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
            text = f"[{time_str}] MINACCIA RILEVATA: {file_name} ({signature}) -> In Quarantena"
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

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["Data e Ora", "Tipo", "Percorso", "Scansionati", "Infetti", "Errori", "Non verificati"]
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


class SchedulerPage(QWidget):
    schedule_saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # QSettings sempre con org/app ESPLICITI (mai QSettings() di
        # default): il valore non deve dipendere da come è stato creato
        # QApplication, e deve coincidere con quello della migrazione.
        self.settings = QSettings(APP_NAME, APP_NAME)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = QLabel("Pianificazione Scansioni")
        title.setStyleSheet("font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        desc = QLabel("Imposta una scansione automatica in background. L'app deve rimanere aperta (anche nella system tray) per eseguire le scansioni programmate.")
        desc.setStyleSheet("font-size: 14px; color: palette(mid);")
        desc.setWordWrap(True)
        layout.addWidget(desc)
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

        buttons_row = QHBoxLayout()
        buttons_row.addStretch()

        save_btn = QPushButton("Salva Pianificazione")
        save_btn.setObjectName("PrimaryButton")
        save_btn.setFixedHeight(36)
        save_btn.setIcon(QIcon.fromTheme("document-save"))
        save_btn.clicked.connect(self._save_schedule)

        buttons_row.addWidget(save_btn)
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

        self._load_settings()

    def update_progress(self, text: str) -> None:
        self.execution_status_label.setText(text)

    def _browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleziona cartella da scansionare", self.target_edit.text())
        if path:
            self.target_edit.setText(path)

    def _load_settings(self) -> None:
        self.enable_check.setChecked(self.settings.value("schedule_enabled", False, type=bool))
        self.interval_spin.setValue(self.settings.value("schedule_interval", 24, type=int))

        unit = self.settings.value("schedule_unit", "Ore")
        idx = self.unit_combo.findText(unit)
        if idx >= 0:
            self.unit_combo.setCurrentIndex(idx)

        self.target_edit.setText(self.settings.value("schedule_target", str(Path.home())))

    def _save_schedule(self) -> None:
        self.settings.setValue("schedule_enabled", self.enable_check.isChecked())
        self.settings.setValue("schedule_interval", self.interval_spin.value())
        self.settings.setValue("schedule_unit", self.unit_combo.currentText())
        self.settings.setValue("schedule_target", self.target_edit.text())

        self.schedule_saved.emit()

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

        socket_layout = QHBoxLayout()
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
        general_layout.addLayout(socket_layout)

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

        self.startup_update_check = QCheckBox("Aggiorna il database dei virus all'avvio dell'applicazione")
        general_layout.addWidget(self.startup_update_check)

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
        version_label = QLabel(f"Versione: {__version__}")
        version_label.setStyleSheet("font-size: 12px; color: palette(mid);")
        about_layout.addWidget(version_label)
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

        buttons_row.addWidget(reset_btn)
        buttons_row.addWidget(save_btn)

        layout.addStretch()
        layout.addLayout(buttons_row)

        self._load_settings()

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

    def _load_settings(self) -> None:
        self.autostart_check.setChecked(self.settings.value("autostart_system", False, type=bool))
        self.start_in_tray_check.setChecked(self.settings.value("start_in_tray", False, type=bool))
        self.socket_edit.setText(self.settings.value("socket_path", DEFAULT_SOCKET))
        self.quar_edit.setText(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR)))
        self.auto_quar_check.setChecked(self.settings.value("auto_quarantine", False, type=bool))
        self.startup_update_check.setChecked(self.settings.value("startup_update", True, type=bool))

        rt_dirs = self.settings.value("realtime_paths", [])
        if isinstance(rt_dirs, str): rt_dirs = [rt_dirs]
        self.rt_dirs_list.addItems(rt_dirs)

    def _reset_defaults(self) -> None:
        self.autostart_check.setChecked(False)
        self.start_in_tray_check.setChecked(False)
        self.socket_edit.setText(DEFAULT_SOCKET)
        self.quar_edit.setText(str(DEFAULT_QUARANTINE_DIR))
        self.auto_quar_check.setChecked(False)
        self.startup_update_check.setChecked(True)
        self.rt_dirs_list.clear()

    def _save_settings(self) -> None:
        self.settings.setValue("autostart_system", self.autostart_check.isChecked())
        self.settings.setValue("start_in_tray", self.start_in_tray_check.isChecked())
        self.settings.setValue("socket_path", self.socket_edit.text())
        self.settings.setValue("quarantine_dir", self.quar_edit.text())
        self.settings.setValue("auto_quarantine", self.auto_quar_check.isChecked())
        self.settings.setValue("startup_update", self.startup_update_check.isChecked())

        rt_dirs = [self.rt_dirs_list.item(i).text() for i in range(self.rt_dirs_list.count())]
        self.settings.setValue("realtime_paths", rt_dirs)

        self.settings_saved.emit()

        main_window = self.window()
        if hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            main_window.tray_icon.showMessage(
                APP_NAME, "Impostazioni salvate con successo.", _icon("emblem-checked"), 3000
            )

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
Exec={exec_cmd} --scan-target %f
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
    def __init__(self, socket_path: str = DEFAULT_SOCKET, quarantine_dir: Path = DEFAULT_QUARANTINE_DIR, scan_target: Path = None) -> None:
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
        quit_action.triggered.connect(QApplication.instance().quit)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

        saved_socket = self.settings.value("socket_path", socket_path)
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
        self._add_sidebar_item("Aggiornamenti", "system-software-update", "view-refresh")
        self._add_sidebar_item("Real-Time", "view-history", "chronometer")
        self._add_sidebar_item("Pianificazione", "view-time-schedule", "task-recurring")
        self._add_sidebar_item("Impostazioni", "configure", "preferences-system")

        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self._change_page)

        self.content_stack = QStackedWidget()
        self.content_stack.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self.scan_page = ScanPage(saved_socket, quarantine, self.history_manager)
        self.history_page = HistoryPage(self.history_manager)
        self.quarantine_page = QuarantinePage(quarantine)
        self.update_page = UpdatePage()
        self.realtime_page = RealTimePage()
        self.scheduler_page = SchedulerPage()
        self.settings_page = SettingsPage()

        self.settings_page.settings_saved.connect(self._on_settings_saved)
        self.scheduler_page.schedule_saved.connect(self._on_schedule_saved)

        self.content_stack.addWidget(self.scan_page)
        self.content_stack.addWidget(self.history_page)
        self.content_stack.addWidget(self.quarantine_page)
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

        self.schedule_timer = QTimer(self)
        self.schedule_timer.timeout.connect(self._run_scheduled_scan)
        self.bg_worker = None
        self._bg_result_lines: list[str] = []
        self._load_schedule()

        self.fs_watcher = QFileSystemWatcher()
        self.fs_watcher.directoryChanged.connect(self._on_dir_changed)
        self._pending_realtime_scans = {}
        self._dir_snapshots = {}
        self._realtime_queue = []
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

        self._check_clamd(saved_socket)

        if self.settings.value("startup_update", True, type=bool):
            QTimer.singleShot(1500, self.update_page._start_update)

        # Se avviata con un target (es. da Dolphin in prima istanza), avvia la scansione
        if scan_target:
            self.scan_page.start_external_scan(scan_target)

    def _reset_tray_tooltip(self) -> None:
        """Riporta il tooltip della tray al riposo: chiamato a fine di
        ogni attività che lo ha modificato (scansione manuale,
        programmata, pausa)."""
        self.tray_icon.setToolTip(_default_tray_tooltip())

    def setup_ipc(self, server: QLocalServer):
        """Configura il server IPC per ricevere file da scansionare da altre istanze."""
        self.ipc_server = server
        self.ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self):
        """Chiamato quando una seconda istanza invia un file da scansionare."""
        client = self.ipc_server.nextPendingConnection()
        if client:
            client.waitForReadyRead(1000)
            # read(), non readAll(): impone il limite in _decode_ipc_payload
            # invece di accettare e poi eventualmente scartare un payload
            # già ricevuto per intero in memoria.
            raw = bytes(client.read(_IPC_MAX_PAYLOAD_BYTES))
            client.disconnectFromServer()

            data = _decode_ipc_payload(raw)
            if data:
                target_path = Path(data)
                self._restore_from_tray() # Mostra la finestra se in tray
                self.sidebar.setCurrentRow(0) # Vai alla pagina di scansione
                self.scan_page.start_external_scan(target_path)

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
        new_socket = self.settings.value("socket_path", DEFAULT_SOCKET)
        self.scan_page.socket_path = new_socket
        self._load_realtime()
        self._load_schedule()

        autostart_enabled = self.settings.value("autostart_system", False, type=bool)
        autostart_ok, autostart_error = self._manage_autostart(autostart_enabled)

        if autostart_ok:
            QMessageBox.information(self, "Impostazioni Aggiornate", "Le nuove impostazioni sono state applicate.")
        else:
            QMessageBox.warning(
                self,
                "Impostazioni Aggiornate (parzialmente)",
                "Le impostazioni sono state salvate, ma non è stato possibile "
                f"aggiornare l'avvio automatico:\n{autostart_error}",
            )

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

    def _load_schedule(self) -> None:
        enabled = self.settings.value("schedule_enabled", False, type=bool)
        if not enabled:
            self.schedule_timer.stop()
            return

        interval = self.settings.value("schedule_interval", 24, type=int)
        unit = self.settings.value("schedule_unit", "Ore")

        if unit == "Giorni":
            ms = interval * 24 * 60 * 60 * 1000
        else:
            ms = interval * 60 * 60 * 1000

        self.schedule_timer.setInterval(ms)
        self.schedule_timer.start()

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
            target_str = self.settings.value("schedule_target", str(Path.home()))
            self.history_manager.add_entry("Programmata (saltata)", target_str, 0, 0, 0)
            if hasattr(self, "history_page"):
                self.history_page.refresh()
            self.tray_icon.showMessage(
                APP_NAME,
                "Scansione programmata saltata: un'altra scansione è già in corso.",
                _icon("dialog-information"),
                4000,
            )
            return

        target_str = self.settings.value("schedule_target", str(Path.home()))
        target = Path(target_str)
        if not target.exists(): return

        self.tray_icon.showMessage(
            APP_NAME, "Avvio scansione automatica in background...", _app_icon(), 3000
        )
        self.scheduler_page.update_progress(f"In corso dal {datetime.now():%H:%M} — avvio…")
        self._bg_result_lines = []
        self.bg_worker = ScanWorker(
            socket_path=self.settings.value("socket_path", DEFAULT_SOCKET),
            target=target,
            quarantine_dir=Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))),
            auto_quarantine=self.settings.value("auto_quarantine", False, type=bool)
        )
        # A differenza della manuale, i risultati della programmata NON
        # sono visibili in nessuna lista UI: senza accumularli qui e
        # scriverli su disco a fine scansione, il dettaglio infetti/
        # errori andrebbe perso per sempre (emerso nei test: tre
        # scansioni programmate con risultati mai ispezionabili).
        self.bg_worker.result_ready.connect(self._on_bg_result)
        self.bg_worker.progress.connect(self._on_bg_progress)
        self.bg_worker.finished_scan.connect(self._on_bg_finished)
        self.bg_worker.quarantined.connect(self._on_quarantine_changed)
        self.bg_worker.start()

    def _on_bg_result(self, result: ScanResult) -> None:
        # Stessi formati di riga del log della pagina Scansione, così
        # "Copia log" dalla GUI e i file di log persistente sono leggibili
        # allo stesso modo.
        if result.infected:
            self._bg_result_lines.append(f"INFETTO — {result.path} ({result.signature})")
        elif result.too_large:
            self._bg_result_lines.append(f"NON VERIFICATO (troppo grande) — {result.path}")
        elif result.status == "ERROR":
            self._bg_result_lines.append(f"ERRORE — {result.path}: {result.signature}")

    def _on_bg_progress(self, scanned: int, infections: int, errors: int, too_large: int) -> None:
        # Visibilità della scansione background: label in Pianificazione +
        # tooltip della tray (già throttled lato worker a 150ms).
        self.scheduler_page.update_progress(
            f"In corso — {scanned} file scansionati, {infections} infetti, {errors} errori"
            + (f", {too_large} non verificati" if too_large else "")
        )
        self.tray_icon.setToolTip(
            f"{APP_NAME} — Scansione automatica in corso: {scanned} file…"
        )

    def _on_bg_finished(self, scanned: int, infections: int, errors: int, too_large: int = 0) -> None:
        status = f"Scansione automatica completata: {infections} infetti trovati, {errors} errori."
        if too_large:
            status += f" {too_large} file non verificati (troppo grandi)."
        self.tray_icon.showMessage(
            APP_NAME, status, _icon("emblem-virus" if infections > 0 else "emblem-checked"), 5000
        )
        self._reset_tray_tooltip()

        # Log persistente: il dettaglio di infetti/errori/non-verificati
        # di una scansione background non vive in nessuna lista UI, quindi
        # va su disco — indicizzato dalla voce di cronologia (campo
        # log_file, visibile come tooltip in Cronologia).
        log_path = None
        if self._bg_result_lines:
            try:
                logs_dir = DEFAULT_LOGS_DIR
                logs_dir.mkdir(parents=True, exist_ok=True)
                log_path = logs_dir / f"scheduled-{datetime.now():%Y%m%d-%H%M%S}.log"
                log_path.write_text("\n".join(self._bg_result_lines) + "\n", encoding="utf-8")
            except OSError:
                log_path = None  # meglio niente log che far fallire il flusso
            self._bg_result_lines = []

        # Rotazione: tieni solo i MAX_BG_LOG_FILES più recenti (i nomi
        # sono ordinabili lessicograficamente per via del formato %Y%m%d).
        try:
            old_logs = sorted(DEFAULT_LOGS_DIR.glob("scheduled-*.log"))
            for old in old_logs[:-MAX_BG_LOG_FILES]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

        target_str = self.settings.value("schedule_target", str(Path.home()))
        self.history_manager.add_entry(
            "Programmata",
            target_str,
            scanned,
            infections,
            errors,
            too_large=too_large,
            log_file=str(log_path) if log_path else None,
        )
        self.history_page.refresh()

        finished_at = datetime.now().strftime("%H:%M")
        summary = (
            f"Ultima esecuzione: {finished_at} — {scanned} file, "
            f"{infections} infetti, {errors} errori"
            + (f", {too_large} non verificati" if too_large else "")
        )
        if log_path:
            summary += f"\nLog: {log_path}"
        self.scheduler_page.update_progress(summary)

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
        if file_path in self._pending_realtime_scans:
            self._pending_realtime_scans[file_path].stop()

        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._queue_realtime_scan(file_path))
        timer.start(3000)
        self._pending_realtime_scans[file_path] = timer

    def _queue_realtime_scan(self, file_path: str) -> None:
        self._pending_realtime_scans.pop(file_path, None)

        if not Path(file_path).exists():
            return

        self._realtime_queue.append(file_path)
        self._process_realtime_queue()

    def _process_realtime_queue(self) -> None:
        if self.realtime_worker is not None:
            return

        if not self._realtime_queue:
            return

        file_path = self._realtime_queue.pop(0)
        self._current_realtime_target = file_path

        self.realtime_page.add_log_entry(Path(file_path).name, False)

        self.realtime_worker = ScanWorker(
            socket_path=self.settings.value("socket_path", DEFAULT_SOCKET),
            target=Path(file_path),
            quarantine_dir=Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))),
            auto_quarantine=True
        )
        self.realtime_worker.result_ready.connect(self._on_realtime_result)
        self.realtime_worker.finished_scan.connect(self._on_realtime_finished)
        self.realtime_worker.quarantined.connect(self._on_quarantine_changed)
        self.realtime_worker.start()

    def _on_realtime_result(self, result: ScanResult) -> None:
        if result.infected:
            self.realtime_page.add_log_entry(Path(result.path).name, True, result.signature)
            self.tray_icon.showMessage(
                f"{APP_NAME} - MINACCIA RILEVATA!",
                f"{Path(result.path).name} è infetto ed è stato messo in quarantena.",
                _icon("emblem-virus"),
                5000
            )
        elif result.too_large:
            # Il file è stato accodato dalla pagina Real-Time (prima riga
            # "Analizzato" al momento dell'osservazione) ma non è stato
            # verificato: correggiamo l'etichetta, che altrimenti resterebbe
            # "Analizzato (Sicuro)" per un file di cui clamd non ha
            # esaminato neanche un byte.
            self.realtime_page.add_log_entry(
                Path(result.path).name, False, status="Non verificato (troppo grande)"
            )

    def _on_realtime_finished(self, scanned: int, infections: int, errors: int, too_large: int = 0) -> None:
        self.history_manager.add_entry(
            "Real-Time", self._current_realtime_target, scanned, infections, errors, too_large=too_large
        )
        self.history_page.refresh()

        # Rilascio differito, vedi _retire_qthread; qui il rilascio è
        # ancora più critico perché _process_realtime_queue() può creare
        # SUBITO il worker del file successivo: il vecchio andrebbe
        # distrutto proprio mentre il nuovo parte.
        worker, self.realtime_worker = self.realtime_worker, None
        if worker is not None:
            _retire_qthread(worker)
        self._process_realtime_queue()

    def _check_clamd(self, socket_path: str) -> None:
        """
        Verifica che clamd risponda, ma senza mai bloccare l'avvio della
        finestra: il ping gira in un QThread separato (PingWorker) e
        l'eventuale avviso arriva in modo asincrono. Un ping sincrono qui
        potrebbe restare appeso fino a 30s se il socket esiste ma clamd
        non risponde.
        """
        self._ping_worker = PingWorker(socket_path, self)
        self._ping_worker.result_ready.connect(
            lambda alive: self._on_ping_result(socket_path, alive)
        )
        self._ping_worker.start()

    def _on_ping_result(self, socket_path: str, alive: bool) -> None:
        if not alive:
            QMessageBox.warning(
                self,
                "clamd non raggiungibile",
                f"Non riesco a contattare clamd su {socket_path}.\n"
                "Verifica che il servizio clamav-daemon sia attivo.",
            )
        # A differenza degli altri worker questo NON passa da
        # _retire_qthread, ed è deliberato: PingWorker è l'unico creato
        # con un parent Qt (vedi _check_clamd, `PingWorker(socket_path,
        # self)`). Con un parent la ownership dell'oggetto passa al C++,
        # quindi la caduta del riferimento Python qui sotto non distrugge
        # l'oggetto sottostante e il qFatal "Destroyed while thread is
        # still running" non può scattare.
        #
        # Il corollario: se qualcuno togliesse quel `self`, o copiasse
        # questo schema per un worker nuovo senza parent, il crash
        # tornerebbe silenziosamente. tests/test_qthread_retire.py
        # verifica che il parent resti.
        self._ping_worker = None
