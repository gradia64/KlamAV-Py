"""
Errori di I/O sulle sottocartelle: guasti, non permessi.

Nella 0.1.13 ogni errno diverso da ENOENT/ENOTDIR su una sottocartella era
«cartella non leggibile»: EIO (disco che degrada) ed ESTALE (mount NFS che
non risponde più) finivano nel contatore a parte, con il suggerimento di
--exclude, e non fra gli errori. Ora solo EACCES ed EPERM restano «cartella
non leggibile»; gli altri errno passano per il percorso degli errori dei
file: stesso conteggio, stessa riga nella GUI e nel log della
pianificazione interna, nessun suggerimento di esclusione. Il codice di
uscita (2 senza rilevamenti) è in test_io_fault_exit.py.

os.scandir è strumentato: fallisce solo per la sottocartella scelta, quindi
la radice e il resto dell'albero si leggono davvero.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import klamav_py.cli as cli  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ScanResult  # noqa: E402
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402
from klamav_py.scan_totals import ScanTotals  # noqa: E402

GUASTI = [errno.EIO, errno.ESTALE]
PERMESSI = [errno.EACCES, errno.EPERM]


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "radice"
    (root / "buona").mkdir(parents=True)
    (root / "buona" / "a.txt").write_text("a")
    (root / "guasta" / "dentro").mkdir(parents=True)
    (root / "guasta" / "dentro" / "b.txt").write_text("b")
    (root / "c.txt").write_text("c")
    return root


@pytest.fixture
def broken(monkeypatch):
    """broken(cartella, errno): os.scandir di quella cartella fallisce."""
    real = os.scandir

    def install(target: Path, code: int) -> None:
        def scandir(path="."):
            if Path(os.fsdecode(path)) == target:
                raise OSError(code, os.strerror(code), os.fsdecode(path))
            return real(path)
        monkeypatch.setattr(os, "scandir", scandir)

    return install


def _walk(root):
    seen = []
    items = list(ClamdClient._iter_files(root, [], lambda p, e: seen.append((p, e.errno))))
    return items, seen


# -- traversata ------------------------------------------------------------

@pytest.mark.parametrize("code", GUASTI, ids=errno.errorcode.get)
def test_guasto_e_un_risultato_errore(tmp_path, broken, code):
    root = _tree(tmp_path)
    broken(root / "guasta", code)
    items, seen = _walk(root)
    errors = [i for i in items if isinstance(i, ScanResult)]
    files = sorted(i.name for i in items if isinstance(i, Path))
    assert files == ["a.txt", "c.txt"]
    assert seen == []
    (err,) = errors
    assert err.status == "ERROR" and err.path == str(root / "guasta")
    assert os.strerror(code) in err.signature and "--exclude" not in err.signature


def test_guasto_sull_ultima_cartella_non_si_perde(tmp_path, broken):
    # os.walk chiama onerror dentro l'ultimo next(): l'errore va restituito
    # anche dopo la fine del ciclo.
    root = tmp_path / "radice"
    (root / "zzz").mkdir(parents=True)
    broken(root / "zzz", errno.EIO)
    items, _ = _walk(root)
    assert [i.path for i in items] == [str(root / "zzz")]


@pytest.mark.parametrize("code", PERMESSI, ids=errno.errorcode.get)
def test_permessi_restano_cartella_non_leggibile(tmp_path, broken, code):
    root = _tree(tmp_path)
    broken(root / "guasta", code)
    items, seen = _walk(root)
    assert not any(isinstance(i, ScanResult) for i in items)
    assert seen == [(root / "guasta", code)]


def test_enoent_ignorato(tmp_path, broken):
    root = _tree(tmp_path)
    broken(root / "guasta", errno.ENOENT)
    items, seen = _walk(root)
    assert seen == [] and not any(isinstance(i, ScanResult) for i in items)


# -- client vero, clamd finto ------------------------------------------------

class OkClient(ClamdClient):
    """scan_stream vero (con la conversione delle cartelle guaste), ogni file
    pulito senza parlare con clamd."""

    def __init__(self, **kw):
        super().__init__(unix_socket="/nonexistent")

    def ping(self):
        return True

    def _instream_one(self, target, max_stream_size):
        return ScanResult(str(target), "OK")

    def scan_stream(self, path, **kw):
        kw["persistent"] = False
        return super().scan_stream(path, **kw)


@pytest.fixture
def cli_env(monkeypatch):
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: OkClient())


@pytest.mark.parametrize("code", GUASTI, ids=errno.errorcode.get)
def test_cli_guasto_contato_fra_gli_errori(tmp_path, broken, cli_env, capsys, code):
    root = _tree(tmp_path)
    broken(root / "guasta", code)
    log = tmp_path / "errori.log"
    # Guasto di I/O senza rilevamenti: uscita 2 (punto 1-bis, vedi STATO DI
    # USCITA in klamav-py(1)).
    assert cli.main(["scan", str(root), "--quiet", "--log-errors", str(log)]) == 2
    out = capsys.readouterr()
    assert "3 file scansionati, 0 infetti, 1 errori." in out.out
    assert f"ERRORE su {root / 'guasta'}: cartella non letta" in out.err
    assert "CARTELLA NON LEGGIBILE" not in out.err
    assert "cartelle non leggibili" not in out.out
    assert "--exclude" not in out.out + out.err
    assert f"{root / 'guasta'}\tcartella non letta" in log.read_text()


@pytest.mark.parametrize("code", PERMESSI, ids=errno.errorcode.get)
def test_cli_permessi_invariati(tmp_path, broken, cli_env, capsys, code):
    root = _tree(tmp_path)
    broken(root / "guasta", code)
    assert cli.main(["scan", str(root), "--quiet"]) == 0
    out = capsys.readouterr()
    assert "2 file scansionati, 0 infetti, 0 errori." in out.out
    assert "1 cartelle non leggibili" in out.out and "--exclude" in out.out


# -- GUI ---------------------------------------------------------------------

def _run_worker(root, **kw):
    w = ScanWorker(endpoint=ClamdEndpoint(), target=root, client_factory=OkClient, **kw)
    got = {"results": [], "unreadable": [], "aborted": [], "finished": []}
    w.result_ready.connect(got["results"].append)
    w.unreadable_dir.connect(lambda p, r: got["unreadable"].append(p))
    w.aborted.connect(got["aborted"].append)
    w.finished_scan.connect(got["finished"].append)
    w.run()
    return got


@pytest.mark.parametrize("code", GUASTI, ids=errno.errorcode.get)
def test_worker_guasto_e_un_errore(tmp_path, broken, code):
    root = _tree(tmp_path)
    broken(root / "guasta", code)
    got = _run_worker(root)
    assert got["unreadable"] == [] and got["aborted"] == []
    assert [(r.path, r.status) for r in got["results"]] == [(str(root / "guasta"), "ERROR")]
    assert got["finished"] == [ScanTotals(scanned=3, errors=1, io_faults=1)]


def test_pianificazione_guasto_nel_log_e_fra_gli_errori(tmp_path, broken, monkeypatch):
    from klamav_py.scan_totals import io_fault_note
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    root = _tree(tmp_path)
    broken(root / "guasta", errno.EIO)
    got = _run_worker(root, strict_roots=True)
    log, messages = [], []
    fake = SimpleNamespace(
        settings=SimpleNamespace(value=lambda k, d=None, type=None: str(root) if k == "schedule_target" else d,
                                 setValue=lambda *a: None, sync=lambda: None),
        tray_icon=SimpleNamespace(showMessage=lambda *a, **k: messages.append(a[1])),
        _reset_tray_tooltip=lambda: None, _bg_log_close=lambda: None, _bg_log_write=log.append,
        _bg_log_path=None, _bg_aborted=None, _schedule_aborted_noted=False,
        history_manager=SimpleNamespace(add_entry=lambda *a, **k: None),
        history_page=SimpleNamespace(refresh=lambda: None),
        scheduler_page=SimpleNamespace(update_progress=lambda *a: None),
        clamd_health=SimpleNamespace(is_down=False), _update_next_run_label=lambda: None,
        bg_worker=None,
    )
    for result in got["results"]:
        mw.MainWindow._on_bg_result(fake, result)
    mw.MainWindow._on_bg_finished(fake, got["finished"][0])
    assert log == [f"ERRORE — {root / 'guasta'}: cartella non letta, contenuto non controllato: "
                   f"{os.strerror(errno.EIO)}",
                   f"ATTENZIONE: {io_fault_note(1)}"]  # 0.1.15: come la CLI
    assert "1 errori" in messages[0] and "escludile" not in messages[0]
