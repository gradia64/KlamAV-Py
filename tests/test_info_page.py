"""
Pagina Info nel menu laterale, controllo della versione nella pagina
Aggiornamenti, Impostazioni senza il gruppo Informazioni (0.2.0).
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QGroupBox  # noqa: E402

import klamav_py.gui.info_page as info  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py import __version__  # noqa: E402
from klamav_py.clamd_client import ClamdEndpoint  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def isolated(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path / "config"))


def _groups(widget):
    return [g.title() for g in widget.findChildren(QGroupBox)]


def test_manutentore_e_repository_come_nel_pacchetto():
    control = (ROOT / "debian/control").read_text(encoding="utf-8")
    assert re.search(r"^Maintainer: (.+)$", control, re.M).group(1) == (
        f"{info.MAINTAINER_NAME} <{info.MAINTAINER_EMAIL}>")
    assert re.search(r"^Homepage: (.+)$", control, re.M).group(1) == info.REPOSITORY_URL


def test_pagina_info_mostra_versione_manutentore_e_link(isolated):
    page = info.InfoPage()
    assert page.name_label.text() == f"KlamAV-Py {__version__}"
    assert info.MAINTAINER_EMAIL in page.maintainer_label.text()
    assert f"mailto:{info.MAINTAINER_EMAIL}" in page.maintainer_label.text()
    for label in (page.repository_label, page.license_label, page.maintainer_label):
        assert label.textFormat() == Qt.RichText and label.openExternalLinks()
    assert info.REPOSITORY_URL in page.repository_label.text()
    assert _groups(page) == ["Programma", "Manutentore", "Componenti"]


def test_aggiornamenti_ha_programma_e_database(isolated):
    page = mw.UpdatePage(lambda: ClamdEndpoint())
    assert _groups(page) == ["Aggiornamento del programma", "Aggiornamento database virus"]
    assert page.app_update.check_update_btn.text() == "Controlla aggiornamenti"
    assert __version__ in page.app_update.version_label.text()


def test_impostazioni_senza_informazioni(isolated):
    page = mw.SettingsPage()
    assert "Informazioni" not in _groups(page)
    for name in ("check_update_btn", "version_label", "update_status_label",
                 "db_status_label", "_update_check_worker", "_check_updates"):
        assert not hasattr(page, name), name
    # La preferenza del controllo all'avvio resta fra le impostazioni.
    assert page.auto_check_updates.text() == "Controlla aggiornamenti all'avvio"


def test_info_e_l_ultima_voce_del_menu():
    src = (ROOT / "klamav_py/gui/main_window.py").read_text(encoding="utf-8")
    voci = re.findall(r'self\._add_sidebar_item\("([^"]+)"', src)
    pagine = re.findall(r"self\.content_stack\.addWidget\(self\.(\w+)\)", src)
    assert voci[-1] == "Info" and pagine[-1] == "info_page"
    assert len(voci) == len(pagine)
    assert voci.index("Aggiornamenti") == pagine.index("update_page")
