"""
Esclusioni configurate in ScanWorker: arrivano nella forma salvata
(scan_exclusions.ExclusionDecision.stored) e si risolvono in run(), al
momento della scansione, come fa la CLI per il timer. La quarantena
resta sempre esclusa, in aggiunta.

Prima di percorrere le radici il worker rivaluta le esclusioni: una
radice dentro un'esclusione (o dentro la quarantena) darebbe una
scansione di zero file conclusa senza errori.
"""

from __future__ import annotations


import pytest

from klamav_py.clamd_client import ClamdEndpoint, ClamdUnavailable

pytest.importorskip("PySide6")

from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402
from klamav_py.scan_totals import ScanTotals  # noqa: E402


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


# -- rivalidazione prima della traversata --------------------------------

def _run_capture(**kw):
    w = ScanWorker(endpoint=ClamdEndpoint(), client_factory=kw.pop("client_factory", Client), **kw)
    got = {"aborted": [], "error": [], "finished": []}
    w.aborted.connect(got["aborted"].append)
    w.error.connect(got["error"].append)
    w.finished_scan.connect(lambda *a: got["finished"].append(a))
    w.run()
    return got


def test_esclusione_ripuntata_su_un_antenato_ferma_la_scansione(tmp_path):
    # Valida al salvataggio, poi il symlink viene ripuntato sulla radice:
    # senza il controllo _iter_files non percorre nulla e la scansione
    # risulta pulita.
    (tmp_path / "radice" / "archivio").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "radice" / "archivio")
    w_kw = dict(target=tmp_path / "radice", exclude_dirs=[str(link)])
    link.unlink()
    link.symlink_to(tmp_path)
    got = _run_capture(**w_kw)
    assert Client.visti == []  # nessuna traversata
    assert len(got["aborted"]) == 1 and "esclusa per intero" in got["aborted"][0]
    assert got["error"] == got["aborted"]
    assert got["finished"] == [(ScanTotals(),)]


def test_esclusione_diventata_file_ferma_la_scansione(tmp_path):
    (tmp_path / "radice").mkdir()
    (tmp_path / "radice" / "nota").write_text("x")
    got = _run_capture(target=tmp_path / "radice", exclude_dirs=[str(tmp_path / "radice" / "nota")])
    assert Client.visti == [] and "non è una directory" in got["aborted"][0]


def test_quarantena_che_contiene_la_radice_ferma_la_scansione(tmp_path):
    # Il caso aperto della 0.1.11: quarantena antenata della cartella della
    # pianificazione interna.
    (tmp_path / "q" / "dentro").mkdir(parents=True)
    got = _run_capture(target=tmp_path / "q" / "dentro", quarantine_dir=tmp_path / "q")
    assert Client.visti == []
    assert got["aborted"] and got["aborted"][0].startswith("Scansione non eseguita. Cartella di quarantena")


def test_radice_uguale_alla_quarantena_ferma_la_scansione(tmp_path):
    (tmp_path / "q").mkdir()
    got = _run_capture(target=tmp_path / "q", quarantine_dir=tmp_path / "q")
    assert Client.visti == [] and got["aborted"]


def test_file_nella_quarantena_non_e_un_errore(tmp_path):
    # Real-Time: un evento su un file della quarantena non va segnalato
    # (i file non sono sfoltiti da _iter_files, il risultato è scartato).
    (tmp_path / "q").mkdir()
    f = tmp_path / "q" / "abc"
    f.write_text("x")
    got = _run_capture(target=f, quarantine_dir=tmp_path / "q")
    assert got["aborted"] == [] and len(Client.visti) == 1


def test_file_sparito_non_e_un_errore(tmp_path):
    got = _run_capture(target=tmp_path / "sparito", quarantine_dir=tmp_path / "q")
    assert got["aborted"] == []


def test_esclusione_valida_non_ferma_la_scansione(tmp_path):
    (tmp_path / "vm").mkdir()
    got = _run_capture(target=tmp_path, exclude_dirs=[str(tmp_path / "vm"), str(tmp_path / "manca")])
    assert got["aborted"] == [] and len(Client.visti) == 1


def test_clamd_irraggiungibile_segnalato_come_aborted(tmp_path):
    class Down(Client):
        def scan_stream(self, target, **kw):
            raise ClamdUnavailable("connessione rifiutata", where="/run/clamd.sock")

    got = _run_capture(target=tmp_path, client_factory=Down)
    assert len(got["aborted"]) == 1 and "non raggiungibile" in got["aborted"][0]


# -- strict_roots (scansione programmata) --------------------------------

def test_strict_cartella_mancante(tmp_path):
    got = _run_capture(target=tmp_path / "sparita", strict_roots=True)
    assert Client.visti == [] and "non esiste" in got["aborted"][0]


def test_strict_cartella_file(tmp_path):
    (tmp_path / "f").write_text("x")
    got = _run_capture(target=tmp_path / "f", strict_roots=True)
    assert Client.visti == [] and "non è una directory" in got["aborted"][0]


def test_strict_percorso_relativo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rel").mkdir()
    got = _run_capture(target="rel", strict_roots=True)
    assert Client.visti == [] and "assoluto" in got["aborted"][0]


def test_strict_radice_risolta_prima_della_traversata(tmp_path, monkeypatch):
    # Senza risoluzione la quarantena (confrontata risolta) non verrebbe
    # sfoltita percorrendo un collegamento alla radice.
    percorsi = []

    class Rec(Client):
        def scan_stream(self, target, *, exclude_dirs, **kw):
            percorsi.append(target)
            return iter(())

    (tmp_path / "vera").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "vera")
    monkeypatch.setenv("HOME", str(tmp_path))
    got = _run_capture(target=tmp_path / "link", strict_roots=True, client_factory=Rec)
    assert got["aborted"] == [] and percorsi == [(tmp_path / "vera").resolve()]
    got = _run_capture(target="~/link", strict_roots=True, client_factory=Rec)
    assert got["aborted"] == [] and percorsi[-1] == (tmp_path / "vera").resolve()
