"""
Il conteggio delle voci della quarantena che le Impostazioni mostrano
prima di cambiarla è in sola lettura.

Prima _quarantine_count costruiva Quarantine nel thread della GUI: come
effetto collaterale completava o annullava le quarantene interrotte e
metteva da parte un indice corrotto, solo per contare.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import klamav_py.gui.main_window as mw  # noqa: E402
from klamav_py.quarantine import Quarantine, QuarantineError, peek_entries  # noqa: E402


class Crash(BaseException):
    pass


def test_conteggio_non_recupera_le_operazioni_interrotte(tmp_path):
    q = Quarantine(tmp_path / "q")
    home = tmp_path / "home"
    home.mkdir()
    for nome in ("a", "b"):
        (home / nome).write_bytes(b"x")
    q.quarantine_file(home / "a", "Sig")
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)), \
            patch.object(Quarantine, "_write_index", side_effect=Crash()):
        with pytest.raises(Crash):
            q.quarantine_file(home / "b", "Sig")
    intenti = sorted(q.dir.glob(".*.intent"))
    assert intenti

    assert mw._quarantine_count(q.dir) == 1
    assert sorted(q.dir.glob(".*.intent")) == intenti  # nessun recupero
    assert Quarantine(q.dir).recovered  # il recupero resta da fare


def test_indice_corrotto_non_messo_da_parte(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.index_path.write_text("{non json")
    assert mw._quarantine_count(q.dir) == 0
    assert q.index_path.read_text() == "{non json" and q.corrupt_backups() == []
    with pytest.raises(QuarantineError):
        peek_entries(q.dir)


def test_directory_mancante_non_creata(tmp_path):
    assert mw._quarantine_count(tmp_path / "manca") == 0
    assert peek_entries(tmp_path / "manca") == []
    assert not (tmp_path / "manca").exists()


def test_indice_valido(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.index_path.write_text(json.dumps([
        {"original_path": "/h/a", "quarantined_path": str(q.dir / "1"), "timestamp": 1.0},
        {"original_path": "/h/b", "quarantined_path": str(q.dir / "2"), "timestamp": 2.0},
    ]))
    assert mw._quarantine_count(q.dir) == 2
