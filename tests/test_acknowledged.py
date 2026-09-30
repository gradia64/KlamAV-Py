"""
Presa visione delle segnalazioni non spostate (acknowledged.py e CLI).

Il caso d'origine: due email di phishing nel cestino di KMail, lasciate
al loro posto dalla policy («solo segnalazione»), segnalate ogni notte dal
timer con uscita 1 e notifica. Il codice di uscita resta 1 per una
segnalazione NUOVA; una già valutata non conta più, finché il contenuto
non cambia.
"""

from __future__ import annotations

import json
import os
import stat
from collections import Counter
from pathlib import Path

import pytest

import klamav_py.cli as cli
from klamav_py.acknowledged import (
    EXPIRY_DAYS,
    AckRegistry,
    RegistryError,
    ScanAcknowledgements,
    delete_if_same,
    hash_file,
)
from klamav_py.clamd_client import ClamdClient, ClamdEndpoint, ScanResult

PHISHING = "Heuristics.Phishing.Email.SpoofedDomain"
TROJAN = "Win.Trojan.Agent-1"
MARK = b"FINTO-PHISHING"


# -- registro ----------------------------------------------------------------

def _reg(tmp_path, now=lambda: 1_800_000_000.0):
    return AckRegistry(tmp_path / "data" / "acknowledged.json", now=now)


def test_round_trip_e_permessi(tmp_path):
    reg = _reg(tmp_path)
    e = reg.acknowledge("a" * 64, PHISHING, Path("/home/u/mail/new/1"))
    assert reg.entries() == [e]
    assert stat.S_IMODE(os.stat(reg.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(reg.path.parent).st_mode) == 0o700
    snap = reg.snapshot()
    from klamav_py.acknowledged import FileIdentity
    assert snap.check(FileIdentity("a" * 64, 0, 0), PHISHING)
    assert not snap.check(FileIdentity("a" * 64, 0, 0), "Heuristics.Altro")
    assert not snap.check(FileIdentity("b" * 64, 0, 0), PHISHING)


@pytest.mark.parametrize("contenuto", [
    "{non json",
    "[]",
    '{"entries": {}}',
    json.dumps({"entries": [{"sha256": "x", "signature": PHISHING, "first_path": "/a",
                             "acknowledged_at": 1, "last_seen": 1}]}),
    json.dumps({"entries": [{"sha256": "a" * 64, "signature": 3, "first_path": "/a",
                             "acknowledged_at": 1, "last_seen": 1}]}),
    json.dumps({"entries": [{"sha256": "a" * 64, "signature": PHISHING, "first_path": "/a",
                             "acknowledged_at": True, "last_seen": 1}]}),
    json.dumps({"entries": [{"sha256": "a" * 64, "signature": PHISHING, "first_path": "/a\0b",
                             "acknowledged_at": 1, "last_seen": 1}]}),
], ids=["json", "lista", "entries-oggetto", "sha", "firma", "bool", "nul"])
def test_registro_non_valido_messo_da_parte(tmp_path, contenuto):
    reg = _reg(tmp_path)
    reg.path.parent.mkdir(parents=True)
    reg.path.write_text(contenuto)
    assert reg.entries() == []
    (backup,) = reg.path.parent.glob("acknowledged.json.corrupt-*")
    assert backup.read_text() == contenuto  # mai cancellato
    assert reg.last_recovery[0] == backup
    # Si riparte da vuoto e si può scrivere.
    reg.acknowledge("a" * 64, PHISHING, Path("/a"))
    assert len(reg.entries()) == 1


def test_tetto_di_dimensione(tmp_path, monkeypatch):
    import klamav_py.acknowledged as ack
    monkeypatch.setattr(ack, "MAX_REGISTRY_BYTES", 100)
    reg = _reg(tmp_path)
    reg.path.parent.mkdir(parents=True)
    reg.path.write_text(json.dumps({"entries": [], "pad": "x" * 200}))
    assert reg.entries() == [] and list(reg.path.parent.glob("*.corrupt-*"))


def test_symlink_al_posto_del_registro_non_seguito(tmp_path):
    reg = _reg(tmp_path)
    reg.path.parent.mkdir(parents=True)
    altrove = tmp_path / "altrove.json"
    altrove.write_text(json.dumps({"entries": []}))
    reg.path.symlink_to(altrove)
    assert reg.entries() == []
    assert altrove.exists() and not reg.path.is_symlink()


def test_pulizia_per_eta(tmp_path):
    ora = [1_800_000_000.0]
    reg = _reg(tmp_path, now=lambda: ora[0])
    reg.acknowledge("a" * 64, PHISHING, Path("/vecchia"))
    reg.acknowledge("b" * 64, PHISHING, Path("/rivista"))
    ora[0] += (EXPIRY_DAYS - 1) * 86400
    reg.mark_seen([("b" * 64, PHISHING)])
    ora[0] += 2 * 86400
    # La vecchia non vale più già alla lettura, e sparisce alla scrittura.
    assert [e.first_path for e in reg.entries()] == ["/rivista"]
    reg.acknowledge("c" * 64, PHISHING, Path("/nuova"))
    salvate = json.loads(reg.path.read_text())["entries"]
    assert sorted(e["first_path"] for e in salvate) == ["/nuova", "/rivista"]


def test_prefisso(tmp_path):
    reg = _reg(tmp_path)
    reg.acknowledge("ab12" + "0" * 60, PHISHING, Path("/a"))
    reg.acknowledge("ab12" + "0" * 60, "Heuristics.Altro", Path("/a"))
    reg.acknowledge("ab34" + "0" * 60, PHISHING, Path("/b"))
    assert len(reg.match_prefix("AB12")) == 2  # stesso file, due firme
    for sbagliato in ("ab", "ffff", "zz", ""):
        with pytest.raises(RegistryError):
            reg.match_prefix(sbagliato)


# -- hash ed eliminazione ------------------------------------------------------

def test_hash_rifiuta_symlink_e_non_regolari(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(MARK)
    (tmp_path / "l").symlink_to(f)
    assert hash_file(f).sha256 == __import__("hashlib").sha256(MARK).hexdigest()
    with pytest.raises(OSError):
        hash_file(tmp_path / "l")
    with pytest.raises(OSError):
        hash_file(tmp_path)


def test_eliminazione_solo_dello_stesso_inode(tmp_path):
    f = tmp_path / "msg"
    f.write_bytes(MARK)
    mostrato = hash_file(f)
    # Sostituito dopo la visualizzazione: il nuovo nasce prima che il
    # vecchio sparisca, così non può riusarne l'inode.
    nuovo = tmp_path / "tmp"
    nuovo.write_bytes(b"altro")
    os.replace(nuovo, f)
    with pytest.raises(RegistryError, match="sostituito"):
        delete_if_same(f, mostrato)
    assert f.read_bytes() == b"altro"

    f2 = tmp_path / "msg2"
    f2.write_bytes(MARK)
    ident = hash_file(f2)
    f2.unlink()
    f2.symlink_to(tmp_path / "msg")
    with pytest.raises(RegistryError, match="regolare"):
        delete_if_same(f2, ident)
    assert (tmp_path / "msg").exists()

    f3 = tmp_path / "msg3"
    f3.write_bytes(MARK)
    delete_if_same(f3, hash_file(f3))
    assert not f3.exists()


def test_errore_di_lettura_trattato_come_nuovo(tmp_path):
    reg = _reg(tmp_path)
    acks = ScanAcknowledgements(reg)
    identity = acks.identify(tmp_path / "sparito")
    assert identity is None and not acks.already_evaluated(identity, PHISHING)
    assert acks.problems


# -- CLI -----------------------------------------------------------------------

class FakeClient:
    """Traversata vera, verdetto dal contenuto: MARK euristico, b"TROJAN"
    da quarantena, altro pulito."""

    scanned: list = []

    def __init__(self, **kw):
        self.skipped = Counter()

    def ping(self):
        return True

    def scan_stream(self, root, exclude_dirs=None, **kw):
        for f in ClamdClient._iter_files(Path(root).resolve(), exclude_dirs):
            FakeClient.scanned.append(f)
            data = f.read_bytes()
            if MARK in data:
                yield ScanResult(str(f), "FOUND", PHISHING)
            elif b"TROJAN" in data:
                yield ScanResult(str(f), "FOUND", TROJAN)
            else:
                yield ScanResult(str(f), "OK")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".local/share/local-mail/trash/new").mkdir(parents=True)
    (h / ".local/share/local-mail/trash/cur").mkdir(parents=True)
    (h / "Scaricati").mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(ClamdEndpoint, "new_client", lambda self, **k: FakeClient())
    FakeClient.scanned = []
    return h


def _mail(home, name="1695.R42.host", content=MARK + b" messaggio"):
    p = home / ".local/share/local-mail/trash/new" / name
    p.write_bytes(content)
    return p


def _timer_scan(home):
    """Come la unit del timer."""
    return cli.main(["scan", str(home), "--quarantine",
                     str(home / ".local/share/klamav-py/quarantine"), "--quiet"])


def test_flusso_completo(home, capsys):
    mail = _mail(home)
    assert _timer_scan(home) == 1  # segnalazione nuova: notifica
    out = capsys.readouterr().out
    assert "NON messo in quarantena" in out and "--acknowledge" in out

    assert cli.main(["--acknowledge", str(mail)]) == 0
    assert "Presa visione registrata" in capsys.readouterr().out

    assert _timer_scan(home) == 0
    out = capsys.readouterr().out
    assert f"GIÀ VALUTATO: {mail} ({PHISHING})" in out  # anche con --quiet
    assert "0 infetti" in out and "1 segnalazioni già valutate" in out
    assert mail.exists()

    assert cli.main(["--list-acknowledged"]) == 0
    riga = capsys.readouterr().out.strip()
    prefisso = riga.split()[0]
    assert len(prefisso) == 12 and PHISHING in riga and str(mail) in riga

    assert cli.main(["--unacknowledge", prefisso]) == 0
    assert _timer_scan(home) == 1


def test_messaggio_letto_rinominato_resta_valutato(home, capsys):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    letto = home / ".local/share/local-mail/trash/cur" / (mail.name + ":2,S")
    os.rename(mail, letto)
    assert _timer_scan(home) == 0
    assert f"GIÀ VALUTATO: {letto}" in capsys.readouterr().out


def test_contenuto_modificato_torna_segnalato(home, capsys):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    mail.write_bytes(MARK + b" messaggio cambiato")
    assert _timer_scan(home) == 1
    assert f"INFETTO: {mail}" in capsys.readouterr().out


def test_stesso_hash_da_quarantena_va_in_quarantena(home, capsys, tmp_path):
    # Stesso contenuto e stessa firma, ma fuori dagli archivi di posta e con
    # una firma non euristica: la presa visione non conta. Si registra a
    # mano l'hash con la firma da quarantena (la CLI lo rifiuterebbe).
    virus = home / "Scaricati" / "virus.exe"
    virus.write_bytes(b"TROJAN")
    AckRegistry().acknowledge(hash_file(virus).sha256, TROJAN, virus)
    assert _timer_scan(home) == 1
    out = capsys.readouterr().out
    assert "messo in quarantena" in out and "GIÀ VALUTATO" not in out
    assert not virus.exists()


def test_senza_quarantena_la_presa_visione_vale(home, capsys):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    assert cli.main(["scan", str(home)]) == 0
    assert "GIÀ VALUTATO" in capsys.readouterr().out


@pytest.mark.parametrize("caso", ["pulito", "da quarantena", "symlink", "inesistente"])
def test_acknowledge_rifiutato_senza_voci(home, capsys, caso):
    if caso == "pulito":
        p = home / "Scaricati" / "nota.txt"
        p.write_text("ciao")
    elif caso == "da quarantena":
        p = home / "Scaricati" / "virus.exe"
        p.write_bytes(b"TROJAN")
    elif caso == "symlink":
        p = home / "Scaricati" / "link"
        p.symlink_to(_mail(home))
    else:
        p = home / "manca"
    assert cli.main(["--acknowledge", str(p)]) == 2
    assert "Nessuna presa visione registrata" in capsys.readouterr().err
    assert AckRegistry().entries() == []


def test_acknowledge_file_cambiato_durante_la_verifica(home, capsys, monkeypatch):
    mail = _mail(home)
    reale = FakeClient.scan_stream

    def e_intanto_cambia(self, root, **kw):
        yield from reale(self, root, **kw)
        Path(root).write_bytes(MARK + b" altro")

    monkeypatch.setattr(FakeClient, "scan_stream", e_intanto_cambia)
    assert cli.main(["--acknowledge", str(mail)]) == 2
    assert "cambiato" in capsys.readouterr().err
    assert AckRegistry().entries() == []


def test_unacknowledge_per_percorso(home, capsys):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    assert cli.main(["--unacknowledge", str(mail)]) == 0
    assert AckRegistry().entries() == []


@pytest.mark.parametrize("valore", ["ffffffffffff", "zzz"])
def test_unacknowledge_inesistente_o_non_valido(home, capsys, valore):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    assert cli.main(["--unacknowledge", valore]) == 2
    assert "Revoca non eseguita" in capsys.readouterr().err
    assert len(AckRegistry().entries()) == 1


def test_unacknowledge_prefisso_ambiguo(home, capsys):
    reg = AckRegistry()
    reg.acknowledge("ab" + "1" * 62, PHISHING, Path("/a"))
    reg.acknowledge("ab" + "2" * 62, PHISHING, Path("/b"))
    assert cli.main(["--unacknowledge", "ab"]) == 2
    assert "file diversi" in capsys.readouterr().err
    assert len(reg.entries()) == 2


def test_errore_di_lettura_durante_l_hash_trattato_come_nuovo(home, capsys, monkeypatch):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    import klamav_py.acknowledged as ack

    def rotto(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(ack, "hash_file", rotto)
    assert _timer_scan(home) == 1
    out = capsys.readouterr()
    assert f"INFETTO: {mail}" in out.out and "per l'hash" in out.err


def test_registro_corrotto_segnalazione_nuova(home, capsys):
    mail = _mail(home)
    assert cli.main(["--acknowledge", str(mail)]) == 0
    AckRegistry().path.write_text("{rotto")
    assert _timer_scan(home) == 1
    assert "messo da parte" in capsys.readouterr().err


def test_opzioni_con_comando_rifiutate(home):
    with pytest.raises(SystemExit):
        cli.main(["--list-acknowledged", "ping"])
    with pytest.raises(SystemExit):
        cli.main([])


def test_scansione_senza_segnalazioni_non_legge_il_registro(home, monkeypatch):
    (home / "Scaricati" / "nota.txt").write_text("ciao")
    monkeypatch.setattr(AckRegistry, "snapshot", lambda self: pytest.fail("registro letto"))
    assert _timer_scan(home) == 0
