"""
Test di scan_exclusions: la regola condivisa per le directory escluse
dalle scansioni programmate (timer systemd e pianificazione interna).

Le radici sono sempre passate esplicitamente, come fanno i consumatori:
i risultati non dipendono dalla home reale né dalla directory corrente.
"""

from __future__ import annotations

import os

import pytest

from klamav_py.scan_exclusions import decide

non_root = pytest.mark.skipif(os.getuid() == 0, reason="da root ogni directory è leggibile")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home" / "utente"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _roots(home, target=None):
    return {"timer systemd": home, "pianificazione interna": target or home}


# -- errori bloccanti ----------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   "])
def test_vuoto(raw, home):
    d = decide(raw, roots=_roots(home))
    assert not d.usable and d.stored is None


@pytest.mark.parametrize("raw", ["/a\nb", "/a\tb", "/a\x00b", "/a\x7fb"])
def test_caratteri_di_controllo(raw, home):
    assert not decide(raw, roots=_roots(home)).usable


def test_relativo_senza_cwd_rifiutato(home):
    d = decide("Scaricati", roots=_roots(home))
    assert not d.usable and "assoluto" in d.error


def test_relativo_con_cwd_ancorato(home):
    (home / "vm").mkdir()
    d = decide("vm", roots=_roots(home), cwd=home)
    assert d.usable and d.stored == str(home / "vm")


def test_file_rifiutato(home):
    (home / "disco.iso").write_bytes(b"")
    d = decide(str(home / "disco.iso"), roots=_roots(home))
    assert not d.usable and "directory" in d.error


@pytest.mark.parametrize("quale", ["home", "genitore", "radice fs"])
def test_contiene_la_radice(quale, home):
    raw = {"home": str(home), "genitore": str(home.parent), "radice fs": "/"}[quale]
    d = decide(raw, roots=_roots(home))
    assert not d.usable and "esclusa per intero" in d.error


def test_contiene_solo_la_radice_interna(home):
    # Lista unica: basta che contenga UNA delle radici per essere rifiutata.
    target = home / "Documenti"
    target.mkdir()
    d = decide(str(target), roots=_roots(home, target))
    assert not d.usable
    assert "pianificazione interna" in d.error and "timer" not in d.error


def test_radice_con_symlink_confrontata_risolta(home, tmp_path):
    # La radice passata come symlink: il confronto usa la forma risolta.
    link = tmp_path / "link-home"
    link.symlink_to(home)
    assert not decide(str(home), roots={"timer systemd": link}).usable


# -- accettate, con o senza avvisi ---------------------------------------

def test_directory_normale(home):
    (home / "vm").mkdir()
    d = decide("~/vm", roots=_roots(home))
    assert d.usable and d.warnings == ()
    assert d.stored == str(home / "vm") and d.path == (home / "vm").resolve()


def test_inesistente_avviso(home):
    d = decide(str(home / "non-ancora"), roots=_roots(home))
    assert d.usable
    assert len(d.warnings) == 1 and "non esiste" in d.warnings[0]


def test_symlink_salvato_non_risolto(home, tmp_path):
    # stored conserva il symlink (si risolve di nuovo a ogni scansione),
    # path è la destinazione (per i confronti).
    reale = home / "dati" / "vm"
    reale.mkdir(parents=True)
    (home / "vm").symlink_to(reale)
    d = decide("~/vm", roots=_roots(home))
    assert d.usable
    assert d.stored == str(home / "vm")
    assert d.path == reale.resolve()


def test_fuori_da_una_radice(home):
    # Radici diverse: un'esclusione nella home ma fuori da schedule_target
    # vale per il timer e non per la pianificazione interna.
    target = home / "Documenti"
    target.mkdir()
    (home / "vm").mkdir()
    d = decide(str(home / "vm"), roots=_roots(home, target))
    assert d.usable
    assert len(d.warnings) == 1
    assert "pianificazione interna" in d.warnings[0] and "timer" not in d.warnings[0]


def test_fuori_da_radici_coincidenti_un_solo_avviso(home, tmp_path):
    # schedule_target predefinito = home: un avviso che nomina entrambe.
    fuori = tmp_path / "altrove"
    fuori.mkdir()
    d = decide(str(fuori), roots=_roots(home))
    assert d.usable
    assert len(d.warnings) == 1
    assert "timer systemd e pianificazione interna" in d.warnings[0]


def test_dentro_la_quarantena_accettata(home):
    # Ridondante (la quarantena è già esclusa) ma innocua.
    q = home / ".local" / "share" / "klamav-py" / "quarantine" / "x"
    q.mkdir(parents=True)
    assert decide(str(q), roots=_roots(home)).usable


@non_root
def test_non_verificabile_avviso(home):
    chiusa = home / "chiusa"
    chiusa.mkdir()
    chiusa.chmod(0)
    try:
        d = decide(str(chiusa / "dentro"), roots=_roots(home))
    finally:
        chiusa.chmod(0o700)
    assert d.usable
    assert any("Impossibile verificare" in w for w in d.warnings)
