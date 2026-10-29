"""
Segnalazioni da valutare persistenti: anche quelle del timer di sistema
arrivano alla pagina Segnalazioni.

Prima la pagina mostrava solo le segnalazioni delle scansioni della
finestra, nella sessione corrente. Quelle del timer di sistema restavano
nel journal: la notifica del mattino diceva «file infetti» e per sapere
quali bisognava cercarli con journalctl. Ora ogni scansione (CLI, quindi
timer, e worker della GUI) scrive ogni rilevamento «solo segnalazione»
nuovo nel registro pending_reports appena lo trova, e la pagina lo legge.

I simboli nuovi si cercano con importlib/getattr: sul commit precedente
questi test falliscono per asserzione, non per ImportError.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import stat
import time
from collections import Counter
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.acknowledged import AckRegistry, PendingReport, hash_file
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ClamdUnavailable, ScanResult

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"
TROJAN = "Win.Trojan.Agent-1"
RADICE = Path(__file__).resolve().parent.parent


def _module():
    assert importlib.util.find_spec("klamav_py.pending_reports") is not None, (
        "nessun registro delle segnalazioni da valutare: quelle del timer restano solo nel journal"
    )
    return importlib.import_module("klamav_py.pending_reports")


def _store(**kw):
    return _module().PendingStore(**kw)


def _report(path: Path, found_at: float | None = None, signature: str = PHISHING) -> PendingReport:
    return PendingReport(str(path), signature, "firma euristica: solo segnalazione",
                         hash_file(path), time.time() if found_at is None else found_at)


# -- il registro --------------------------------------------------------------

def test_registro_privato_e_una_voce_per_percorso_e_firma(tmp_path):
    msg = tmp_path / "msg"
    msg.write_bytes(b"phishing")
    store = _store(path=tmp_path / "data" / "pending.json")
    adesso = time.time()
    store.record(_report(msg, adesso - 60))
    msg.write_bytes(b"phishing, versione nuova")
    store.record(_report(msg, adesso))
    (r,) = store.reports()
    assert r.found_at == adesso and r.identity == hash_file(msg)
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_identita_mancante_si_conserva(tmp_path):
    store = _store(path=tmp_path / "pending.json")
    adesso = time.time()
    store.record(PendingReport(str(tmp_path / "x"), PHISHING, "", None, adesso))
    (r,) = store.reports()
    assert r.identity is None and r.found_at == adesso


def test_registro_non_valido_messo_da_parte(tmp_path):
    store = _store(path=tmp_path / "pending.json")
    store.path.write_text(json.dumps({"entries": [{"path": 3}]}))
    assert store.reports() == []
    (backup,) = tmp_path.glob("pending.json.corrupt-*")
    assert store.last_recovery is not None and store.last_recovery[0] == backup


def test_voci_scadute(tmp_path):
    mod = _module()
    msg = tmp_path / "msg"
    msg.write_bytes(b"phishing")
    now = [1_000_000_000.0]
    store = _store(path=tmp_path / "pending.json", now=lambda: now[0])
    store.record(_report(msg, now[0]))
    now[0] += mod.EXPIRY_DAYS * 86400 + 1
    assert store.reports() == []


def test_discard_per_percorso_e_per_contenuto(tmp_path):
    a, b, c = (tmp_path / n for n in "abc")
    a.write_bytes(b"uguale")
    b.write_bytes(b"uguale")
    c.write_bytes(b"diverso")
    store = _store(path=tmp_path / "pending.json")
    for f in (a, b, c):
        store.record(_report(f))
    # Presa visione: vale per il contenuto in ogni percorso.
    assert store.discard(contents=[(hash_file(a).sha256, PHISHING)]) == 2
    assert [r.path for r in store.reports()] == [str(c)]
    # Eliminazione: solo quel percorso.
    assert store.discard(locations=[(str(c), PHISHING)]) == 1
    assert store.reports() == []


def test_prune_toglie_valutati_spariti_e_sostituiti(tmp_path):
    valutato, sparito, sostituito, resta = (tmp_path / n for n in ("v", "s", "x", "r"))
    for f in (valutato, sparito, sostituito, resta):
        f.write_bytes(f.name.encode())
    store = _store(path=tmp_path / "pending.json")
    for f in (valutato, sparito, sostituito, resta):
        store.record(_report(f))
    sparito.unlink()
    nuovo = tmp_path / "nuovo"
    nuovo.write_bytes(b"x")
    os.replace(nuovo, sostituito)  # altro inode allo stesso percorso

    result = store.prune(acknowledged={(hash_file(valutato).sha256, PHISHING)})
    assert [r.path for r in result.reports] == [str(resta)] and result.removed == 3
    assert [r.path for r in store.reports()] == [str(resta)]


# -- scritto dalla CLI, quindi dal timer -------------------------------------

class FakeClient:
    """b"PHISH" euristica (solo segnalazione), b"TROJAN" da quarantena;
    con `die_after` clamd sparisce dopo quel file, come una scansione
    interrotta a metà."""

    die_after: str | None = None

    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        return True

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for f in sorted(ClamdClient._iter_files(Path(root).resolve(), exclude_dirs)):
            data = f.read_bytes()
            if b"PHISH" in data:
                yield ScanResult(str(f), "FOUND", PHISHING)
            elif b"TROJAN" in data:
                yield ScanResult(str(f), "FOUND", TROJAN)
            else:
                yield ScanResult(str(f), "OK")
            if FakeClient.die_after == f.name:
                raise ClamdUnavailable("connessione rifiutata", "socket di prova")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "posta").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    monkeypatch.setattr(FakeClient, "die_after", None)
    return h


def _scan(home):
    return cli.main(["scan", str(home), "--quarantine",
                     str(home / ".local/share/klamav-py/quarantine"), "--quiet"])


def test_cli_registra_la_segnalazione(home, capsys):
    msg = home / "posta" / "1"
    msg.write_bytes(b"PHISH")
    virus = home / "virus.exe"
    virus.write_bytes(b"TROJAN")
    assert _scan(home) == 1
    reports = _store().reports()
    assert [(r.path, r.signature) for r in reports] == [(str(msg), PHISHING)], (
        "la segnalazione del timer non è registrata da nessuna parte fuori dal journal"
    )
    assert reports[0].identity == hash_file(msg)
    assert "pagina Segnalazioni" in capsys.readouterr().out


def test_cli_interrotta_conserva_quanto_trovato(home, capsys):
    msg = home / "posta" / "1"
    msg.write_bytes(b"PHISH")
    FakeClient.die_after = "1"
    assert _scan(home) == 2  # scansione incompleta
    assert [r.path for r in _store().reports()] == [str(msg)]


def test_cli_gia_valutato_non_registrato(home):
    msg = home / "posta" / "1"
    msg.write_bytes(b"PHISH")
    AckRegistry().acknowledge(hash_file(msg).sha256, PHISHING, msg)
    assert _scan(home) == 0
    assert _store().reports() == []


# -- scritto dal worker della GUI ------------------------------------------

def test_worker_della_gui_registra_la_segnalazione(home):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from klamav_py.gui.scan_worker import ScanWorker

    msg = home / "posta" / "1"
    msg.write_bytes(b"PHISH")
    found = []
    worker = ScanWorker(endpoint=ClamdEndpoint(), target=home / "posta", client_factory=FakeClient)
    worker.report_only_found.connect(found.append)
    worker.run()
    del worker
    assert [r.path for r in found] == [str(msg)]
    assert [r.path for r in _store().reports()] == [str(msg)]


# -- la notifica del timer rimanda alla GUI ---------------------------------

def test_notifica_del_timer_rimanda_alla_pagina_segnalazioni():
    unit = (RADICE / "debian/klamav-py.klamav-scan-notify.user.service").read_text()
    infetti = next(line for line in unit.splitlines() if "exit-code:1)" in line)
    assert "Segnalazioni" in infetti
