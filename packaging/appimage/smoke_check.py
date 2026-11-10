"""Controlli dentro l'AppImage, eseguiti con il suo Python (smoke-test.sh).

APPIMAGE e APPDIR vengono dall'ambiente, come li imposterebbe il runtime;
HOME, XDG_CONFIG_HOME e XDG_RUNTIME_DIR sono cartelle temporanee.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.environ["APPDIR"], "usr", "share", "klamav-py"))

from PySide6.QtGui import QIcon, QImageReader  # noqa: E402
from PySide6.QtWidgets import QApplication, QGroupBox  # noqa: E402

app = QApplication(sys.argv)

from klamav_py import __version__, appimage  # noqa: E402
from klamav_py.gui import main_window as mw  # noqa: E402

assert appimage.is_appimage(), "marcatore «appimage» assente"
assert appimage.launcher() == os.environ["APPIMAGE"], appimage.launcher()
# I file .desktop generati puntano al file .AppImage, non al mount temporaneo.
cmd = mw._gui_relaunch_command()
assert cmd == appimage.desktop_exec_arg(os.environ["APPIMAGE"]), cmd
assert "/tmp/.mount_" not in cmd and "usr/python" not in cmd, cmd

# Niente timer di sistema: gruppo dell'orario nascosto, nota al suo posto.
assert mw.system_timer_available() is False
page = mw.SchedulerPage()
page.show()
assert not any(
    "klamav-scan.timer" in g.title() and g.isVisible() for g in page.findChildren(QGroupBox)
), "gruppo del timer di sistema visibile"
assert page.no_system_timer_label.isVisible()
page.hide()

assert not QIcon(str(mw._BUNDLED_ICON_PATH)).pixmap(64, 64).isNull(), "icona SVG non caricata"
formats = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
assert "svg" in formats, formats

import ssl  # noqa: E402,F401  controllo aggiornamenti via HTTPS
print(f"AppImage di KlamAV-Py {__version__}: controlli OK")
