"""
Esecuzione da AppImage (klamav_py/appimage.py).

Rispetto al .deb e al pacchetto Arch cambiano i file .desktop generati
(avvio automatico, menu di Dolphin): puntano al file .AppImage, mai al
Python del mount temporaneo /tmp/.mount_*, che sparisce alla chiusura.
Pianificazione e Impostazioni si salvano come negli altri pacchetti.

I simboli nuovi si cercano con importlib/getattr: sul commit precedente i
test falliscono per asserzione, non per un import.
"""

from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest


def _appimage_module():
    try:
        return importlib.import_module("klamav_py.appimage")
    except ImportError:
        pytest.fail("klamav_py.appimage assente: nessun riconoscimento dell'AppImage")


def _appdir(tmp_path, marker="appimage", apprun=True):
    """Un AppDir finto: <AppDir>/usr/share/klamav-py/klamav_py con il marcatore."""
    appdir = tmp_path / "squashfs-root"
    package = appdir / "usr/share/klamav-py/klamav_py"
    package.mkdir(parents=True)
    if marker is not None:
        (package / "_packaging").write_text(marker + "\n")
    if apprun:
        (appdir / "AppRun").write_text("#!/bin/sh\n")
    return appdir, package


# -- riconoscimento ------------------------------------------------------

def test_marcatore_riconosciuto(tmp_path):
    ai = _appimage_module()
    _, package = _appdir(tmp_path)
    assert ai.is_appimage(package) is True


@pytest.mark.parametrize("marker", [None, "deb", ""], ids=["assente", "deb", "vuoto"])
def test_senza_marcatore_non_e_appimage(tmp_path, marker):
    ai = _appimage_module()
    _, package = _appdir(tmp_path, marker=marker)
    assert ai.is_appimage(package) is False


def test_sorgente_del_repository_non_e_appimage():
    ai = _appimage_module()
    assert ai.is_appimage() is False, "il marcatore non va nel repository (.gitignore)"


def test_launcher_e_il_file_appimage(tmp_path):
    ai = _appimage_module()
    appdir, package = _appdir(tmp_path)
    env = {"APPIMAGE": "/home/u/Applicazioni/KlamAV-Py.AppImage", "APPDIR": str(appdir)}
    assert ai.launcher(env, package) == "/home/u/Applicazioni/KlamAV-Py.AppImage"


def test_variabili_ereditate_non_valgono(tmp_path):
    """APPIMAGE e APPDIR di un'altra AppImage (un terminale avviato da lì):
    il pacchetto non è sotto quell'APPDIR, quindi si usa l'AppRun del
    proprio AppDir."""
    ai = _appimage_module()
    appdir, package = _appdir(tmp_path)
    altro = tmp_path / "altra"
    altro.mkdir()
    env = {"APPIMAGE": "/opt/Altra.AppImage", "APPDIR": str(altro)}
    assert ai.launcher(env, package) == str(appdir / "AppRun")


def test_appimage_estratta_usa_apprun(tmp_path):
    ai = _appimage_module()
    appdir, package = _appdir(tmp_path)
    assert ai.launcher({}, package) == str(appdir / "AppRun")


def test_senza_marcatore_nessun_launcher(tmp_path):
    """Un .deb lanciato da un terminale aperto da un'AppImage ha APPIMAGE
    nell'ambiente, ma non è un'AppImage."""
    ai = _appimage_module()
    appdir, package = _appdir(tmp_path, marker=None)
    env = {"APPIMAGE": "/opt/Altra.AppImage", "APPDIR": str(appdir)}
    assert ai.launcher(env, package) is None


def test_senza_apprun_nessun_launcher(tmp_path):
    ai = _appimage_module()
    _, package = _appdir(tmp_path, apprun=False)
    assert ai.launcher({}, package) is None


# -- Exec= dei file .desktop ---------------------------------------------

@pytest.mark.parametrize(
    "arg, expected",
    [
        ("/usr/bin/klamav-py-gui", "/usr/bin/klamav-py-gui"),
        ("/home/u/My Apps/K.AppImage", '"/home/u/My Apps/K.AppImage"'),
        ('/a"b', '"/a\\\\"b"'),
        ("/a$b", '"/a\\\\$b"'),
        ("/a`b", '"/a\\\\`b"'),
        ("/a\\b", '"/a\\\\\\\\b"'),
        ("/100%/K", "/100%%/K"),
        ("", '""'),
    ],
    ids=["semplice", "spazio", "virgolette", "dollaro", "backtick", "backslash",
         "percento", "vuoto"],
)
def test_desktop_exec_arg(arg, expected):
    ai = _appimage_module()
    assert ai.desktop_exec_arg(arg) == expected


# -- GUI -------------------------------------------------------------------

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.quarantine_location import decide  # noqa: E402

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


def _inline(fn, callback):
    try:
        result = fn()
    except Exception as exc:  # come run_off_gui_thread
        result = exc
    callback(result)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _fake_appimage(monkeypatch, image="/home/u/My Apps/KlamAV-Py.AppImage"):
    ai = _appimage_module()
    monkeypatch.setattr(ai, "is_appimage", lambda package_dir=None: True)
    monkeypatch.setattr(ai, "launcher", lambda env=None, package_dir=None: image)


def test_comando_desktop_dall_appimage(monkeypatch):
    _fake_appimage(monkeypatch)
    # Anche con un klamav-py-gui di un'altra installazione nel PATH.
    monkeypatch.setattr(mw.shutil, "which", lambda name: "/usr/bin/klamav-py-gui")
    cmd = mw._gui_relaunch_command()
    assert cmd == '"/home/u/My Apps/KlamAV-Py.AppImage"'
    assert "/tmp/.mount_" not in cmd


def test_comando_desktop_senza_appimage_invariato(monkeypatch):
    ai = _appimage_module()
    monkeypatch.setattr(ai, "launcher", lambda env=None, package_dir=None: None)
    monkeypatch.setattr(mw.shutil, "which", lambda name: "/usr/bin/klamav-py-gui")
    assert mw._gui_relaunch_command() == "/usr/bin/klamav-py-gui"


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documenti").mkdir(parents=True)
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))
    monkeypatch.setattr(mw, "run_off_gui_thread", _inline)
    monkeypatch.setattr(mw, "DEFAULT_QUARANTINE_DIR", home / ".local/share/klamav-py/quarantine")
    # tmp_path sta sotto /tmp: la regola delle cartelle volatili la
    # rifiuterebbe.
    monkeypatch.setattr(
        mw, "decide_quarantine_dir",
        lambda raw, **kw: decide(raw, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=(), **kw),
    )

    calls = SimpleNamespace(shown=[])

    def dialog(kind):
        def show(parent, title, text, *a, **k):
            calls.shown.append((kind, text))
            return QMessageBox.Yes
        return staticmethod(show)

    for kind in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, kind, dialog(kind))
    return SimpleNamespace(home=home, calls=calls)


def _page(env):
    s = QSettings(mw.APP_NAME, mw.APP_NAME)
    s.setValue("schedule_target", str(env.home / "Documenti"))
    s.sync()
    return mw.SchedulerPage()


def test_salvataggio_della_pianificazione(env, monkeypatch):
    _fake_appimage(monkeypatch)
    page = _page(env)
    page.enable_check.setChecked(True)
    page.interval_spin.setValue(6)
    page._save_schedule()
    s = QSettings(mw.APP_NAME, mw.APP_NAME)
    assert s.value("schedule_enabled", False, type=bool) is True
    assert s.value("schedule_interval", 0, type=int) == 6
    assert env.calls.shown == []


def test_impostazioni_salvate(env, monkeypatch):
    _fake_appimage(monkeypatch)
    page = mw.SettingsPage()
    page.quar_edit.setText(str(env.home / "Quarantena"))
    page._save_settings()
    assert QSettings(mw.APP_NAME, mw.APP_NAME).value("quarantine_dir") == str(
        (env.home / "Quarantena").resolve()
    )
    assert [k for k, _ in env.calls.shown] == []
