"""
Entry point della GUI: `python3 -m klamav_py.gui.app` oppure via lo
script installato `klamav-py-gui` (vedi setup/pyproject se lo aggiungi).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QLocalSocket, QLocalServer

from .main_window import (
    APP_NAME,
    DEFAULT_QUARANTINE_DIR,
    DEFAULT_SOCKET,
    MainWindow,
    _migrate_legacy_settings,
)


def main() -> int:
    parser = argparse.ArgumentParser(prog="klamav-py-gui")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--quarantine-dir", type=Path, default=DEFAULT_QUARANTINE_DIR)
    parser.add_argument("--scan-target", type=Path, default=None)
    args = parser.parse_args()

    app = QApplication(sys.argv)

    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    # Deve coincidere ESATTAMENTE con il nome del file .desktop installato
    # (/usr/share/applications/klamav-py.desktop), senza estensione e
    # tutto minuscolo: è l'app_id che Wayland/KDE usa per abbinare
    # finestra, icona e raggruppamento in taskbar. Un maiuscolo di troppo
    # ("klamav-Py") rompe l'abbinamento quanto lo rompeva il vecchio
    # "org.kde.klamav".
    app.setDesktopFileName("klamav-py")
    app.setQuitOnLastWindowClosed(False)

    # Migrazione one-shot delle impostazioni legacy ("KlamAV" ->
    # "KlamAV-Py"): va fatta nella PRIMA istanza PRIMA di qualunque
    # lettura QSettings di questo file (qui sotto: start_in_tray) e
    # prima di MainWindow (che ha la sua chiamata difensiva — idempotente
    # per costruzione: se il file nuovo è già popolato, non tocca nulla).
    # Chiamarla qui rende l'ordine delle operazioni successive
    # irrilevante invece di dipendere dal caso che MainWindow venga
    # costruita prima della lettura.
    _migrate_legacy_settings()

    # --- SISTEMA SINGLE INSTANCE E IPC ---
    ipc_socket = QLocalSocket()
    ipc_socket.connectToServer("klamav_py_ipc")

    if ipc_socket.waitForConnected(500):
        # Un'altra istanza è già attiva. Invia il target e chiudi questa.
        if args.scan_target:
            ipc_socket.write(str(args.scan_target).encode('utf-8'))
            ipc_socket.waitForBytesWritten(1000)
        ipc_socket.disconnectFromServer()
        return 0

    # Siamo la prima istanza: creiamo il server IPC per ricevere future richieste
    QLocalServer.removeServer("klamav_py_ipc")
    ipc_server = QLocalServer(app)
    # Su Linux, senza setSocketOptions(), i permessi del socket UNIX
    # dipendono dallo umask del processo (documentato da Qt): con uno
    # umask permissivo (es. 022, comune di default) altri UTENTI del
    # sistema — non solo altri processi tuoi — potrebbero connettersi al
    # socket. UserAccessOption forza esplicitamente l'accesso al solo
    # utente proprietario, indipendentemente dallo umask attivo.
    ipc_server.setSocketOptions(QLocalServer.UserAccessOption)
    ipc_server.listen("klamav_py_ipc")
    # ------------------------------------

    window = MainWindow(
        socket_path=args.socket,
        quarantine_dir=args.quarantine_dir,
        scan_target=args.scan_target
    )
    # Passa il server IPC alla finestra
    window.setup_ipc(ipc_server)

    # QSettings SEMPRE espliciti (org/app), mai il default da
    # QApplication: la lettura non deve dipendere dall'ordine con cui
    # setOrganizationName/setApplicationName vengono chiamati rispetto
    # alla lettura stessa, né dalla migrazione (che comunque è già
    # avvenuta, vedi sopra).
    settings = QSettings(APP_NAME, APP_NAME)
    start_in_tray = settings.value("start_in_tray", False, type=bool)

    if start_in_tray and not args.scan_target:
        if window.tray_icon.isVisible():
            window.tray_icon.showMessage(
                APP_NAME, "L'applicazione è in esecuzione in background.", window.windowIcon(), 3000
            )
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
