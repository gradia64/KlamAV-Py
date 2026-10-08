"""
Rigenerazione all'avvio del drop-in di klamav-scan.service (0.1.15).

Un drop-in scritto da una versione precedente (0.1.13: senza --log-errors)
restava com'era finché l'utente non salvava Impostazioni o Pianificazione,
e niente glielo diceva. All'avvio la GUI lo riporta al testo che il
salvataggio produrrebbe con le impostazioni salvate, fuori dal thread della
GUI, e lo dice una volta. Un file non nostro (senza l'intestazione) non si
tocca mai.

L'ultima parte strumenta subprocess: nessuna chiamata a systemctl --user
deve partire dal thread della GUI, né all'avvio né dalla pagina
Pianificazione né dai due salvataggi.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
import klamav_py.systemd_dropin as sd  # noqa: E402
from klamav_py.quarantine_location import decide  # noqa: E402

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


def _inline(fn, callback):
    try:
        result = fn()
    except Exception as exc:  # come run_off_gui_thread
        result = exc
    callback(result)


def _dropin_0_1_13(quarantine) -> str:
    """Il drop-in come lo scriveva la 0.1.13 (EXEC_TEMPLATE senza
    --log-errors), per una quarantena dentro la home."""
    return (
        sd.HEADER + "\n"
        "# Scritto e rimosso dalle Impostazioni di klamav-py-gui; vedi klamav-py-gui(1).\n"
        "\n"
        "[Service]\n"
        "# Azzeramento obbligatorio: su Type=oneshot le ExecStart si sommano.\n"
        "ExecStart=\n"
        f'ExecStart=/usr/bin/klamav-py scan %h --quarantine "{quarantine}" --quiet\n'
    )


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def env(app, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(config))
    monkeypatch.setattr(mw, "DEFAULT_QUARANTINE_DIR", home / ".local/share/klamav-py/quarantine")
    monkeypatch.setattr(
        mw, "decide_quarantine_dir",
        lambda raw, **kw: decide(raw, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=(), **kw),
    )
    monkeypatch.setattr(mw, "foreign_overrides", lambda **kw: [])
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Yes))

    quarantine = home / "Quarantena"
    quarantine.mkdir()
    settings = QSettings(mw.APP_NAME, mw.APP_NAME)
    settings.setValue("quarantine_dir", str(quarantine))
    settings.sync()
    return SimpleNamespace(home=home, quarantine=quarantine, settings=settings,
                           dropin=sd.dropin_path())


@pytest.fixture
def inline(env, monkeypatch):
    """systemctl finto e valutazioni in linea: l'esito arriva subito."""
    calls = SimpleNamespace(reload=0, reload_result=None)

    def fake_reload():
        calls.reload += 1
        return calls.reload_result

    monkeypatch.setattr(mw, "run_off_gui_thread", _inline)
    monkeypatch.setattr(mw, "daemon_reload", fake_reload)
    return calls


def _current_text(env) -> str:
    """Il testo che un salvataggio scriverebbe con le impostazioni di env."""
    text = sd.render_dropin(env.quarantine.resolve(), home=env.home)
    assert text is not None and "--log-errors" in text
    return text


def _startup(env):
    """I due metodi dell'avvio di MainWindow su un oggetto minimo, con la
    pagina Pianificazione vera. Cercati con getattr: senza la
    rigenerazione la controprova fallisce su un'asserzione."""
    refresh = getattr(mw.MainWindow, "_refresh_system_dropin", None)
    done = getattr(mw.MainWindow, "_dropin_refreshed", None)
    assert refresh is not None and done is not None, "nessuna rigenerazione all'avvio"
    page = mw.SchedulerPage()
    fake = SimpleNamespace(settings=env.settings, scheduler_page=page)
    fake._dropin_refreshed = lambda result: done(fake, result)
    refresh(fake)
    return page


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# -- avvio ---------------------------------------------------------------

def test_dropin_0_1_13_riscritto_all_avvio_una_volta(env, inline, capsys):
    _write(env.dropin, _dropin_0_1_13(env.quarantine))

    page = _startup(env)
    assert env.dropin.read_text() == _current_text(env)
    assert inline.reload == 1
    notice = page.dropin_notice_label.text()
    assert not page.dropin_notice_label.isHidden()
    assert "aggiornato" in notice and str(env.dropin) in notice
    assert capsys.readouterr().err.count("aggiornato") == 1

    # Avvio successivo: già aggiornato, niente scrittura né avviso.
    page = _startup(env)
    assert inline.reload == 1
    assert page.dropin_notice_label.isHidden() and not page.dropin_notice_label.text()
    assert capsys.readouterr().err == ""


def test_dropin_aggiornato_nessuna_scrittura(env, inline, monkeypatch, capsys):
    _write(env.dropin, _current_text(env))
    monkeypatch.setattr(sd, "write_private_text", lambda *a: pytest.fail("scritto"))
    page = _startup(env)
    assert inline.reload == 0 and page.dropin_notice_label.isHidden()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("text", [
    "[Service]\nNice=5\n",
    # Lo stesso comando della 0.1.13 senza la nostra intestazione: scritto a mano.
    "\n".join(_dropin_0_1_13("/q").splitlines()[1:]) + "\n",
])
def test_dropin_scritto_a_mano_intatto(env, inline, text, capsys):
    _write(env.dropin, text)
    page = _startup(env)
    assert env.dropin.read_text() == text
    assert inline.reload == 0 and page.dropin_notice_label.isHidden()
    assert capsys.readouterr().err == ""


def test_dropin_assente_non_creato(env, inline):
    page = _startup(env)
    assert not env.dropin.exists() and not env.dropin.parent.exists()
    assert inline.reload == 0 and page.dropin_notice_label.isHidden()


def test_scrittura_fallita_avviso_con_il_motivo(env, inline, monkeypatch, capsys):
    old = _dropin_0_1_13(env.quarantine)
    _write(env.dropin, old)

    def denied(*a):
        raise PermissionError(13, "Permission denied", str(env.dropin))

    monkeypatch.setattr(sd, "write_private_text", denied)
    page = _startup(env)  # nessuna eccezione
    notice = page.dropin_notice_label.text()
    assert "Permission denied" in notice and "non è stato possibile" in notice
    assert "Permission denied" in capsys.readouterr().err
    assert env.dropin.read_text() == old and inline.reload == 0


def test_reload_fallito_nell_avviso(env, inline):
    _write(env.dropin, _dropin_0_1_13(env.quarantine))
    inline.reload_result = "Failed to connect to bus"
    page = _startup(env)
    assert "Failed to connect to bus" in page.dropin_notice_label.text()
    assert env.dropin.read_text() == _current_text(env)


def test_endpoint_non_valido_avviso_senza_scrittura(env, inline):
    old = _dropin_0_1_13(env.quarantine)
    _write(env.dropin, old)
    env.settings.setValue("tcp_port", "abc")
    page = _startup(env)
    assert "porta TCP non valida" in page.dropin_notice_label.text()
    assert env.dropin.read_text() == old


def test_salvataggio_nel_frattempo_vince(env):
    """Il worker ha letto lo stato prima di un salvataggio: il drop-in
    scritto dal salvataggio non va sovrascritto con lo stato vecchio."""
    _write(env.dropin, _dropin_0_1_13(env.quarantine))
    generation = getattr(sd, "generation", None)
    assert generation is not None, "nessun ordine fra salvataggio e rigenerazione"
    since = generation()
    stale = _current_text(env)
    saved = stale.replace("--quiet", "--quiet --exclude /x")
    assert sd.sync_dropin(saved) is True
    assert sd.refresh_outdated(stale, since) is False
    assert env.dropin.read_text() == saved


def test_salvataggio_toglie_l_avviso(env, inline):
    _write(env.dropin, _dropin_0_1_13(env.quarantine))
    page = _startup(env)
    assert page.dropin_notice_label.text()
    page._save_schedule()
    assert page.dropin_notice_label.isHidden()


# -- systemctl mai nel thread della GUI ----------------------------------

def _instrument(monkeypatch):
    """subprocess.run del modulo dei drop-in sostituito: registra argv e se
    la chiamata parte dal thread della GUI."""
    main = threading.main_thread()
    seen: list[tuple[tuple[str, ...], bool]] = []

    def fake_run(argv, **kw):
        assert argv[0] == sd.SYSTEMCTL
        seen.append((tuple(argv[1:]), threading.current_thread() is main))
        out = "enabled\n" if "is-enabled" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(sd.subprocess, "run", fake_run)
    return seen


def _settle():
    """I risultati arrivano con un segnale queued: si elaborano gli eventi
    finché i thread daemon dei controlli non sono finiti."""
    app = QApplication.instance()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        app.processEvents()
        if not any(t.name == "klamav-fs-check" for t in threading.enumerate()):
            app.processEvents()
            return
        time.sleep(0.01)
    pytest.fail("controlli fuori thread non terminati")


def _commands(seen) -> set[str]:
    return {argv[1] if argv[0] == "--user" else argv[0] for argv, _ in seen}


def test_systemctl_mai_nel_thread_della_gui(env, monkeypatch):
    """run_off_gui_thread vero: stato del timer nella pagina Pianificazione,
    salvataggio della Pianificazione (con la domanda sul timer: Sì,
    disattivalo) e delle Impostazioni (daemon-reload)."""
    seen = _instrument(monkeypatch)
    page = mw.SchedulerPage()
    page.refresh_system_timer()
    _settle()
    page.enable_check.setChecked(True)
    page._save_schedule()
    _settle()
    settings_page = mw.SettingsPage()
    settings_page.quar_edit.setText(str(env.home / "Altra"))
    settings_page._save_settings()
    _settle()

    assert {"daemon-reload", "is-enabled", "disable"} <= _commands(seen), seen
    assert [argv for argv, on_main in seen if on_main] == []


def test_rigenerazione_all_avvio_fuori_dal_thread_della_gui(env, monkeypatch):
    _write(env.dropin, _dropin_0_1_13(env.quarantine))
    seen = _instrument(monkeypatch)
    page = _startup(env)
    _settle()
    assert "--log-errors" in env.dropin.read_text()
    assert page.dropin_notice_label.text()
    assert _commands(seen) == {"daemon-reload"}
    assert [argv for argv, on_main in seen if on_main] == []
