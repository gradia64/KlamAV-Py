"""Regola "solo segnalazione": firme euristiche e archivi di posta."""
from __future__ import annotations

from pathlib import Path

import pytest

from klamav_py import cli
from klamav_py.clamd_client import ScanResult
from klamav_py.quarantine_policy import (
    REASON_HEURISTIC, REASON_MAIL_STORE, QuarantinePolicy, default_report_only_dirs,
)


# --- regola ------------------------------------------------------------------

@pytest.fixture
def policy(tmp_path):
    return QuarantinePolicy(default_report_only_dirs(tmp_path))


def test_firma_euristica_solo_segnalata(policy, tmp_path):
    d = policy.decide(tmp_path / "Scaricati" / "x.eml", "Heuristics.Phishing.Email.SpoofedDomain")
    assert not d.quarantine and d.reason == REASON_HEURISTIC


def test_malware_normale_in_quarantena(policy, tmp_path):
    assert policy.decide(tmp_path / "Scaricati" / "x.exe", "Win.Trojan.Agent-1").quarantine


@pytest.mark.parametrize("rel", [
    ".local/share/akonadi/file_db_data/63/34563_r0",
    ".local/share/local-mail/inbox/cur/123:2,S",
    ".thunderbird/abc.default/Mail/Local Folders/Inbox",
    ".local/share/evolution/mail/local/cur/1",
    "Maildir/cur/1",
])
def test_archivi_di_posta_solo_segnalati(policy, tmp_path, rel):
    d = policy.decide(tmp_path / rel, "Email.Phishing.Pdf-1")
    assert not d.quarantine and d.reason == REASON_MAIL_STORE


def test_nome_simile_non_e_un_archivio(policy, tmp_path):
    # is_relative_to lavora per componenti: ".thunderbird-backup" non è ".thunderbird".
    assert policy.decide(tmp_path / ".thunderbird-backup" / "x", "Win.Trojan.A").quarantine


def test_regola_disattivata(tmp_path):
    p = QuarantinePolicy(default_report_only_dirs(tmp_path), enabled=False)
    assert p.decide(tmp_path / ".thunderbird" / "x", "Heuristics.X").quarantine


def test_directory_via_symlink(tmp_path):
    vera = tmp_path / "posta-vera"
    (vera / "cur").mkdir(parents=True)
    (tmp_path / "Maildir").symlink_to(vera)
    p = QuarantinePolicy(default_report_only_dirs(tmp_path))
    assert not p.decide(vera / "cur" / "1", "Email.Phishing.X").quarantine


# --- CLI -----------------------------------------------------------------------

class _FakeClient:
    results: list[ScanResult] = []

    def __init__(self, *a, **kw):
        self.skipped = {}

    def scan_stream(self, root, **kw):
        yield from self.results


def _scan(monkeypatch, tmp_path, capsys, results, *extra):
    monkeypatch.setenv("HOME", str(tmp_path))
    _FakeClient.results = results
    monkeypatch.setattr(cli, "ClamdClient", _FakeClient)
    rc = cli.main(["scan", str(tmp_path), "--quarantine", str(tmp_path / "q"), *extra])
    return rc, capsys.readouterr().out


def _file(tmp_path, rel):
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x")
    return f


def test_cli_euristica_non_spostata(monkeypatch, tmp_path, capsys):
    f = _file(tmp_path, "Scaricati/mail.eml")
    rc, out = _scan(monkeypatch, tmp_path, capsys,
                    [ScanResult(str(f), "FOUND", "Heuristics.Phishing.Email.SpoofedDomain")])
    assert rc == 1
    assert f.exists()
    assert "NON messo in quarantena" in out and "1 infetti solo segnalati" in out


def test_cli_archivio_posta_non_spostato(monkeypatch, tmp_path, capsys):
    f = _file(tmp_path, ".thunderbird/p/Mail/Inbox")
    rc, out = _scan(monkeypatch, tmp_path, capsys, [ScanResult(str(f), "FOUND", "Email.Phishing.X")])
    assert rc == 1 and f.exists()


def test_cli_malware_spostato(monkeypatch, tmp_path, capsys):
    f = _file(tmp_path, "Scaricati/x.exe")
    rc, out = _scan(monkeypatch, tmp_path, capsys, [ScanResult(str(f), "FOUND", "Win.Trojan.A")])
    assert rc == 1 and not f.exists()
    assert "messo in quarantena:" in out and "solo segnalati" not in out


def test_cli_report_only_aggiuntivo(monkeypatch, tmp_path, capsys):
    f = _file(tmp_path, "progetti/campioni/x.exe")
    rc, _ = _scan(monkeypatch, tmp_path, capsys, [ScanResult(str(f), "FOUND", "Win.Trojan.A")],
                  "--report-only", str(tmp_path / "progetti"))
    assert f.exists()


def test_cli_quarantine_all(monkeypatch, tmp_path, capsys):
    f = _file(tmp_path, "Scaricati/mail.eml")
    rc, _ = _scan(monkeypatch, tmp_path, capsys,
                  [ScanResult(str(f), "FOUND", "Heuristics.Phishing.X")], "--quarantine-all")
    assert not f.exists()


# --- ScanWorker ----------------------------------------------------------------

def test_worker_esito_dopo_result_ready(tmp_path):
    pytest.importorskip("PySide6")
    from klamav_py.gui.scan_worker import ScanWorker

    euristico = _file(tmp_path, "a/mail.eml")
    malware = _file(tmp_path, "a/x.exe")
    results = [
        ScanResult(str(euristico), "FOUND", "Heuristics.Phishing.X"),
        ScanResult(str(malware), "FOUND", "Win.Trojan.A"),
    ]

    class Client(_FakeClient):
        def scan_stream(self, root, **kw):
            yield from results

    w = ScanWorker(
        socket_path="unused", target=tmp_path / "a", quarantine_dir=tmp_path / "q",
        auto_quarantine=True, client_factory=Client,
        policy=QuarantinePolicy(default_report_only_dirs(tmp_path)),
    )
    eventi = []
    w.result_ready.connect(lambda r: eventi.append(("result", Path(r.path).name)))
    w.quarantine_outcome.connect(lambda p, o, d: eventi.append((o, Path(p).name)))
    w.run()   # nello stesso thread: segnali consegnati in modo diretto e in ordine

    assert eventi == [
        ("result", "mail.eml"), ("report_only", "mail.eml"),
        ("result", "x.exe"), ("quarantined", "x.exe"),
    ]
    assert euristico.exists() and not malware.exists()
