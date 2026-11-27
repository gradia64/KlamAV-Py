import os
import socket
import threading
from datetime import datetime, timedelta

import pytest

from klamav_py.clamd_client import ClamdEndpoint
from klamav_py.db_freshness import DbInfo, describe

pytest.importorskip("PySide6")
from klamav_py.gui.db_info_worker import probe_db_info  # noqa: E402

BUILT = datetime(2026, 9, 22, 9, 0, 0)
INFO = DbInfo("1.4.2", 27072, BUILT)


def test_describe_fresh():
    text = describe(INFO, now=BUILT + timedelta(hours=5))
    assert "27072" in text and "22/09/2026 09:00" in text and "5 h" in text
    assert "non aggiornate" not in text


def test_describe_stale_in_days():
    text = describe(INFO, now=BUILT + timedelta(days=3))
    assert "3 giorni" in text and "non aggiornate" in text


def test_describe_unknown():
    assert "sconosciuto" in describe(None)


def _fake_clamd(path, reply):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            assert conn.recv(64) == b"zVERSION\0"
            conn.sendall(reply)
        srv.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_probe_reads_version(tmp_path):
    path = str(tmp_path / "clamd.ctl")
    t = _fake_clamd(path, b"ClamAV 1.4.2/27072/Tue Sep 22 09:00:00 2026\0")
    assert probe_db_info(ClamdEndpoint.unix(path), timeout=2) == INFO
    t.join(2)


def test_probe_without_database(tmp_path):
    path = str(tmp_path / "clamd.ctl")
    t = _fake_clamd(path, b"ClamAV 1.4.2\0")
    assert probe_db_info(ClamdEndpoint.unix(path), timeout=2) is None
    t.join(2)


def test_probe_missing_socket(tmp_path):
    # ClamdUnavailable non è un OSError: probe_db_info deve intercettarla.
    assert probe_db_info(ClamdEndpoint.unix(str(tmp_path / "assente.ctl")), timeout=1) is None


def test_probe_tcp_closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert probe_db_info(ClamdEndpoint.tcp("127.0.0.1", port), timeout=1) is None


def test_describe_stale_but_confirmed():
    text = describe(INFO.confirmed({27072}), now=BUILT + timedelta(days=2))
    assert "non aggiornate" not in text and "ultima versione pubblicata" in text


def test_confirm_with_journal(monkeypatch):
    from klamav_py import freshclam_service as fs
    from klamav_py.gui import db_info_worker as w
    calls = []

    def fake(since, run=None):
        calls.append(since)
        return frozenset({27072})
    monkeypatch.setattr(fs, "recent_up_to_date_versions", fake)
    old = DbInfo("1.4.2", 27072, datetime.now() - timedelta(days=3))
    assert w.confirm_with_journal(old).confirmed_current
    # Database fresco: il journal non si legge nemmeno.
    fresh = DbInfo("1.4.2", 27072, datetime.now())
    assert w.confirm_with_journal(fresh) is fresh and len(calls) == 1
    assert w.confirm_with_journal(None) is None
