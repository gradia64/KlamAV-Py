"""Regressione: dopo lo streaming a clamd le pagine del file vengono scartate."""
import os
import socket
import struct
import threading

from klamav_py.clamd_client import ClamdClient


def _fake_instream_clamd(path, reply=b"stream: OK\0"):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            data = conn.recv(n - len(buf))
            if not data:
                raise ConnectionError
            buf += data
        return buf

    def serve():
        conn, _ = srv.accept()
        with conn:
            assert recv_exact(conn, len(b"zINSTREAM\0")) == b"zINSTREAM\0"
            while True:
                (size,) = struct.unpack("!L", recv_exact(conn, 4))
                if size == 0:
                    break
                recv_exact(conn, size)
            conn.sendall(reply)
        srv.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_drop_page_cache_calls_fadvise(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(os, "posix_fadvise", lambda fd, off, ln, adv: calls.append((off, ln, adv)))
    f = tmp_path / "x"
    f.write_bytes(b"a" * 10)
    with ClamdClient._open_regular(f) as fh:
        ClamdClient._drop_page_cache(fh)
    assert calls == [(0, 0, os.POSIX_FADV_DONTNEED)]


def test_drop_page_cache_is_best_effort(monkeypatch, tmp_path):
    def boom(*_a):
        raise OSError("non supportato")
    monkeypatch.setattr(os, "posix_fadvise", boom)
    f = tmp_path / "x"
    f.write_bytes(b"a")
    with ClamdClient._open_regular(f) as fh:
        ClamdClient._drop_page_cache(fh)  # nessuna eccezione


def test_instream_drops_cache_after_streaming(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(os, "posix_fadvise", lambda fd, off, ln, adv: calls.append(adv))
    sock_path = str(tmp_path / "clamd.ctl")
    t = _fake_instream_clamd(sock_path)
    target = tmp_path / "file.bin"
    target.write_bytes(os.urandom(200_000))

    result = ClamdClient(unix_socket=sock_path, timeout=2)._instream_one(target)
    t.join(2)

    assert not result.infected
    assert calls == [os.POSIX_FADV_DONTNEED]
