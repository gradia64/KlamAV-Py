"""
Esclusioni configurate in ScanWorker: arrivano nella forma salvata
(scan_exclusions.ExclusionDecision.stored) e si risolvono in run(), al
momento della scansione, come fa la CLI per il timer. La quarantena
resta sempre esclusa, in aggiunta.
"""

from __future__ import annotations


import pytest

from klamav_py.clamd_client import ClamdEndpoint

pytest.importorskip("PySide6")

from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402


class Client:
    """Nessun clamd: registra le esclusioni ricevute da scan_stream."""

    visti: list = []

    def __init__(self, **kw):
        self.skipped = {}

    def scan_stream(self, target, *, exclude_dirs, **kw):
        Client.visti.append(list(exclude_dirs))
        return iter(())


@pytest.fixture(autouse=True)
def _reset():
    Client.visti = []


def _run(tmp_path, **kw):
    w = ScanWorker(endpoint=ClamdEndpoint(), target=tmp_path, client_factory=Client, **kw)
    w.run()
    return Client.visti[-1]


def test_default_nessuna_esclusione(tmp_path):
    assert _run(tmp_path) == []


def test_esclusioni_e_quarantena(tmp_path):
    (tmp_path / "vm").mkdir()
    q = tmp_path / "q"
    assert _run(tmp_path, exclude_dirs=[str(tmp_path / "vm")], quarantine_dir=q) == [
        (tmp_path / "vm").resolve(),
        q.resolve(),
    ]


def test_tilde_espansa(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "vm").mkdir()
    assert _run(tmp_path, exclude_dirs=["~/vm"]) == [(tmp_path / "vm").resolve()]


def test_symlink_risolto_alla_scansione_non_alla_creazione(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a)
    w = ScanWorker(endpoint=ClamdEndpoint(), target=tmp_path, client_factory=Client, exclude_dirs=[str(link)])
    link.unlink()
    link.symlink_to(b)  # cambiato dopo la creazione del worker
    w.run()
    assert Client.visti[-1] == [b.resolve()]


def test_copia_della_lista_del_chiamante(tmp_path):
    (tmp_path / "vm").mkdir()
    lista = [str(tmp_path / "vm")]
    w = ScanWorker(endpoint=ClamdEndpoint(), target=tmp_path, client_factory=Client, exclude_dirs=lista)
    lista.append(str(tmp_path / "altro"))
    w.run()
    assert Client.visti[-1] == [(tmp_path / "vm").resolve()]


def test_esclusioni_per_ogni_destinazione(tmp_path):
    # Selezione multipla: la stessa lista arriva a ogni scan_stream.
    for n in ("a", "b", "vm"):
        (tmp_path / n).mkdir()
    w = ScanWorker(
        endpoint=ClamdEndpoint(), target=[tmp_path / "a", tmp_path / "b"],
        client_factory=Client, exclude_dirs=[str(tmp_path / "vm")],
    )
    w.run()
    assert Client.visti == [[(tmp_path / "vm").resolve()]] * 2
