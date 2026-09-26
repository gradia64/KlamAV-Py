"""
Endpoint di clamd nella GUI: persistenza delle quattro chiavi (senza
migrazione), pagina Impostazioni (combo, righe, avviso sul traffico in
chiaro, validazione prima di ogni effetto collaterale), drop-in della
scansione programmata e worker che ricevono l'endpoint.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import DEFAULT_SOCKET, ClamdEndpoint  # noqa: E402
from klamav_py.gui.db_info_worker import probe_db_info  # noqa: E402
from klamav_py.gui.ping_worker import PingWorker  # noqa: E402
from klamav_py.quarantine_location import decide  # noqa: E402
from klamav_py.systemd_dropin import dropin_path  # noqa: E402

MOUNTS_EXT4 = "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"


def load_fake_clamd():
    import importlib.util

    path = Path(__file__).with_name("fake_clamd.py")
    spec = importlib.util.spec_from_file_location("klamav_fake_clamd", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fake = load_fake_clamd()


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
        lambda raw: decide(raw, unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=()),
    )
    calls = SimpleNamespace(reload=0)
    # I drop-in reali della macchina (es. un override.conf dello sviluppatore)
    # non devono entrare nei test: vedi test_systemd_dropin per il rilevamento.
    monkeypatch.setattr(mw, "foreign_overrides", lambda **kw: [])
    monkeypatch.setattr(mw, "daemon_reload", lambda: setattr(calls, "reload", calls.reload + 1))
    shown = []
    for name in ("question", "warning", "information"):
        monkeypatch.setattr(
            QMessageBox, name,
            staticmethod(lambda *a, _n=name, **k: shown.append((_n, a[2])) or QMessageBox.Yes),
        )
    return SimpleNamespace(home=home, calls=calls, shown=shown, dropin=dropin_path())


def settings():
    return QSettings(mw.APP_NAME, mw.APP_NAME)


# -- persistenza ---------------------------------------------------------

@pytest.mark.parametrize("ep", [
    ClamdEndpoint(),
    ClamdEndpoint.unix("/run/clamd.scan/clamd.sock"),
    ClamdEndpoint.tcp("10.0.0.5", 3311),
    ClamdEndpoint.tcp("::1"),
])
def test_round_trip(env, ep):
    mw.save_endpoint(settings(), ep)
    assert mw.load_endpoint(settings()) == ep


def test_installazione_precedente_senza_migrazione(env):
    # Solo socket_path, come prima della 0.1.10: letta come unix.
    settings().setValue("socket_path", "/run/clamd.scan/clamd.sock")
    assert mw.load_endpoint(settings()) == ClamdEndpoint.unix("/run/clamd.scan/clamd.sock")


def test_chiavi_assenti_dal_default_dell_entry_point(env):
    cli_default = ClamdEndpoint.tcp("127.0.0.1", 3311)
    assert mw.load_endpoint(settings(), cli_default) == cli_default


def test_torna_a_unix_conservando_host_e_porta(env):
    mw.save_endpoint(settings(), ClamdEndpoint(transport="unix", tcp_host="10.0.0.5", tcp_port=3311))
    ep = mw.load_endpoint(settings())
    assert not ep.is_tcp and (ep.tcp_host, ep.tcp_port) == ("10.0.0.5", 3311)


@pytest.mark.parametrize("key, value", [
    ("tcp_port", "abc"), ("tcp_port", "0"), ("clamd_transport", "udp"),
])
def test_valori_salvati_non_validi(env, key, value):
    mw.save_endpoint(settings(), ClamdEndpoint.tcp("10.0.0.5"))
    settings().setValue(key, value)
    with pytest.raises(ValueError):
        mw.load_endpoint(settings())


def test_finestra_usa_il_default_e_avvisa_una_volta(env):
    settings().setValue("clamd_transport", "udp")
    messages = []
    win = SimpleNamespace(
        settings=settings(),
        _default_endpoint=ClamdEndpoint(),
        _endpoint_problem_noted=False,
        tray_icon=SimpleNamespace(showMessage=lambda *a: messages.append(a)),
    )
    for _ in range(3):
        assert mw.MainWindow._clamd_endpoint(win) == ClamdEndpoint()
    assert len(messages) == 1


# -- pagina Impostazioni -------------------------------------------------

def test_righe_e_avviso_seguono_il_trasporto(env):
    page = mw.SettingsPage()
    assert not page.socket_row.isHidden() and page.tcp_row.isHidden()
    assert page.tcp_warning_label.isHidden()
    page.transport_combo.setCurrentIndex(page.transport_combo.findData("tcp"))
    page.tcp_host_edit.setText("::1")
    page.tcp_port_spin.setValue(3311)
    assert page.socket_row.isHidden() and not page.tcp_row.isHidden()
    assert "in chiaro a [::1]:3311" in page.tcp_warning_label.text()


def test_carica_le_impostazioni_salvate(env):
    mw.save_endpoint(settings(), ClamdEndpoint.tcp("clamd.lan", 3311))
    page = mw.SettingsPage()
    assert page._endpoint_from_form() == ClamdEndpoint.tcp("clamd.lan", 3311)


def _tcp_page(host, port=3310):
    page = mw.SettingsPage()
    page.transport_combo.setCurrentIndex(page.transport_combo.findData("tcp"))
    page.tcp_host_edit.setText(host)
    page.tcp_port_spin.setValue(port)
    return page


@pytest.mark.parametrize("host", ["", "   ", "/run/clamav/clamd.ctl", "ho st", "127.0.0.1:3310"])
def test_host_non_valido_blocca_prima_di_ogni_effetto(env, host):
    _tcp_page(host)._save_settings()
    assert env.shown and env.shown[0][0] == "warning"
    assert settings().value("clamd_transport") is None and not env.dropin.exists()


def test_parentesi_ipv6_accettate(env):
    _tcp_page("[::1]")._save_settings()
    assert mw.load_endpoint(settings()) == ClamdEndpoint.tcp("::1")


def test_tcp_salvato_scrive_il_dropin_con_la_rete(env):
    _tcp_page("10.0.0.5", 3311)._save_settings()
    assert mw.load_endpoint(settings()) == ClamdEndpoint.tcp("10.0.0.5", 3311)
    text = env.dropin.read_text()
    assert '--tcp "10.0.0.5:3311"' in text and "PrivateNetwork=no" in text
    assert env.calls.reload == 1


def test_ritorno_al_socket_predefinito_rimuove_il_dropin(env):
    _tcp_page("10.0.0.5", 3311)._save_settings()
    page = mw.SettingsPage()
    page.transport_combo.setCurrentIndex(page.transport_combo.findData("unix"))
    page._save_settings()
    assert not env.dropin.exists()
    ep = mw.load_endpoint(settings())
    assert ep == ClamdEndpoint(transport="unix", tcp_host="10.0.0.5", tcp_port=3311)


def test_ripristina_predefiniti(env):
    page = _tcp_page("10.0.0.5")
    page._reset_defaults()
    assert page._endpoint_from_form() == ClamdEndpoint()
    assert page.tcp_row.isHidden()


# -- worker --------------------------------------------------------------

def test_ping_worker_via_tcp(app):
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    try:
        worker = PingWorker(ClamdEndpoint.tcp("127.0.0.1", server.address[1]), timeout=5)
        results = []
        worker.result_ready.connect(results.append)
        worker.run()
    finally:
        server.close()
    assert results == [True]


def test_ping_worker_irraggiungibile_non_solleva(app):
    worker = PingWorker(ClamdEndpoint.tcp("127.0.0.1", fake.closed_tcp_port()), timeout=2)
    results = []
    worker.result_ready.connect(results.append)
    worker.run()
    assert results == [False]


def test_probe_db_info_irraggiungibile_restituisce_none():
    # ClamdUnavailable non è un OSError: probe_db_info deve intercettarla.
    assert probe_db_info(ClamdEndpoint.tcp("127.0.0.1", fake.closed_tcp_port()), timeout=2) is None


def test_default_socket_invariato():
    assert ClamdEndpoint().unix_socket == DEFAULT_SOCKET


def test_scan_worker_usa_l_endpoint_senza_factory(app, tmp_path):
    # Senza client_factory il worker deve costruire il client dal SUO
    # endpoint, non da un default: qui c'è solo un clamd TCP.
    from klamav_py.gui.scan_worker import ScanWorker

    (tmp_path / "infetto.txt").write_bytes(b"x " + fake.EICAR_MARK)
    server = fake.FakeClamd(socket.AF_INET, ("127.0.0.1", 0))
    try:
        worker = ScanWorker(endpoint=ClamdEndpoint.tcp("127.0.0.1", server.address[1]), target=tmp_path)
        rows, errors = [], []
        worker.result_ready.connect(rows.append)
        worker.error.connect(errors.append)
        worker.run()
    finally:
        server.close()
    assert not errors and [r.status for r in rows] == ["FOUND"]


def test_valore_salvato_non_valido_mostrato_per_correggerlo(env):
    # Come salvato dalla versione senza il controllo sugli host con ':'.
    s = settings()
    s.setValue("clamd_transport", "tcp")
    s.setValue("tcp_host", "127.0.0.1:3310")
    s.setValue("tcp_port", 3310)
    page = mw.SettingsPage()
    assert page.transport_combo.currentData() == "tcp"
    assert page.tcp_host_edit.text() == "127.0.0.1:3310" and not page.tcp_row.isHidden()
    page._save_settings()
    assert env.shown[0][0] == "warning" and "porta va indicata a parte" in env.shown[0][1]
    page.tcp_host_edit.setText("127.0.0.1")
    env.shown.clear()
    page._save_settings()
    assert mw.load_endpoint(settings()) == ClamdEndpoint.tcp("127.0.0.1", 3310)



def test_dropin_in_conflitto_avvisato_al_salvataggio(env, monkeypatch):
    from klamav_py.systemd_dropin import ForeignOverride

    override = ForeignOverride(Path("/x/override.conf"), ("ExecStart",), True, True)
    seen = []
    monkeypatch.setattr(mw, "foreign_overrides", lambda **kw: seen.append(kw) or [override])
    page = _tcp_page("127.0.0.1")
    page._save_settings()
    assert seen == [{"tcp": True}]
    assert any("override.conf" in n for n in page.save_notes)
    assert mw.load_endpoint(settings()).is_tcp  # avviso, non blocco


def test_dropin_in_conflitto_nella_pagina_pianificazione(env, monkeypatch):
    from klamav_py.systemd_dropin import ForeignOverride

    monkeypatch.setattr(mw, "timer_enabled", lambda: True)
    monkeypatch.setattr(
        mw, "foreign_overrides",
        lambda **kw: [ForeignOverride(Path("/x/override.conf"), ("ExecStart",), True, False)],
    )
    page = mw.SchedulerPage()
    page.refresh_system_timer()
    assert "override.conf" in page.system_timer_label.text()
