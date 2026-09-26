"""
Flusso di salvataggio delle Impostazioni per la directory di quarantena, ed
esclusione reciproca fra pianificazione interna e klamav-scan.timer.

Le finestre di dialogo sono sostituite da risposte registrate e systemctl
da funzioni finte: si verifica l'ORDINE delle operazioni (validazione,
conferme, directory, drop-in, QSettings), che è ciò che impedisce a GUI e
timer di divergere, non l'aspetto dei dialoghi.
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
from klamav_py.systemd_dropin import HEADER, dropin_path  # noqa: E402

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


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
    monkeypatch.setattr(mw, "DEFAULT_QUARANTINE_DIR", default)
    # tmp_path sta sotto /tmp: senza neutralizzare le radici volatili ogni
    # directory di prova verrebbe (giustamente) rifiutata.
    monkeypatch.setattr(
        mw, "decide_quarantine_dir",
        lambda raw: decide(raw, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=()),
    )

    calls = SimpleNamespace(reload=0, reload_result=None, disable=0, disable_result=None, timer=False)

    def fake_reload():
        calls.reload += 1
        return calls.reload_result

    def fake_disable():
        calls.disable += 1
        return calls.disable_result

    monkeypatch.setattr(mw, "daemon_reload", fake_reload)
    monkeypatch.setattr(mw, "disable_timer", fake_disable)
    monkeypatch.setattr(mw, "timer_enabled", lambda: calls.timer)
    # I drop-in reali della macchina (es. un override.conf dello sviluppatore)
    # non devono entrare nei test: vedi test_systemd_dropin per il rilevamento.
    monkeypatch.setattr(mw, "foreign_overrides", lambda **kw: [])

    def dialogs(*answers):
        d = Dialogs(answers)
        for name in ("question", "warning", "information"):
            monkeypatch.setattr(QMessageBox, name, staticmethod(getattr(d, name)))
        return d

    return SimpleNamespace(home=home, tmp=tmp_path, default=default, calls=calls,
                           dialogs=dialogs, dropin=dropin_path())


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
    assert _saved() is None and not env.dropin.exists()


def test_relativa_rifiutata(env):
    d = env.dialogs()
    _settings_page("quarantena")._save_settings()
    assert d.kinds() == ["warning"] and _saved() is None


def test_dentro_home_dropin_senza_readwritepaths(env):
    env.dialogs()
    page = _settings_page("~/Quarantena")
    page._save_settings()
    q = env.home / "Quarantena"
    assert _saved() == str(q) and page.quar_edit.text() == str(q)
    assert q.is_dir() and q.stat().st_mode & 0o777 == 0o700
    text = env.dropin.read_text()
    assert text.startswith(HEADER) and "ReadWritePaths" not in text
    assert env.calls.reload == 1


def test_fuori_home_dropin_con_readwritepaths(env):
    env.dialogs()
    _settings_page(str(env.tmp / "esterna" / "q"))._save_settings()
    assert "ReadWritePaths=" in env.dropin.read_text()


def test_ritorno_al_default_rimuove_il_dropin(env):
    env.dialogs()
    _settings_page("~/Quarantena")._save_settings()
    _settings_page(str(env.default))._save_settings()
    assert not env.dropin.exists() and _saved() == str(env.default)
    assert env.calls.reload == 2


def test_nessun_reload_se_il_dropin_non_cambia(env):
    env.dialogs()
    _settings_page(str(env.default))._save_settings()
    assert env.calls.reload == 0


def test_dropin_estraneo_blocca_il_salvataggio(env):
    env.dropin.parent.mkdir(parents=True)
    env.dropin.write_text("[Service]\nNice=5\n")
    d = env.dialogs()
    _settings_page("~/Quarantena")._save_settings()
    assert d.kinds() == ["warning"] and _saved() is None
    assert env.dropin.read_text() == "[Service]\nNice=5\n"


def test_reload_fallito_e_solo_un_avviso(env):
    env.calls.reload_result = "Failed to connect to bus"
    env.dialogs()
    page = _settings_page("~/Quarantena")
    page._save_settings()
    assert _saved() == str(env.home / "Quarantena")
    assert page.save_notes and "Failed to connect to bus" in page.save_notes[0]


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
    assert env.dropin.exists() is risposta


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
    env.dialogs()
    vecchia = mw.Quarantine(tmp_path / "vecchia")
    refreshed = []
    fake = SimpleNamespace(
        settings=QSettings(mw.APP_NAME, mw.APP_NAME),
        scan_page=SimpleNamespace(quarantine=vecchia),
        quarantine_page=SimpleNamespace(quarantine=vecchia, refresh=lambda: refreshed.append(1)),
    )
    fake.settings.setValue("quarantine_dir", str(tmp_path / "nuova"))
    mw.MainWindow._apply_quarantine_dir(fake)
    assert fake.scan_page.quarantine.dir == tmp_path / "nuova"
    assert fake.quarantine_page.quarantine is fake.scan_page.quarantine
    assert refreshed == [1]


# -- pianificazione ------------------------------------------------------

def _scheduler(enabled=True):
    page = mw.SchedulerPage()
    page.enable_check.setChecked(enabled)
    return page


def _schedule_enabled():
    return QSettings(mw.APP_NAME, mw.APP_NAME).value("schedule_enabled", False, type=bool)


def test_timer_attivo_e_rifiuto_nessun_salvataggio(env):
    env.calls.timer = True
    d = env.dialogs(False)
    _scheduler()._save_schedule()
    assert d.kinds() == ["question"] and not _schedule_enabled()
    assert env.calls.disable == 0


def test_timer_attivo_e_conferma_lo_disattiva(env):
    env.calls.timer = True
    env.dialogs(True)
    _scheduler()._save_schedule()
    assert env.calls.disable == 1 and _schedule_enabled()


def test_disattivazione_fallita_nessun_salvataggio(env):
    env.calls.timer = True
    env.calls.disable_result = "Access denied"
    d = env.dialogs(True)
    _scheduler()._save_schedule()
    assert d.kinds() == ["question", "warning"] and not _schedule_enabled()


@pytest.mark.parametrize("stato", [False, None])
def test_timer_non_attivo_o_sconosciuto_non_blocca(env, stato):
    env.calls.timer = stato
    d = env.dialogs()
    _scheduler()._save_schedule()
    assert d.kinds() == [] and _schedule_enabled()


def test_disattivare_la_pianificazione_non_chiede_nulla(env):
    env.calls.timer = True
    d = env.dialogs()
    _scheduler(enabled=False)._save_schedule()
    assert d.kinds() == [] and env.calls.disable == 0


def test_label_timer(env):
    page = _scheduler()
    env.calls.timer = True
    assert page.refresh_system_timer() is True
    assert not page.system_timer_label.isHidden()
    env.calls.timer = None
    page.refresh_system_timer()
    assert page.system_timer_label.isHidden()
