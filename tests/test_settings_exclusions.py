"""
Cartelle escluse nella pagina Pianificazione: una lista sola per la
pianificazione interna e per il timer di sistema, validata contro
entrambe le radici (scan_exclusions).

Il caso che motiva il salvataggio congiunto: un'esclusione valida con la
cartella di prima (una sottodirectory della home) diventa non valida se
la cartella interna viene spostata proprio lì. Lista e cartella si
validano insieme, qualunque delle due sia cambiata.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Documenti").mkdir(parents=True)
    (home / "vm").mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))

    calls = SimpleNamespace(timer=False, disable=0, shown=[], answers=[])
    monkeypatch.setattr(mw, "timer_enabled", lambda: calls.timer)
    monkeypatch.setattr(mw, "foreign_overrides", lambda **kw: [])

    def disable():
        calls.disable += 1
        return None

    monkeypatch.setattr(mw, "disable_timer", disable)

    def warning(parent, title, text, *a, **k):
        calls.shown.append(("warning", text))

    def question(parent, title, text, *a, **k):
        calls.shown.append(("question", text))
        return QMessageBox.Yes if calls.answers.pop(0) else QMessageBox.No

    monkeypatch.setattr(QMessageBox, "warning", staticmethod(warning))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return SimpleNamespace(home=home, tmp=tmp_path, calls=calls)


def _settings():
    return QSettings(mw.APP_NAME, mw.APP_NAME)


def _page(env, excludes=(), target=None):
    s = _settings()
    s.setValue("schedule_target", str(target or env.home))
    if excludes:
        s.setValue(mw.SCHEDULE_EXCLUDES_KEY, list(excludes))
    s.sync()
    return mw.SchedulerPage()


def _kinds(env):
    return [k for k, _ in env.calls.shown]


# -- chiave QSettings ----------------------------------------------------

@pytest.mark.parametrize(
    "valori",
    [[], ["/a"], ["/a", "/b"], ["/con, virgola", "/con spazi  finali ", "/àèìòù"]],
    ids=["vuota", "uno", "due", "caratteri"],
)
def test_round_trip(env, valori):
    s = _settings()
    s.setValue(mw.SCHEDULE_EXCLUDES_KEY, valori)
    s.sync()
    assert mw.load_schedule_excludes(_settings()) == valori


def test_chiave_assente(env):
    assert mw.load_schedule_excludes(_settings()) == []


def test_radici(env):
    assert mw.schedule_roots("", home=env.home) == {"timer di sistema": env.home}
    assert mw.schedule_roots("relativa", home=env.home) == {"timer di sistema": env.home}
    assert mw.schedule_roots("~/Documenti", home=env.home) == {
        "timer di sistema": env.home,
        "pianificazione interna": env.home / "Documenti",
    }


# -- pagina --------------------------------------------------------------

def test_caricamento_e_salvataggio(env):
    page = _page(env, [str(env.home / "vm")])
    assert page.excluded_dirs() == [str(env.home / "vm")]
    page._save_schedule()
    assert mw.load_schedule_excludes(_settings()) == [str(env.home / "vm")]


def test_aggiunta_da_sfoglia(env, monkeypatch):
    page = _page(env)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(env.home / "vm")))
    page._add_exclusion()
    assert page.excluded_dirs() == [str(env.home / "vm")]
    # Seconda volta: duplicato, nessuna voce nuova.
    page._add_exclusion()
    assert page.excluded_dirs() == [str(env.home / "vm")]


def test_aggiunta_rifiutata(env, monkeypatch):
    page = _page(env)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(env.home)))
    page._add_exclusion()
    assert page.excluded_dirs() == [] and _kinds(env) == ["warning"]


def test_modifica_con_testo_libero(env, monkeypatch):
    page = _page(env, [str(env.home / "vm")])
    page.excl_list.setCurrentRow(0)
    nuova = env.home / "non-ancora"
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (str(nuova), True)))
    page._edit_exclusion()
    assert page.excluded_dirs() == [str(nuova)]
    # Avviso (non errore): resta visibile sulla voce.
    assert "non esiste" in page.excl_list.item(0).toolTip()


def test_rimozione(env):
    page = _page(env, [str(env.home / "vm")])
    page.excl_list.setCurrentRow(0)
    page._remove_exclusion()
    assert page.excluded_dirs() == []
    assert not page.excl_remove_btn.isEnabled()


def test_forma_salvata_non_risolta(env):
    (env.home / "link-vm").symlink_to(env.home / "vm")
    page = _page(env)
    page._put_exclusion("~/link-vm")
    assert page.excluded_dirs() == [str(env.home / "link-vm")]


def test_avviso_ricalcolato_al_cambio_cartella(env):
    page = _page(env, [str(env.home / "vm")])
    item = page.excl_list.item(0)
    assert item.toolTip() == ""
    page.target_edit.setText(str(env.home / "Documenti"))
    assert "pianificazione interna" in item.toolTip()


def test_cambio_cartella_che_invalida_una_esclusione(env):
    # ~/Documenti è un'esclusione legittima con la cartella interna = home;
    # spostare la cartella interna proprio lì la rende non valida.
    page = _page(env, [str(env.home / "Documenti")])
    page.target_edit.setText(str(env.home / "Documenti"))
    assert "esclusa per intero" in page.excl_list.item(0).toolTip()
    page._save_schedule()
    assert _kinds(env) == ["warning"]
    assert _settings().value("schedule_target") == str(env.home)  # nulla salvato


def test_validazione_prima_della_domanda_sul_timer(env):
    # Con esclusioni non valide il timer non va disattivato: il
    # salvataggio fallirebbe comunque dopo.
    env.calls.timer = True
    page = _page(env, [str(env.home / "Documenti")], target=env.home / "Documenti")
    page.enable_check.setChecked(True)
    page._save_schedule()
    assert _kinds(env) == ["warning"] and env.calls.disable == 0


def test_voci_con_errore_segnalate_al_caricamento(env):
    # File di impostazioni modificato a mano: la voce resta visibile, con
    # l'errore, perché l'utente possa correggerla.
    page = _page(env, ["relativa"])
    item = page.excl_list.item(0)
    assert item.data(Qt.UserRole) == "relativa" and "assoluto" in item.toolTip()


# -- scansione programmata interna ---------------------------------------

def test_scansione_programmata_passa_le_esclusioni(env, monkeypatch):
    creati = []

    class Signal:
        def connect(self, *a):
            pass

    class FakeWorker:
        def __init__(self, **kw):
            creati.append(kw)
            for name in ("result_ready", "progress", "finished_scan", "quarantined", "quarantine_outcome"):
                setattr(self, name, Signal())

        def start(self):
            pass

    monkeypatch.setattr(mw, "ScanWorker", FakeWorker)
    s = _settings()
    s.setValue("schedule_target", "~/Documenti")
    s.setValue(mw.SCHEDULE_EXCLUDES_KEY, ["~/Documenti/archivio"])
    s.sync()
    fake = SimpleNamespace(
        bg_worker=None,
        scan_page=SimpleNamespace(worker=None),
        settings=_settings(),
        tray_icon=SimpleNamespace(showMessage=lambda *a: None),
        scheduler_page=SimpleNamespace(update_progress=lambda *a: None),
        _bg_log_close=lambda: None,
        _clamd_endpoint=lambda: None,
        _schedule_skip_noted=False,
        _schedule_missing_noted=False,
        _schedule_late_noted=False,
        _on_bg_result=None, _on_bg_progress=None, _on_bg_finished=None,
        _on_quarantine_changed=None, _on_bg_quarantine_outcome=None,
    )
    mw.MainWindow._run_scheduled_scan(fake)
    assert len(creati) == 1
    assert creati[0]["target"] == (env.home / "Documenti").resolve()
    assert creati[0]["exclude_dirs"] == ["~/Documenti/archivio"]
