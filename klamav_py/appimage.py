"""Esecuzione da AppImage: come riconoscerla e cosa cambia.

L'AppImage contiene il pacchetto con un marcatore scritto dalla build
(``_packaging``, contenuto «appimage»; tools/build-appimage.sh). Il
marcatore vale anche per un'AppImage estratta (``--appimage-extract``),
che non ha APPIMAGE e APPDIR nell'ambiente. La sola variabile APPIMAGE non
basta: la ereditano anche i programmi avviati da un'AppImage (un terminale,
per esempio), e un KlamAV-Py del .deb lanciato da lì non è un'AppImage.

Cosa cambia:

- **Timer di sistema**: le unit (klamav-scan.service, .timer,
  klamav-scan-notify.service) le installano il .deb e il pacchetto Arch in
  /usr/lib/systemd/user, con ExecStart=/usr/bin/klamav-py. L'AppImage non
  installa nulla: la GUI non scrive drop-in, non interroga systemctl
  --user per il timer e non propone l'orario. Resta la pianificazione
  interna, che gira solo con la GUI aperta (SYSTEM_TIMER_NOTE).
- **File .desktop generati** (avvio automatico, menu di Dolphin): il
  Python dell'AppImage vive nel mount temporaneo /tmp/.mount_*, che sparisce
  alla chiusura. Il comando è il file .AppImage (``launcher``).

Nessuna dipendenza da Qt: lo usano anche CLI e test senza PySide6.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

MARKER_NAME = "_packaging"
PACKAGE_DIR = Path(__file__).resolve().parent

SYSTEM_TIMER_NOTE = (
    "Con l'AppImage la scansione programmata di sistema (klamav-scan.timer) "
    "non è disponibile: le sue unit systemd le installano solo il pacchetto "
    ".deb e quello di Arch. Resta la pianificazione interna qui sotto, che "
    "gira solo con KlamAV-Py aperto (anche nella system tray)."
)


def is_appimage(package_dir: Path | None = None) -> bool:
    """True se questo pacchetto è quello di un'AppImage (marcatore della build)."""
    marker = (package_dir or PACKAGE_DIR) / MARKER_NAME
    try:
        return marker.read_text(encoding="utf-8").strip() == "appimage"
    except (OSError, UnicodeDecodeError):
        return False


def launcher(env: Mapping[str, str] | None = None, package_dir: Path | None = None) -> str | None:
    """
    Percorso stabile da cui rilanciare l'AppImage, o None se il pacchetto
    non è un'AppImage.

    Il file .AppImage (APPIMAGE) quando il pacchetto sta davvero sotto
    APPDIR, cioè nel mount di questa esecuzione; altrimenti (AppImage
    estratta, variabili ereditate da un'altra AppImage) l'AppRun accanto al
    pacchetto: <AppDir>/usr/share/klamav-py/klamav_py → <AppDir>/AppRun.
    None anche quando nessuno dei due esiste.
    """
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    if not is_appimage(package_dir):
        return None
    env = os.environ if env is None else env
    image, appdir = env.get("APPIMAGE"), env.get("APPDIR")
    if image and appdir:
        try:
            inside = package_dir.is_relative_to(Path(appdir).resolve())
        except OSError:
            inside = False
        if inside and Path(image).is_absolute():
            return image
    apprun = package_dir.parents[3] / "AppRun"
    if apprun.is_file():
        return str(apprun)
    return None


# Caratteri che in Exec= impongono le virgolette (Desktop Entry
# Specification, «The Exec key»).
_EXEC_RESERVED = set(" \t\n\"'\\><~|&;$*?#()`")


def desktop_exec_arg(arg: str) -> str:
    """
    Un argomento come va scritto nella chiave Exec= di un file .desktop.

    Fra virgolette se contiene un carattere riservato, con \\ davanti a
    " ` $ e \\; poi l'escape delle stringhe del formato, che si applica
    prima del quoting (ogni \\ diventa \\\\), e % raddoppiato perché non
    sia letto come un codice di campo (%F, %U…).
    """
    if arg and not (set(arg) & _EXEC_RESERVED):
        quoted = arg
    else:
        inner = "".join("\\" + c if c in '"`$\\' else c for c in arg)
        quoted = f'"{inner}"'
    return quoted.replace("\\", "\\\\").replace("%", "%%")
