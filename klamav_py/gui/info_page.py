"""Pagina Info: chi è il programma, la sua licenza e chi lo mantiene.

I dati del manutentore e l'indirizzo del repository sono gli stessi di
debian/control (Maintainer, Homepage): tests/test_info_page.py li confronta,
così una modifica al pacchetto non lascia la pagina indietro.
"""

from __future__ import annotations

import html
import platform

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import Qt, qVersion
from PySide6.QtWidgets import QGroupBox, QLabel, QVBoxLayout, QWidget

from .. import __version__

APP_TITLE = "KlamAV-Py"
DESCRIPTION = (
    "Frontend minimale per l'antivirus ClamAV, attraverso il demone clamd: "
    "scansioni a richiesta e pianificate, protezione in tempo reale, "
    "quarantena e aggiornamento delle firme. Erede spirituale di KlamAV 0.22."
)
LICENSE = "GNU General Public License v3.0 o successiva (GPL-3.0-or-later)"
LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"
REPOSITORY_URL = "https://github.com/gradia64/KlamAV-Py"
ISSUES_URL = REPOSITORY_URL + "/issues"
MAINTAINER_NAME = "gradia"
MAINTAINER_EMAIL = "gradia@disroot.org"


def _link(url: str, text: str | None = None) -> str:
    return (f"<a href='{html.escape(url, quote=True)}'>"
            f"{html.escape(text if text is not None else url)}</a>")


def _rich(text: str) -> QLabel:
    """Etichetta con link: rich text dichiarato (plain_labels non la tocca)."""
    label = QLabel(text)
    label.setTextFormat(Qt.RichText)
    label.setOpenExternalLinks(True)
    label.setTextInteractionFlags(Qt.TextBrowserInteraction)
    label.setWordWrap(True)
    return label


def _plain(text: str, style: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    if style:
        label.setStyleSheet(style)
    return label


class InfoPage(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 30)
        layout.setSpacing(15)

        title = _plain("Info", "font-size: 22px; font-weight: bold;")
        layout.addWidget(title)

        program = QGroupBox("Programma")
        program_layout = QVBoxLayout(program)
        self.name_label = _plain(f"{APP_TITLE} {__version__}", "font-size: 16px; font-weight: bold;")
        program_layout.addWidget(self.name_label)
        program_layout.addWidget(_plain(DESCRIPTION))
        self.license_label = _rich("Licenza: " + _link(LICENSE_URL, LICENSE))
        program_layout.addWidget(self.license_label)
        self.repository_label = _rich("Codice sorgente: " + _link(REPOSITORY_URL))
        program_layout.addWidget(self.repository_label)
        self.issues_label = _rich("Segnalazioni e richieste: " + _link(ISSUES_URL))
        program_layout.addWidget(self.issues_label)
        layout.addWidget(program)

        maintainer = QGroupBox("Manutentore")
        maintainer_layout = QVBoxLayout(maintainer)
        self.maintainer_label = _rich(
            html.escape(MAINTAINER_NAME) + " &lt;"
            + _link("mailto:" + MAINTAINER_EMAIL, MAINTAINER_EMAIL) + "&gt;"
        )
        maintainer_layout.addWidget(self.maintainer_label)
        layout.addWidget(maintainer)

        components = QGroupBox("Componenti")
        components_layout = QVBoxLayout(components)
        self.components_label = _plain(
            f"Python {platform.python_version()} · Qt {qVersion()} · PySide6 {PYSIDE_VERSION}",
            "font-size: 12px; color: palette(mid);",
        )
        components_layout.addWidget(self.components_label)
        components_layout.addWidget(_plain(
            "La versione di ClamAV e del database delle firme è nella pagina Aggiornamenti.",
            "font-size: 12px; color: palette(mid);",
        ))
        layout.addWidget(components)
        layout.addStretch()
