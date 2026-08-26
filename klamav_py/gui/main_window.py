"""
Finestra principale PySide6 (UI Moderna Stile KDE Plasma).
Aggiunto supporto Single Instance (IPC) e fix ridimensionamento finestra.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime
import os
import sys
import json

from PySide6.QtCore import Qt, QSize, QSettings, Signal, QTimer, QFileSystemWatcher
from PySide6.QtGui import QIcon, QColor, QAction, QFont
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
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

from ..clamd_client import ScanResult
from ..quarantine import Quarantine
from .scan_worker import ScanWorker
from .update_worker import UpdateWorker
from .ping_worker import PingWorker

DEFAULT_SOCKET = "/run/clamav/clamd.ctl"
DEFAULT_QUARANTINE_DIR = Path.home() / ".local/share/klamav-py/quarantine"
DEFAULT_HISTORY_FILE = Path.home() / ".local/share/klamav-py/history.json"

_BUNDLED_ICON_PATH = Path(__file__).parent / "resources" / "klamav-icon.svg"


def _icon(*theme_names: str) -> QIcon:
    for name in theme_names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    if _BUNDLED_ICON_PATH.exists():
        return QIcon(str(_BUNDLED_ICON_PATH))
    return QIcon()


def _app_icon() -> QIcon:
    return _icon("emblem-virus", "security-high", "security-medium")


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

    def add_entry(self, scan_type: str, target: str, scanned: int, infections: int, errors: int):
        entries = self.get_entries()
        entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "type": scan_type,
            "target": target,
            "scanned": scanned,
            "infections": infections,
            "errors": errors
        }
        entries.append(entry)
        if len(entries) > 1000:
            entries = entries[-1000:]
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(entries, f, indent=4)
        except Exception:
            pass

    def get_entries(self) -> list:
        if not self.file_path.exists():
            return []
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            return []

    def clear(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
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
        self._scanned = self._infections = self._errors = 0

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

        self.status_label = QLabel("Pronto.")
        self.status_label.setStyleSheet("font-size: 14px; color: palette(mid);")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

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
        buttons_row.addWidget(self.stop_button)
        buttons_row.addStretch()

        results_buttons_row = QHBoxLayout()
        results_buttons_row.setSpacing(10)
        results_buttons_row.addWidget(self.quarantine_selected_button)
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

        self.results_list.clear()
        self.progress.setVisible(True)
        self.status_label.setText("Scansione in corso…")
        self._scanned = self._infections = self._errors = 0
        self.counts_label.setText("")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        self.worker = ScanWorker(
            socket_path=self.socket_path,
            target=target,
            quarantine_dir=self.quarantine.dir,
            auto_quarantine=self.auto_quarantine_checkbox.isChecked(),
        )
        self.worker.scanning.connect(self._on_scanning)
        self.worker.result_ready.connect(self._on_result)
        self.worker.error.connect(self._on_error)
        self.worker.finished_scan.connect(self._on_finished)
        self.worker.start()

    def _stop_scan(self) -> None:
        if self.worker is not None:
            self.worker.stop()
            self.status_label.setText("Interruzione richiesta…")

    def _on_scanning(self, path: str) -> None:
        self.status_label.setText(f"Scansione in corso: {path}")

    def _on_result(self, result: ScanResult) -> None:
        self._scanned += 1
        if result.infected:
            self._infections += 1
        elif result.status == "ERROR":
            self._errors += 1
        self.counts_label.setText(
            f"{self._scanned} scansionati — {self._infections} infetti — {self._errors} errori"
        )

        if result.infected:
            item = QListWidgetItem(f"INFETTO — {result.path} ({result.signature})")
            item.setIcon(QIcon.fromTheme("emblem-virus"))
            item.setForeground(QColor("#e4311b"))
            item.setData(Qt.UserRole, {"path": result.path, "signature": result.signature})
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()
        elif result.status == "ERROR":
            item = QListWidgetItem(f"ERRORE — {result.path}: {result.signature}")
            item.setIcon(QIcon.fromTheme("data-error"))
            self.results_list.addItem(item)
            self.results_list.scrollToBottom()

    def _on_error(self, message: str) -> None:
        item = QListWidgetItem(f"ERRORE SISTEMA — {message}")
        item.setIcon(QIcon.fromTheme("data-error"))
        self.results_list.addItem(item)

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

        if skipped and not moved:
            QMessageBox.information(self, "Nessun file infetto selezionato", "La selezione non contiene file infetti da mettere in quarantena.")

    def _on_finished(self, scanned: int, infections: int, errors: int) -> None:
        self.progress.setVisible(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

        status_text = f"Completato: {scanned} file scansionati, {infections} infetti, {errors} errori."
        self.status_label.setText(status_text)

        self.history.add_entry("Manuale", self.path_edit.text(), scanned, infections, errors)
        main_window = self.window()
        if hasattr(main_window, 'history_page'):
            main_window.history_page.refresh()

        if hasattr(main_window, 'tray_icon') and main_window.tray_icon.isVisible():
            icon_type = "emblem-checked" if infections == 0 else "emblem-virus"
            main_window.tray_icon.showMessage("KlamAV", status_text, _icon(icon_type), 5000)

        self.worker = None


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
            main_window.tray_icon.showMessage("KlamAV - Aggiornamento", message, _icon(icon_type), 5000)

        self.worker = None


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

    def add_log_entry(self, file_name: str, infected: bool, signature: str = "") -> None:
        time_str = datetime.now().strftime("%H:%M:%S")
        if infected:
            text = f"[{time_str}] MINACCIA RILEVATA: {file_name} ({signature}) -> In Quarantena"
            item = QListWidgetItem(text)
            item.setIcon(QIcon.fromTheme("emblem-virus"))
            item.setForeground(QColor("#e4311b"))
        else:
            text = f"[{time_str}] Analizzato: {file_name} (Sicuro)"
            item = QListWidgetItem(text)
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

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Data e Ora", "Tipo", "Percorso", "Scansionati", "Infetti", "Errori"])
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

    def _clear_history(self) -> None:
        confirm = QMessageBox.question(self, "Conferma", "Cancellare tutta la cronologia delle scansioni?")
        if confirm == QMessageBox.Yes:
            self.history.clear()
            self.refresh()


class SchedulerPage(QWidget):
    schedule_saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.settings = QSettings("KlamAV", "KlamAV")

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

        self._load_settings()

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
            main_window.tray_icon.showMessage("KlamAV", "Pianificazione salvata con successo.", _icon("emblem-checked"), 3000)


class SettingsPage(QWidget):
    settings_saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.settings = QSettings("KlamAV", "KlamAV")

        layout = QVBoxLayout(self)
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

        layout.addWidget(rt_group)

        dolphin_group = QGroupBox("Integrazione File Manager (Dolphin)")
        dolphin_layout = QVBoxLayout(dolphin_group)
        dolphin_desc = QLabel("Aggiunge la voce \"Scansiona con KlamAV\" al menu del tasto destro su file e cartelle.")
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
            main_window.tray_icon.showMessage("KlamAV", "Impostazioni salvate con successo.", _icon("emblem-checked"), 3000)

    def _install_dolphin(self):
        try:
            dirs = [
                Path.home() / ".local/share/kservices5/ServiceMenus",
                Path.home() / ".local/share/kio/servicemenus"
            ]
            file_name = "klamav_scan.desktop"
            python_exec = sys.executable
            project_root = Path(__file__).parent.parent.parent

            content = f"""[Desktop Entry]
Type=Service
Actions=scanWithKlamAV
Encoding=UTF-8
MimeType=all/all;inode/directory;
X-KDE-ServiceTypes=KonqPopupMenuPlugin
X-KDE-Priority=TopLevel
Path={project_root}

[Desktop Action scanWithKlamAV]
Name=Scansiona con KlamAV-Py
Icon=edit-find
Exec={python_exec} -m klamav_py.gui.app --scan-target %f
"""
            for d in dirs:
                d.mkdir(parents=True, exist_ok=True)
                file_path = d / file_name
                file_path.write_text(content)
                os.chmod(file_path, 0o755)

            os.system("kbuildsycoca6 2>/dev/null; kbuildsycoca5 2>/dev/null")

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

            os.system("kbuildsycoca6 2>/dev/null; kbuildsycoca5 2>/dev/null")

            QMessageBox.information(self, "Integrazione Dolphin", "Integrazione rimossa con successo.")

        except Exception as e:
            QMessageBox.critical(self, "Errore Rimozione", f"Si è verificato un errore:\n{str(e)}")


class MainWindow(QMainWindow):
    def __init__(self, socket_path: str = DEFAULT_SOCKET, quarantine_dir: Path = DEFAULT_QUARANTINE_DIR, scan_target: Path = None) -> None:
        super().__init__()
        self.setWindowTitle("KlamAV")
        self.resize(900, 600)
        self.setMinimumSize(700, 400) # Impedisce di restringere troppo la finestra
        self.setWindowIcon(_app_icon())

        self.setStyleSheet(KDE_STYLESHEET)

        QApplication.instance().setOrganizationName("KlamAV")
        QApplication.instance().setApplicationName("KlamAV")
        self.settings = QSettings()

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(_app_icon())
        self.tray_icon.setToolTip("KlamAV - Protezione Attiva")

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
        self._load_schedule()

        self.fs_watcher = QFileSystemWatcher()
        self.fs_watcher.directoryChanged.connect(self._on_dir_changed)
        self._pending_realtime_scans = {}
        self._dir_snapshots = {}
        self._realtime_queue = []
        self.realtime_worker = None
        self._current_realtime_target = ""
        self._load_realtime()

        self._check_clamd(saved_socket)

        if self.settings.value("startup_update", True, type=bool):
            QTimer.singleShot(1500, self.update_page._start_update)

        # Se avviata con un target (es. da Dolphin in prima istanza), avvia la scansione
        if scan_target:
            self.scan_page.start_external_scan(scan_target)

    def setup_ipc(self, server: QLocalServer):
        """Configura il server IPC per ricevere file da scansionare da altre istanze."""
        self.ipc_server = server
        self.ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self):
        """Chiamato quando una seconda istanza invia un file da scansionare."""
        client = self.ipc_server.nextPendingConnection()
        if client:
            client.waitForReadyRead(1000)
            data = client.readAll().data().decode('utf-8')
            client.disconnectFromServer()

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
            self.tray_icon.showMessage("KlamAV", "L'applicazione continua a girare nella system tray.", _app_icon(), 3000)

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
                project_root = Path(__file__).parent.parent.parent
                python_exec = sys.executable

                content = f"""[Desktop Entry]
Name=KlamAV
Comment=Antivirus frontend for ClamAV
Exec={python_exec} -m klamav_py.gui.app
Path={project_root}
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
        if self.bg_worker is not None: return
        target_str = self.settings.value("schedule_target", str(Path.home()))
        target = Path(target_str)
        if not target.exists(): return

        self.tray_icon.showMessage("KlamAV", "Avvio scansione automatica in background...", _app_icon(), 3000)
        self.bg_worker = ScanWorker(
            socket_path=self.settings.value("socket_path", DEFAULT_SOCKET),
            target=target,
            quarantine_dir=Path(self.settings.value("quarantine_dir", str(DEFAULT_QUARANTINE_DIR))),
            auto_quarantine=self.settings.value("auto_quarantine", False, type=bool)
        )
        self.bg_worker.finished_scan.connect(self._on_bg_finished)
        self.bg_worker.start()

    def _on_bg_finished(self, scanned: int, infections: int, errors: int) -> None:
        status = f"Scansione automatica completata: {infections} infetti trovati."
        self.tray_icon.showMessage("KlamAV", status, _icon("emblem-virus" if infections > 0 else "emblem-checked"), 5000)

        target_str = self.settings.value("schedule_target", str(Path.home()))
        self.history_manager.add_entry("Programmata", target_str, scanned, infections, errors)
        self.history_page.refresh()

        self.bg_worker = None

    def _load_realtime(self) -> None:
        paths = self.settings.value("realtime_paths", [])
        if isinstance(paths, str): paths = [paths]

        old_paths = self.fs_watcher.directories()
        if old_paths:
            self.fs_watcher.removePaths(old_paths)

        if paths:
            self.fs_watcher.addPaths(paths)
            for p in paths:
                self._update_snapshot(p)

    def _update_snapshot(self, dir_path: str) -> None:
        snap = {}
        try:
            for entry in os.scandir(dir_path):
                if entry.is_file():
                    snap[entry.path] = entry.stat().st_mtime
        except OSError:
            pass
        self._dir_snapshots[dir_path] = snap

    def _on_dir_changed(self, dir_path: str) -> None:
        old_snap = self._dir_snapshots.get(dir_path, {})
        new_snap = {}
        try:
            for entry in os.scandir(dir_path):
                if entry.is_file():
                    new_snap[entry.path] = entry.stat().st_mtime
        except OSError:
            return

        for fpath, mtime in new_snap.items():
            if fpath not in old_snap or old_snap[fpath] != mtime:
                self._schedule_realtime_scan(fpath)

        self._dir_snapshots[dir_path] = new_snap

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
        self.realtime_worker.start()

    def _on_realtime_result(self, result: ScanResult) -> None:
        if result.infected:
            self.realtime_page.add_log_entry(Path(result.path).name, True, result.signature)
            self.tray_icon.showMessage(
                "KlamAV - MINACCIA RILEVATA!",
                f"{Path(result.path).name} è infetto ed è stato messo in quarantena.",
                _icon("emblem-virus"),
                5000
            )

    def _on_realtime_finished(self, scanned: int, infections: int, errors: int) -> None:
        self.history_manager.add_entry("Real-Time", self._current_realtime_target, scanned, infections, errors)
        self.history_page.refresh()

        self.realtime_worker = None
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
        self._ping_worker = None
