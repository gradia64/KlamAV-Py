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

from .main_window import DEFAULT_QUARANTINE_DIR, DEFAULT_SOCKET, MainWindow

def main() -> int:
    parser = argparse.ArgumentParser(prog="klamav-py-gui")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument("--quarantine-dir", type=Path, default=DEFAULT_QUARANTINE_DIR)
    parser.add_argument("--scan-target", type=Path, default=None)
    args = parser.parse_args()

    app = QApplication(sys.argv)

    app.setApplicationName("KlamAV")
    app.setOrganizationName("KlamAV")
    app.setDesktopFileName("org.kde.klamav")
    app.setQuitOnLastWindowClosed(False)

    # --- SISTMA SINGLE INSTANCE E IPC ---
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
    ipc_server.listen("klamav_py_ipc")
    # ------------------------------------

    window = MainWindow(
        socket_path=args.socket,
        quarantine_dir=args.quarantine_dir,
        scan_target=args.scan_target
    )
    # Passa il server IPC alla finestra
    window.setup_ipc(ipc_server)

    settings = QSettings()
    start_in_tray = settings.value("start_in_tray", False, type=bool)

    if start_in_tray and not args.scan_target:
        if window.tray_icon.isVisible():
            window.tray_icon.showMessage("KlamAV", "L'applicazione è in esecuzione in background.", window.windowIcon(), 3000)
    else:
        window.show()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
