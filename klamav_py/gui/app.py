"""
Entry point della GUI: `python3 -m klamav_py.gui.app` oppure via lo
script installato `klamav-py-gui` (vedi setup/pyproject se lo aggiungi).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtNetwork import QLocalServer

from .main_window import (
    APP_NAME,
    DEFAULT_QUARANTINE_DIR,
    DEFAULT_SOCKET,
    MainWindow,
    _migrate_legacy_settings,
)
from .single_instance import ipc_socket_path, notify_running_instance


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
    # Socket nella runtime directory dell'utente, mai in /tmp, e verifica
    # del peer prima di inviare qualunque dato: vedi single_instance.py
    # per l'attacco (squatting di /tmp/klamav_py_ipc da parte di un altro
    # utente locale) che questo schema chiude.
    ipc_path = ipc_socket_path()
    payload = str(args.scan_target).encode("utf-8") if args.scan_target else None
    if ipc_path is not None and notify_running_instance(ipc_path, payload):
        # Un'istanza dello stesso utente è attiva e ha ricevuto il target.
        return 0

    # Siamo la prima istanza. Se il server IPC non può partire, la GUI
    # parte COMUNQUE, senza single-instance, e l'utente viene avvisato:
    # rifiutare l'avvio consegnerebbe a chi causa il fallimento proprio
    # il blocco dell'antivirus che questo codice deve impedire.
    ipc_server: QLocalServer | None = None
    ipc_error: str | None = None
    if ipc_path is None:
        ipc_error = (
            "nessuna directory runtime sicura disponibile "
            "(XDG_RUNTIME_DIR assente o non valida)"
        )
    else:
        # Rimuove un socket morto lasciato da un'istanza precedente
        # terminata male: nella runtime directory può essere solo nostro.
        QLocalServer.removeServer(ipc_path)
        server = QLocalServer(app)
        # Su Linux, senza setSocketOptions(), i permessi del socket UNIX
        # dipendono dallo umask del processo (documentato da Qt): con uno
        # umask permissivo (es. 022, comune di default) altri UTENTI del
        # sistema — non solo altri processi tuoi — potrebbero connettersi
        # al socket. UserAccessOption forza esplicitamente l'accesso al
        # solo utente proprietario, indipendentemente dallo umask attivo.
        # Con il socket nella runtime directory (0700) è ridondante, ma
        # resta valido se il percorso dovesse cambiare.
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if server.listen(ipc_path):
            ipc_server = server
        else:
            ipc_error = server.errorString() or "listen() non riuscita"
            server.deleteLater()
    # ------------------------------------

    window = MainWindow(
        socket_path=args.socket,
        quarantine_dir=args.quarantine_dir,
        scan_target=args.scan_target
    )
    if ipc_server is not None:
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

    if ipc_server is None:
        _warn_no_single_instance(window, ipc_error or "motivo sconosciuto")

    return app.exec()


def _warn_no_single_instance(window: MainWindow, reason: str) -> None:
    """
    Avviso quando il single-instance non è attivo. Senza, due istanze
    possono girare insieme (quarantene e scansioni programmate
    concorrenti) e "Scansiona con KlamAV-Py" da Dolphin apre una nuova
    istanza invece di passare il file a questa.
    """
    print(f"{APP_NAME}: controllo di istanza singola non attivo: {reason}", file=sys.stderr)
    text = (
        "Il controllo di istanza singola non è attivo, quindi un secondo "
        f"avvio di {APP_NAME} aprirebbe un'altra copia dell'applicazione.\n\n"
        f"Motivo: {reason}"
    )
    if window.tray_icon.isVisible():
        window.tray_icon.showMessage(APP_NAME, text, window.windowIcon(), 10000)
    else:
        QMessageBox.warning(window, APP_NAME, text)


if __name__ == "__main__":
    sys.exit(main())
