"""
Regressione: evasione della quarantena via hardlink + symlink.

quarantine_file() apre il file con O_NOFOLLOW, ne prende (st_dev, st_ino)
con fstat(), sposta con rename() e poi verifica che l'inode finito in
quarantena sia lo stesso. La verifica serve a intercettare una
sostituzione del path fra l'open() e il rename().

Il controllo era aggirabile perché usava os.stat(), che segue i symlink:

  1. il malware crea un hardlink del file infetto altrove — stesso inode,
     quindi stessi st_dev/st_ino;
  2. sostituisce il path originale con un symlink verso quell'hardlink;
  3. rename() sposta il symlink in quarantena;
  4. os.stat() sulla destinazione segue il link e ritrova l'inode di
     partenza: il confronto passava.

Esito: in quarantena finiva un symlink, il contenuto infetto restava
fuori ed eseguibile, l'indice registrava una quarantena riuscita e la UI
mostrava il file come neutralizzato. "Elimina definitivamente" rimuoveva
il solo link.

L'attaccante deve essere same-uid (il malware che gira mentre lo si
scansiona) e vincere una gara, ma può riprovare in ciclo.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from klamav_py.quarantine import Quarantine, QuarantineError


def _sostituisci_con_symlink_a_hardlink(vittima: Path, nascosto: Path) -> None:
    """La manovra del malware, eseguita dentro la finestra di gara."""
    os.link(vittima, nascosto)  # hardlink: preserva (st_dev, st_ino)
    os.unlink(vittima)
    os.symlink(nascosto, vittima)


def test_evasione_hardlink_symlink_viene_rilevata(tmp_path):
    qdir = tmp_path / "quarantena"
    vittima = tmp_path / "malware.bin"
    nascosto = tmp_path / "copia-nascosta"
    vittima.write_bytes(b"CONTENUTO INFETTO")

    manager = Quarantine(qdir)

    # La sostituzione avviene fra l'fstat() e il rename(): si aggancia a
    # os.rename, che nel flusso reale è la prima cosa dopo la finestra.
    rename_reale = os.rename

    def rename_sabotato(src, dst):
        _sostituisci_con_symlink_a_hardlink(Path(src), nascosto)
        return rename_reale(src, dst)

    with patch("klamav_py.quarantine.os.rename", side_effect=rename_sabotato):
        with pytest.raises(QuarantineError, match="sostituito durante la quarantena"):
            manager.quarantine_file(vittima)

    # La quarantena non deve contenere nulla oltre all'indice: il symlink
    # spostato va rimosso dal ramo di cleanup.
    file_di_servizio = {"index.json", "index.json.lock", "index.json.tmp"}
    residui = [p for p in qdir.iterdir() if p.name not in file_di_servizio]
    assert not residui, f"voce spuria rimasta in quarantena: {residui}"

    # E nessuna entry deve risultare registrata: una quarantena fallita
    # che compare nell'indice è esattamente la bugia da evitare.
    assert manager.list_entries() == []


def test_permessi_applicati_alla_voce_in_quarantena(tmp_path):
    """
    I permessi 0o400 devono finire sull'inode quarantenato.

    Con Path.chmod() — che segue i symlink — nello scenario di evasione
    finivano sul file infetto rimasto fuori dalla quarantena. Qui il caso
    è quello normale, senza attacco: serve a garantire che il passaggio a
    os.fchmod() non abbia cambiato il comportamento atteso.
    """
    qdir = tmp_path / "quarantena"
    vittima = tmp_path / "malware.bin"
    vittima.write_bytes(b"CONTENUTO INFETTO")
    vittima.chmod(0o755)

    manager = Quarantine(qdir)
    entry = manager.quarantine_file(vittima)

    quarantenato = Path(entry.quarantined_path)
    assert quarantenato.exists()
    assert not quarantenato.is_symlink()
    assert stat.S_IMODE(os.lstat(quarantenato).st_mode) == 0o400
    assert quarantenato.read_bytes() == b"CONTENUTO INFETTO"
    assert not vittima.exists()

    # La mode originale va conservata per il restore, non persa dal chmod.
    assert entry.original_mode == 0o755
