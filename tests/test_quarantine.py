"""
Test per Quarantine: usano solo il filesystem temporaneo di pytest
(tmp_path), nessuna dipendenza da clamd.
"""

from __future__ import annotations

import stat

import pytest

from klamav_py.quarantine import Quarantine, QuarantineError


@pytest.fixture
def quarantine(tmp_path):
    return Quarantine(tmp_path / "quarantena")


def _crea_file_infetto(tmp_path, name="infetto.txt", content=b"contenuto"):
    original = tmp_path / name
    original.write_bytes(content)
    return original


def test_directory_creata_con_permessi_ristretti(quarantine):
    mode = stat.S_IMODE(quarantine.dir.stat().st_mode)
    assert mode == 0o700
    assert quarantine.index_path.exists()


def test_quarantine_file_sposta_e_registra(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path)

    entry = quarantine.quarantine_file(original, signature="Eicar-Test-Signature")

    assert not original.exists()
    assert entry.original_path == str(original)
    assert entry.signature == "Eicar-Test-Signature"

    entries = quarantine.list_entries()
    assert len(entries) == 1
    assert entries[0].quarantined_path == entry.quarantined_path


def test_quarantine_file_collisione_nome(tmp_path, quarantine, monkeypatch):
    # Forziamo lo stesso timestamp per due file con lo stesso nome,
    # provenienti da directory diverse, per verificare che non si
    # sovrascrivano a vicenda (TODO storico di klamav 0.22).
    import klamav_py.quarantine as qmod

    monkeypatch.setattr(qmod.time, "time", lambda: 1000.0)

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    file_a = dir_a / "stesso_nome.txt"
    file_b = dir_b / "stesso_nome.txt"
    file_a.write_text("uno")
    file_b.write_text("due")

    entry_a = quarantine.quarantine_file(file_a)
    entry_b = quarantine.quarantine_file(file_b)

    assert entry_a.quarantined_path != entry_b.quarantined_path
    assert len(quarantine.list_entries()) == 2


def test_quarantine_file_non_esistente(tmp_path, quarantine):
    with pytest.raises(QuarantineError):
        quarantine.quarantine_file(tmp_path / "non_esiste.txt")


def test_restore_riporta_al_percorso_originale(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path)
    entry = quarantine.quarantine_file(original)

    restored = quarantine.restore(entry.quarantined_path)

    assert restored == original
    assert original.exists()
    assert quarantine.list_entries() == []


def test_restore_path_sconosciuto(quarantine):
    with pytest.raises(QuarantineError):
        quarantine.restore("/percorso/mai/registrato")


def test_delete_rimuove_file_e_voce_indice(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path)
    entry = quarantine.quarantine_file(original)

    quarantine.delete(entry.quarantined_path)

    assert not original.exists()
    from pathlib import Path
    assert not Path(entry.quarantined_path).exists()
    assert quarantine.list_entries() == []
