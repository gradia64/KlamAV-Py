import os
import socket
import threading
from datetime import datetime, timedelta

import pytest

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
    assert probe_db_info(path, timeout=2) == INFO
    t.join(2)


def test_probe_without_database(tmp_path):
    path = str(tmp_path / "clamd.ctl")
    t = _fake_clamd(path, b"ClamAV 1.4.2\0")
    assert probe_db_info(path, timeout=2) is None
    t.join(2)


def test_probe_missing_socket(tmp_path):
    assert probe_db_info(str(tmp_path / "assente.ctl"), timeout=1) is None
