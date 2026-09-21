"""
Test del socket IPC single-instance (issue di sicurezza: socket in /tmp
occupabile da un altro utente locale). Vedi klamav_py/gui/single_instance.py.

Il comportamento con utenti reali diversi (squatting, runtime directory
ostile) è stato verificato a mano; qui si fissano le proprietà che il
codice deve mantenere, senza bisogno di un secondo utente.
"""

from __future__ import annotations

import ast
import os
import socket
import threading
from pathlib import Path

import pytest

from klamav_py.gui import single_instance
from klamav_py.gui.single_instance import ipc_socket_path, notify_running_instance

APP_PY = Path(__file__).resolve().parent.parent / "klamav_py" / "gui" / "app.py"


def _server(path: str):
    """Server AF_UNIX che accetta una connessione e ne raccoglie i dati."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    received: list[bytes] = []

    def serve() -> None:
        srv.settimeout(2)
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        conn.settimeout(1)
        chunks = b""
        try:
            while data := conn.recv(4096):
                chunks += data
        except OSError:
            pass
        received.append(chunks)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return srv, t, received


def test_nessun_socket_significa_prima_istanza(tmp_path):
    assert notify_running_instance(str(tmp_path / "ipc"), b"/x") is False


def test_socket_morto_significa_prima_istanza(tmp_path):
    path = str(tmp_path / "ipc")
    dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    dead.bind(path)  # file socket presente, nessuno in ascolto
    try:
        assert notify_running_instance(path, b"/x") is False
    finally:
        dead.close()


def test_istanza_dello_stesso_utente_riceve_il_target(tmp_path):
    path = str(tmp_path / "ipc")
    srv, t, received = _server(path)
    try:
        assert notify_running_instance(path, b"/home/u/file.pdf") is True
        t.join(3)
        assert received == [b"/home/u/file.pdf"]
    finally:
        srv.close()


def test_peer_di_altro_utente_non_riceve_nulla(tmp_path, monkeypatch):
    path = str(tmp_path / "ipc")
    srv, t, received = _server(path)
    monkeypatch.setattr(single_instance, "_peer_uid", lambda _s: os.getuid() + 1)
    try:
        # False: il chiamante deve diventare la prima istanza, non uscire.
        assert notify_running_instance(path, b"/home/u/segreto.pdf") is False
        t.join(3)
        assert received in ([], [b""])
    finally:
        srv.close()


def test_peer_non_determinabile_non_riceve_nulla(tmp_path, monkeypatch):
    path = str(tmp_path / "ipc")
    srv, t, received = _server(path)
    monkeypatch.setattr(single_instance, "_peer_uid", lambda _s: None)
    try:
        assert notify_running_instance(path, b"/home/u/segreto.pdf") is False
        t.join(3)
        assert received in ([], [b""])
    finally:
        srv.close()


def test_peer_uid_reale_e_il_nostro(tmp_path):
    path = str(tmp_path / "ipc")
    srv, t, _ = _server(path)
    cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        cli.connect(path)
        assert single_instance._peer_uid(cli) == os.getuid()
    finally:
        cli.close()
        srv.close()


class _FakePaths:
    RuntimeLocation = object()

    def __init__(self, value: str) -> None:
        self._value = value

    def writableLocation(self, _loc):
        return self._value


def test_percorso_nella_runtime_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(single_instance, "QStandardPaths", _FakePaths(str(tmp_path)))
    assert ipc_socket_path() == os.path.join(str(tmp_path), single_instance.IPC_SOCKET_NAME)


def test_runtime_directory_rifiutata_niente_ripiego_su_tmp(monkeypatch):
    # Qt restituisce "" quando la runtime directory non è sicura: non si
    # deve MAI ripiegare su un percorso condiviso.
    monkeypatch.setattr(single_instance, "QStandardPaths", _FakePaths(""))
    assert ipc_socket_path() is None


def test_app_non_usa_piu_il_nome_relativo_ne_qlocalsocket():
    src = APP_PY.read_text(encoding="utf-8")
    strings = {
        n.value for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "klamav_py_ipc" not in strings, "nome relativo: Qt lo risolve in /tmp"
    assert "QLocalSocket" not in src, "il client deve passare da notify_running_instance"


def test_app_controlla_il_ritorno_di_listen():
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    listen_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute) and n.func.attr == "listen"
    ]
    assert listen_calls, "listen() non trovata in app.py"
    # Ogni listen() deve stare in una condizione, mai come istruzione nuda.
    bare = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == "listen"
    ]
    assert not bare, "il valore di ritorno di listen() viene ignorato"
