"""
Radice e cartelle non leggibili.

os.walk senza onerror ingoia gli errori di scandir:
- una radice illeggibile (cartella di un altro utente, 0300, 0700 altrui)
  dava «completata, 0 file, 0 errori», cioè una scansione pulita senza aver
  controllato nulla. Ora è bloccante: aborted nella GUI, uscita 2 nella CLI,
  con una sonda esplicita prima della traversata e della connessione a
  clamd;
- una sottocartella illeggibile era saltata in silenzio. Ora ha un
  contatore suo (non «errori») e il percorso nel log.

I test sui permessi non hanno senso da root, che legge tutto.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import klamav_py.cli as cli  # noqa: E402
import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.clamd_client import (  # noqa: E402
    ClamdClient,
    ClamdEndpoint,
    ScanResult,
    UnreadableRoot,
    root_problem,
)
from klamav_py.gui.scan_worker import ScanWorker  # noqa: E402
from klamav_py.scan_totals import ScanTotals  # noqa: E402

non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root legge anche le cartelle 0000")


@pytest.fixture
def chmod_restore():
    """Rende di nuovo percorribili le cartelle chiuse dal test, perché
    tmp_path si possa ripulire."""
    closed = []

    def close(p: Path, mode: int) -> Path:
        os.chmod(p, mode)
        closed.append(p)
        return p

    yield close
    for p in closed:
        if p.exists():
            os.chmod(p, 0o700)


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "radice"
    (root / "leggibile").mkdir(parents=True)
    (root / "leggibile" / "a.txt").write_text("a")
    (root / "chiusa" / "dentro").mkdir(parents=True)
    (root / "chiusa" / "dentro" / "b.txt").write_text("b")
    (root / "c.txt").write_text("c")
    return root


def _walk(root, exclude=()):
    seen = []
    files = list(ClamdClient._iter_files(root, list(exclude), lambda p, e: seen.append((p, e.errno))))
    return files, seen


def _other_owner_dir() -> Path | None:
    """Una cartella di un altro proprietario che l'utente non può leggere."""
    for candidate in (Path("/root"), Path("/var/lib/private"), Path("/proc/1/fd")):
        try:
            if candidate.is_dir() and candidate.stat().st_uid != os.geteuid():
                with os.scandir(candidate):
                    pass
        except PermissionError:
            return candidate
        except OSError:
            continue
    return None


# -- traversata ----------------------------------------------------------

@non_root
def test_sottocartella_illeggibile_riportata_una_volta(tmp_path, chmod_restore):
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    files, seen = _walk(root)
    assert sorted(f.name for f in files) == ["a.txt", "c.txt"]
    # Solo la più alta: os.walk non scende in una cartella illeggibile.
    assert seen == [(root / "chiusa", errno.EACCES)]


@non_root
def test_sottocartella_illeggibile_esclusa_non_contata(tmp_path, chmod_restore):
    # È la via d'uscita suggerita nel riepilogo: funziona perché il pruning
    # delle esclusioni avviene prima dello scandir.
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    _, seen = _walk(root, exclude=[root / "chiusa"])
    assert seen == []


@pytest.mark.parametrize("come", ["rimossa", "sostituita da un file"])
def test_cartella_sparita_durante_la_traversata_non_contata(tmp_path, come):
    root = tmp_path / "radice"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "x").write_text("x")
    (root / "primo.txt").write_text("1")
    seen = []
    it = ClamdClient._iter_files(root, [], lambda p, e: seen.append(p))
    # Prima di scendere in sub, os.walk restituisce i file della radice.
    assert next(it).name == "primo.txt"
    shutil.rmtree(root / "sub")
    if come != "rimossa":
        (root / "sub").write_text("ora sono un file")
    assert list(it) == []
    assert seen == []


@non_root
def test_radice_illeggibile_durante_la_traversata_e_bloccante(tmp_path, chmod_restore):
    # Corsa fra la sonda del chiamante e os.walk: mai una traversata vuota.
    root = _tree(tmp_path)
    chmod_restore(root, 0o300)
    with pytest.raises(UnreadableRoot):
        _walk(root)


@non_root
def test_sonda_della_radice(tmp_path, chmod_restore):
    root = _tree(tmp_path)
    assert root_problem(root) is None
    vuota = tmp_path / "vuota"
    vuota.mkdir()
    assert root_problem(vuota) is None
    assert root_problem(root / "c.txt") is None  # un file regolare è una radice valida
    chmod_restore(root, 0o300)
    assert "non è leggibile" in root_problem(root)


# -- worker della GUI ------------------------------------------------------

class WalkingClient:
    """Traversata vera (_iter_files), nessun clamd: ogni file è pulito."""

    calls = 0

    def __init__(self, **kw):
        self.skipped = Counter()

    def scan_stream(self, target, *, exclude_dirs=None, on_unreadable_dir=None, **kw):
        WalkingClient.calls += 1
        for f in ClamdClient._iter_files(Path(target).resolve(), exclude_dirs, on_unreadable_dir):
            yield ScanResult(str(f), "OK")


def _run_worker(**kw):
    WalkingClient.calls = 0
    w = ScanWorker(endpoint=ClamdEndpoint(), client_factory=WalkingClient, **kw)
    got = {"aborted": [], "finished": [], "unreadable": [], "error": []}
    w.aborted.connect(got["aborted"].append)
    w.error.connect(got["error"].append)
    w.finished_scan.connect(got["finished"].append)
    w.unreadable_dir.connect(lambda p, r: got["unreadable"].append(p))
    w.run()
    return got


@non_root
@pytest.mark.parametrize("strict", [False, True], ids=["manuale", "pianificazione"])
def test_worker_radice_0300_non_completata(tmp_path, chmod_restore, strict):
    root = _tree(tmp_path)
    chmod_restore(root, 0o300)
    got = _run_worker(target=root, strict_roots=strict)
    assert len(got["aborted"]) == 1 and "non è leggibile" in got["aborted"][0]
    assert WalkingClient.calls == 0  # nessuna traversata
    assert got["finished"] == [ScanTotals()]


@non_root
def test_worker_radice_di_un_altro_proprietario(tmp_path):
    altrui = _other_owner_dir()
    if altrui is None:
        pytest.skip("nessuna cartella altrui non leggibile su questo sistema")
    got = _run_worker(target=altrui)
    assert len(got["aborted"]) == 1 and "non è leggibile" in got["aborted"][0]
    assert WalkingClient.calls == 0


def test_worker_radice_vuota_leggibile_e_pulita(tmp_path):
    vuota = tmp_path / "vuota"
    vuota.mkdir()
    got = _run_worker(target=vuota, strict_roots=True)
    assert got["aborted"] == [] and got["finished"] == [ScanTotals()]


@non_root
def test_worker_sottocartella_illeggibile_contata_a_parte(tmp_path, chmod_restore):
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    got = _run_worker(target=root)
    assert got["aborted"] == [] and got["error"] == []
    assert got["unreadable"] == [str(root / "chiusa")]
    assert got["finished"] == [ScanTotals(scanned=2, unreadable_dirs=1)]


@non_root
def test_worker_sottocartella_illeggibile_esclusa(tmp_path, chmod_restore):
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    got = _run_worker(target=root, exclude_dirs=[str(root / "chiusa")])
    assert got["unreadable"] == [] and got["finished"] == [ScanTotals(scanned=2)]


# -- pianificazione interna ------------------------------------------------

class _Settings:
    def __init__(self, target):
        self.values = {"schedule_target": str(target)}

    def value(self, key, default=None, type=None):
        return self.values.get(key, default)

    def setValue(self, key, value):
        self.values[key] = value

    def sync(self):
        pass


def _bg_window(tmp_path, target):
    messages, entries, log = [], [], []
    fake = SimpleNamespace(
        settings=_Settings(target),
        tray_icon=SimpleNamespace(showMessage=lambda *a, **k: messages.append(a[1])),
        _reset_tray_tooltip=lambda: None,
        _bg_log_close=lambda: None,
        _bg_log_write=log.append,
        _bg_log_path=None,
        _bg_aborted=None,
        _schedule_aborted_noted=False,
        history_manager=SimpleNamespace(add_entry=lambda *a, **k: entries.append(a)),
        history_page=SimpleNamespace(refresh=lambda: None),
        scheduler_page=SimpleNamespace(update_progress=lambda *a: None),
        clamd_health=SimpleNamespace(is_down=False),
        _update_next_run_label=lambda: None,
        bg_worker=None,
    )
    return fake, messages, entries, log


def _deliver(fake, got):
    for message in got["aborted"]:
        mw.MainWindow._on_bg_aborted(fake, message)
    for path in got["unreadable"]:
        mw.MainWindow._on_bg_unreadable_dir(fake, path, "Permission denied")
    mw.MainWindow._on_bg_finished(fake, got["finished"][0])


@non_root
def test_pianificazione_radice_illeggibile_non_risulta_eseguita(tmp_path, chmod_restore, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    root = _tree(tmp_path)
    chmod_restore(root, 0o300)
    fake, messages, entries, _ = _bg_window(tmp_path, root)
    _deliver(fake, _run_worker(target=root, strict_roots=True))
    assert not any(m.startswith("Scansione automatica completata") for m in messages)
    assert messages and messages[0].startswith("Scansione programmata non completata")
    assert [e[0] for e in entries] == ["Programmata (non completata)"]
    assert "schedule_last_run" not in fake.settings.values


@non_root
def test_pianificazione_sottocartella_illeggibile_nel_log(tmp_path, chmod_restore, monkeypatch):
    monkeypatch.setattr(mw, "DEFAULT_LOGS_DIR", tmp_path / "logs")
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    fake, messages, entries, log = _bg_window(tmp_path, root)
    _deliver(fake, _run_worker(target=root, strict_roots=True))
    assert log == [f"CARTELLA NON LEGGIBILE — {root / 'chiusa'}: Permission denied"]
    assert "1 cartelle non leggibili" in messages[0] and "0 errori" in messages[0]
    ((_, _, totals),) = entries
    assert totals == ScanTotals(scanned=2, unreadable_dirs=1)
    assert "schedule_last_run" in fake.settings.values


# -- CLI -------------------------------------------------------------------

class CliClient(WalkingClient):
    def ping(self):
        CliClient.pinged = True
        return True


@pytest.fixture
def cli_env(monkeypatch):
    CliClient.pinged = False
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: CliClient())


@non_root
def test_cli_radice_illeggibile_uscita_2(tmp_path, chmod_restore, cli_env, capsys):
    root = _tree(tmp_path)
    chmod_restore(root, 0o300)
    assert cli.main(["scan", str(root)]) == 2
    assert "non è leggibile" in capsys.readouterr().err
    assert CliClient.pinged is False  # prima della connessione a clamd


@non_root
def test_cli_sottocartella_illeggibile_uscita_0(tmp_path, chmod_restore, cli_env, capsys):
    root = _tree(tmp_path)
    chmod_restore(root / "chiusa", 0o000)
    log = tmp_path / "errori.log"
    assert cli.main(["scan", str(root), "--quiet", "--log-errors", str(log)]) == 0
    out = capsys.readouterr()
    # Il motivo è strerror, tradotto se un test precedente ha impostato
    # la locale: si confronta con quello del sistema.
    reason = os.strerror(errno.EACCES)
    assert f"CARTELLA NON LEGGIBILE — {root / 'chiusa'}: {reason}" in out.err
    assert "2 file scansionati, 0 infetti, 0 errori." in out.out
    assert "1 cartelle non leggibili" in out.out and "--exclude" in out.out
    assert str(root / "chiusa") in log.read_text()


def test_cli_radice_vuota_uscita_0(tmp_path, cli_env):
    vuota = tmp_path / "vuota"
    vuota.mkdir()
    assert cli.main(["scan", str(vuota)]) == 0


# -- cronologia ------------------------------------------------------------

def test_cronologia_voce_vecchia_senza_campo(tmp_path):
    hist = mw.HistoryManager(tmp_path / "history.json")
    (tmp_path / "history.json").write_text(json.dumps([{
        "timestamp": "2026-09-01 10:00:00", "type": "Manuale", "target": "/x",
        "scanned": 5, "infections": 0, "errors": 1,
    }]))
    (old,) = hist.get_entries()
    assert ScanTotals.from_entry(old) == ScanTotals(scanned=5, errors=1)

    hist.add_entry("Manuale", "/y", ScanTotals(scanned=3, unreadable_dirs=2))
    new = hist.get_entries()[-1]
    assert new["unreadable_dirs"] == 2 and new["too_large"] == 0

    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    page = mw.HistoryPage(hist)
    # Righe dalla più recente: la nuova, poi quella senza il campo.
    assert page.table.item(0, 7).text() == "2"
    assert page.table.item(1, 7).text() == "0"
