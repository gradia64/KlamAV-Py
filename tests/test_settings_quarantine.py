"""
Flusso di salvataggio delle Impostazioni per la directory di quarantena, e
regola inversa al salvataggio della Pianificazione.

Le finestre di dialogo sono sostituite da risposte registrate: si verifica
l'ORDINE delle operazioni (validazione, conferme, directory, QSettings),
non l'aspetto dei dialoghi.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

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


class Dialogs:
    """Registra i dialoghi mostrati e risponde a question() con answers."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.shown: list[tuple[str, str]] = []

    def question(self, parent, title, text, *a, **k):
        self.shown.append(("question", text))
        return QMessageBox.Yes if self.answers.pop(0) else QMessageBox.No

    def warning(self, parent, title, text, *a, **k):
        self.shown.append(("warning", text))

    def information(self, parent, title, text, *a, **k):
        self.shown.append(("information", text))

    def kinds(self):
        return [k for k, _ in self.shown]


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    # Un file di impostazioni per test: su Linux QSettings(org, app) usa
    # NativeFormat, che ha un percorso distinto da IniFormat.
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))

    default = home / ".local/share/klamav-py/quarantine"
    # Validazioni della Pianificazione in linea invece che in un thread:
    # i test verificano l'esito subito dopo la chiamata. Il percorso con il
    # thread vero è in test_settings_exclusions::test_validazione_fuori_dal_thread_gui.
    monkeypatch.setattr(mw, "run_off_gui_thread", _inline)
    monkeypatch.setattr(mw, "DEFAULT_QUARANTINE_DIR", default)
    # tmp_path sta sotto /tmp: senza neutralizzare le radici volatili ogni
    # directory di prova verrebbe (giustamente) rifiutata.
    monkeypatch.setattr(
        mw, "decide_quarantine_dir",
        lambda raw, **kw: decide(raw, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=(), **kw),
    )

    def dialogs(*answers):
        d = Dialogs(answers)
        for name in ("question", "warning", "information"):
            monkeypatch.setattr(QMessageBox, name, staticmethod(getattr(d, name)))
        return d

    return SimpleNamespace(home=home, tmp=tmp_path, default=default, dialogs=dialogs)


def _settings_page(text):
    page = mw.SettingsPage()
    page.quar_edit.setText(text)
    return page


def _saved():
    return QSettings(mw.APP_NAME, mw.APP_NAME).value("quarantine_dir")


# -- Impostazioni --------------------------------------------------------

def test_volatile_rifiutata_e_nulla_salvato(env, monkeypatch):
    monkeypatch.setattr(mw, "decide_quarantine_dir", decide)  # regola vera
    d = env.dialogs()
    _settings_page("/var/tmp/q")._save_settings()
    assert d.kinds() == ["warning"] and "/var/tmp" in d.shown[0][1]
    assert _saved() is None


def test_relativa_rifiutata(env):
    d = env.dialogs()
    _settings_page("quarantena")._save_settings()
    assert d.kinds() == ["warning"] and _saved() is None


def test_dentro_home_salvata_e_privata(env):
    env.dialogs()
    page = _settings_page("~/Quarantena")
    page._save_settings()
    q = env.home / "Quarantena"
    assert _saved() == str(q) and page.quar_edit.text() == str(q)
    assert q.is_dir() and q.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("risposta", [False, True])
def test_permessi_larghi_chiedono_conferma(env, risposta):
    q = env.home / "Condivisa"
    q.mkdir()
    q.chmod(0o755)
    d = env.dialogs(risposta)
    _settings_page(str(q))._save_settings()
    assert d.kinds()[0] == "question" and "755" in d.shown[0][1]
    assert (_saved() == str(q)) is risposta
    assert (q.stat().st_mode & 0o777) == (0o700 if risposta else 0o755)


def test_vecchia_quarantena_non_vuota_chiede_conferma(env):
    env.dialogs()
    _settings_page("~/Vecchia")._save_settings()
    vecchia = mw.Quarantine(env.home / "Vecchia")
    infetto = env.home / "infetto.txt"
    infetto.write_text("x")
    vecchia.quarantine_file(infetto, "Eicar-Test-Signature")

    d = env.dialogs(False)
    _settings_page("~/Nuova")._save_settings()
    assert d.kinds() == ["question"] and "1 file" in d.shown[0][1]
    assert _saved() == str(env.home / "Vecchia")


# -- quarantena attiva nella GUI -----------------------------------------

def test_cambio_directory_applicato_a_scansione_manuale_e_pagina(env, tmp_path):
    # Dalla 0.1.15 la nuova Quarantine si crea nel worker della pagina
    # Quarantena (test_quarantine_loader): qui si verifica che entrambe le
    # pagine passino alla nuova directory.
    env.dialogs()
    scan_page = mw.ScanPage(mw.ClamdEndpoint(), tmp_path / "vecchia", mw.HistoryManager(tmp_path / "h.json"))
    loaded = []
    fake = SimpleNamespace(
        settings=QSettings(mw.APP_NAME, mw.APP_NAME),
        scan_page=scan_page,
        quarantine_page=SimpleNamespace(load=loaded.append),
    )
    fake.settings.setValue("quarantine_dir", str(tmp_path / "nuova"))
    mw.MainWindow._apply_quarantine_dir(fake)
    assert scan_page.quarantine_dir == tmp_path / "nuova" and scan_page.quarantine is None
    assert loaded == [tmp_path / "nuova"]


def test_quarantena_che_contiene_la_cartella_pianificata_rifiutata(env):
    target = env.tmp / "dati" / "progetti"
    target.mkdir(parents=True)
    s = QSettings(mw.APP_NAME, mw.APP_NAME)
    s.setValue("schedule_target", str(target))
    s.sync()
    d = env.dialogs()
    _settings_page(str(env.tmp / "dati"))._save_settings()
    assert d.kinds() == ["warning"] and "pianificazione interna" in d.shown[0][1]
    assert _saved() is None


# -- pianificazione ------------------------------------------------------

def _scheduler(enabled=True):
    page = mw.SchedulerPage()
    page.enable_check.setChecked(enabled)
    return page


def _schedule_enabled():
    return QSettings(mw.APP_NAME, mw.APP_NAME).value("schedule_enabled", False, type=bool)


def test_attivare_la_pianificazione_non_chiede_nulla(env):
    d = env.dialogs()
    _scheduler()._save_schedule()
    assert d.kinds() == [] and _schedule_enabled()


def test_disattivare_la_pianificazione_non_chiede_nulla(env):
    d = env.dialogs()
    _scheduler(enabled=False)._save_schedule()
    assert d.kinds() == [] and not _schedule_enabled()


def test_cartella_pianificata_dentro_la_quarantena_rifiutata(env):
    # La regola inversa, al salvataggio della Pianificazione.
    q = env.tmp / "q"
    (q / "dentro").mkdir(parents=True)
    s = QSettings(mw.APP_NAME, mw.APP_NAME)
    s.setValue("quarantine_dir", str(q))
    s.sync()
    d = env.dialogs()
    page = _scheduler()
    page.target_edit.setText(str(q / "dentro"))
    page._save_schedule()
    assert d.kinds() == ["warning"] and "non è stata salvata" in d.shown[0][1]
    assert not QSettings(mw.APP_NAME, mw.APP_NAME).contains("schedule_target")


def test_cartella_pianificata_fuori_dalla_quarantena_salvata(env):
    (env.tmp / "dati").mkdir()
    env.dialogs()
    page = _scheduler()
    page.target_edit.setText(str(env.tmp / "dati"))
    page._save_schedule()
    assert QSettings(mw.APP_NAME, mw.APP_NAME).value("schedule_target") == str(env.tmp / "dati")
