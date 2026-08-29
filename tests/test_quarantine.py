"""
Test per Quarantine: usano solo il filesystem temporaneo di pytest
(tmp_path), nessuna dipendenza da clamd.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

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


def test_quarantine_file_collisione_nome(tmp_path, quarantine):
    # Due file con lo STESSO NOME, provenienti da directory diverse:
    # anche senza forzare lo stesso timestamp, il nome file originale
    # non fa più parte del nome su disco in quarantena (fix permessi/
    # prevedibilità), quindi la collisione è strutturalmente impossibile
    # ora. Verifichiamo comunque che entrambi finiscano in quarantena
    # con percorsi distinti.
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


def test_quarantine_file_nome_su_disco_non_prevedibile(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path, name="virus_riconoscibile.exe")
    entry = quarantine.quarantine_file(original)

    # Il nome originale non deve comparire nel nome del file su disco:
    # resta solo nell'indice (che ha permessi 0600, non leggibile da altri).
    assert "virus_riconoscibile" not in Path(entry.quarantined_path).name


def test_quarantine_file_permessi_neutralizzati(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path, name="script.sh")
    original.chmod(0o755)

    entry = quarantine.quarantine_file(original)

    mode = stat.S_IMODE(Path(entry.quarantined_path).stat().st_mode)
    assert mode == 0o400, "il file in quarantena deve essere read-only e non eseguibile"
    assert entry.original_mode == 0o755, "la mode originale va salvata per il restore"


def test_restore_ripristina_permessi_originali(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path, name="script.sh")
    original.chmod(0o755)
    entry = quarantine.quarantine_file(original)

    restored = quarantine.restore(entry.quarantined_path)

    mode = stat.S_IMODE(restored.stat().st_mode)
    assert mode == 0o755, "il restore deve riportare la mode originale, non lasciare 0400"


def test_restore_su_percorso_occupato_rifiutato(tmp_path, quarantine):
    original = _crea_file_infetto(tmp_path)
    entry = quarantine.quarantine_file(original)

    # Qualcun altro ha ricreato un file allo stesso percorso originale
    # nel frattempo: il restore NON deve sovrascriverlo silenziosamente.
    original.write_text("contenuto nuovo, non deve essere perso")

    with pytest.raises(QuarantineError):
        quarantine.restore(entry.quarantined_path)

    assert original.read_text() == "contenuto nuovo, non deve essere perso"
    # La voce resta in indice: il file è ancora recuperabile in quarantena.
    assert len(quarantine.list_entries()) == 1


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
    assert not Path(entry.quarantined_path).exists()
    assert quarantine.list_entries() == []


def test_quarantine_file_rifiuta_symlink(tmp_path, quarantine):
    # Un file infetto può essere un symlink che punta altrove: quarantenare
    # "il file" significherebbe seguire il link e spostare il bersaglio (o,
    # a seconda dell'implementazione, il link stesso lasciando il bersaglio
    # intatto e accessibile) — nessuno dei due esiti isola davvero nulla.
    bersaglio = tmp_path / "bersaglio.txt"
    bersaglio.write_text("contenuto reale, non deve essere toccato")
    link = tmp_path / "link_malevolo.txt"
    link.symlink_to(bersaglio)

    with pytest.raises(QuarantineError):
        quarantine.quarantine_file(link)

    assert link.is_symlink(), "il link non deve essere stato spostato"
    assert bersaglio.read_text() == "contenuto reale, non deve essere toccato"
    assert quarantine.list_entries() == []


def test_quarantine_file_rifiuta_fifo(tmp_path, quarantine):
    # Difesa in profondità: os.open con O_NOFOLLOW segue comunque i file
    # speciali non-symlink (FIFO, device...). Il controllo S_ISREG dopo la
    # fstat() deve rigettarli esplicitamente invece di provare a spostarli.
    fifo_path = tmp_path / "fifo_sospetto"
    os.mkfifo(fifo_path)

    with pytest.raises(QuarantineError):
        quarantine.quarantine_file(fifo_path)

    assert quarantine.list_entries() == []


def test_restore_reclama_destinazione_atomicamente(tmp_path, quarantine):
    # Verifica che il meccanismo O_CREAT|O_EXCL lasci comunque il file
    # originale recuperabile in quarantena quando la destinazione è già
    # occupata (stesso comportamento visibile della versione precedente
    # basata su exists(), ma senza la finestra di race nel mezzo).
    original = _crea_file_infetto(tmp_path)
    entry = quarantine.quarantine_file(original)

    original.write_text("qualcun altro ha ricreato questo file")

    with pytest.raises(QuarantineError):
        quarantine.restore(entry.quarantined_path)

    # Nessun placeholder vuoto lasciato al posto del contenuto rioccupato.
    assert original.read_text() == "qualcun altro ha ricreato questo file"
    assert Path(entry.quarantined_path).exists(), "il file resta recuperabile in quarantena"
    assert len(quarantine.list_entries()) == 1


def test_index_json_vecchio_senza_original_mode(tmp_path, quarantine):
    # Compatibilità con voci scritte da una versione precedente, senza
    # il campo original_mode: non deve sollevare TypeError in lettura.
    import json

    old_style_entry = {
        "original_path": "/tmp/vecchio.txt",
        "quarantined_path": str(quarantine.dir / "vecchia_voce"),
        "signature": "Old-Sig",
        "timestamp": 123.0,
        # niente "original_mode" qui, come nelle versioni precedenti
    }
    quarantine.index_path.write_text(json.dumps([old_style_entry]))

    entries = quarantine.list_entries()
    assert len(entries) == 1
    assert entries[0].original_mode is None
