"""Pulizia dei resti della scansione programmata di sistema (legacy_timer)."""

from __future__ import annotations

from pathlib import Path

from klamav_py import legacy_timer as lt


def _setup(tmp_path):
    units = tmp_path / "usr-lib-systemd-user"
    units.mkdir()
    config = tmp_path / "config"
    user = config / "systemd" / "user"
    home = tmp_path / "home"
    home.mkdir()
    return units, config, user, home


def _systemctl(calls):
    def run(*args):
        calls.append(args)
        return None
    return run


def _ours(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lt.HEADER + "\n[Service]\nExecStart=\n")


def test_unit_ancora_installata_non_tocca_nulla(tmp_path):
    units, config, user, home = _setup(tmp_path)
    (units / lt.TIMER_NAME).write_text("[Timer]\n")
    dropin = user / f"{lt.SERVICE_NAME}.d" / lt.DROPIN_NAME
    _ours(dropin)
    calls = []
    assert lt.cleanup(config_home=config, home=home, unit_dir=units,
                      systemctl=_systemctl(calls)) is None
    assert dropin.exists() and calls == []


def test_niente_da_togliere(tmp_path):
    units, config, user, home = _setup(tmp_path)
    calls = []
    assert lt.cleanup(config_home=config, home=home, unit_dir=units,
                      systemctl=_systemctl(calls)) is None
    assert calls == []


def test_toglie_abilitazione_drop_in_e_marcatore(tmp_path):
    units, config, user, home = _setup(tmp_path)
    wants = user / "timers.target.wants"
    wants.mkdir(parents=True)
    link = wants / lt.TIMER_NAME
    link.symlink_to(units / lt.TIMER_NAME)  # rotto: la unit non c'è più
    altro = wants / "altro.timer"
    altro.symlink_to(units / "altro.timer")
    service = user / f"{lt.SERVICE_NAME}.d" / lt.DROPIN_NAME
    timer = user / f"{lt.TIMER_NAME}.d" / lt.DROPIN_NAME
    _ours(service)
    _ours(timer)
    state = home / lt.STATE_FILE
    state.parent.mkdir(parents=True)
    state.write_text("{}")
    calls = []

    result = lt.cleanup(config_home=config, home=home, unit_dir=units,
                        systemctl=_systemctl(calls))

    assert result is not None
    assert not link.is_symlink() and altro.is_symlink()
    assert not service.exists() and not timer.exists() and not state.exists()
    assert not service.parent.exists() and not timer.parent.exists()
    assert set(result.removed) == {link, service, timer, state}
    assert calls == [("stop", lt.TIMER_NAME, lt.SERVICE_NAME), ("daemon-reload",)]
    notice = result.notice()
    assert "Pianificazione" in notice and str(service) in notice


def test_file_altrui_restano_e_si_nominano(tmp_path):
    units, config, user, home = _setup(tmp_path)
    directory = user / f"{lt.TIMER_NAME}.d"
    directory.mkdir(parents=True)
    mano = directory / "orario.conf"
    mano.write_text("[Timer]\nOnCalendar=\nOnCalendar=*-*-* 09:00\n")
    # Stesso nome del nostro, ma senza HEADER: scritto a mano.
    finto = user / f"{lt.SERVICE_NAME}.d" / lt.DROPIN_NAME
    finto.parent.mkdir(parents=True)
    finto.write_text("[Service]\nNice=5\n")
    # Abilitazione che punta altrove: non è quella del pacchetto.
    wants = user / "timers.target.wants"
    wants.mkdir()
    link = wants / lt.TIMER_NAME
    link.symlink_to(config / "mio.timer")
    calls = []

    result = lt.cleanup(config_home=config, home=home, unit_dir=units,
                        systemctl=_systemctl(calls))

    assert mano.exists() and finto.exists() and link.is_symlink()
    assert result.removed == [] and set(result.foreign) == {mano, finto}
    assert calls == []  # nulla tolto: niente daemon-reload
    assert str(mano) in result.notice()


def test_reload_fallito_nell_avviso(tmp_path):
    units, config, user, home = _setup(tmp_path)
    _ours(user / f"{lt.SERVICE_NAME}.d" / lt.DROPIN_NAME)

    def systemctl(*args):
        return "systemctl --user daemon-reload: Failed to connect to bus" if args[0] == "daemon-reload" \
            else "systemctl --user stop: Unit not loaded"

    result = lt.cleanup(config_home=config, home=home, unit_dir=units, systemctl=systemctl)
    # Lo stop fallito (unit non caricata) non è un problema da riportare.
    assert result.problems == ["systemctl --user daemon-reload: Failed to connect to bus"]
    assert "Failed to connect to bus" in result.notice()


def test_gui_avvisa_una_volta(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("PySide6")
    from types import SimpleNamespace

    from PySide6.QtCore import QSettings

    import klamav_py.gui.main_window as mw

    for fmt in (QSettings.NativeFormat, QSettings.IniFormat):
        QSettings.setPath(fmt, QSettings.UserScope, str(tmp_path / "config"))
    pagina, notifiche = [], []
    fake = SimpleNamespace(
        settings=QSettings(mw.APP_NAME, mw.APP_NAME),
        scheduler_page=SimpleNamespace(set_legacy_timer_notice=pagina.append),
        tray_icon=SimpleNamespace(showMessage=lambda *a: notifiche.append(a)),
    )
    esito = lt.Cleanup(removed=[tmp_path / "x"])
    mw.MainWindow._legacy_timer_cleaned(fake, esito)
    mw.MainWindow._legacy_timer_cleaned(fake, esito)
    assert len(pagina) == 2 and len(notifiche) == 1
    mw.MainWindow._legacy_timer_cleaned(fake, None)
    mw.MainWindow._legacy_timer_cleaned(fake, OSError("x"))
    assert len(pagina) == 2
