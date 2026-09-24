"""
Robustezza della quarantena: file di servizio privati, indice corrotto,
sostituzioni innocue durante la quarantena, file su altri filesystem.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from klamav_py import quarantine as qmod
from klamav_py.private_files import PrivateFileError
from klamav_py.quarantine import Quarantine, QuarantineError


@pytest.fixture(autouse=True)
def umask_debian():
    # L'umask 0002 di Debian è quella che produceva index.json.lock 0664.
    vecchio = os.umask(0o002)
    yield
    os.umask(vecchio)


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _infetto(tmp_path: Path, nome="malware.bin", contenuto=b"INFETTO") -> Path:
    f = tmp_path / nome
    f.write_bytes(contenuto)
    return f


# --- file di servizio -----------------------------------------------------

def test_file_di_servizio_privati(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.quarantine_file(_infetto(tmp_path))
    assert _mode(q.dir) == 0o700
    assert _mode(q.index_path) == 0o600
    assert _mode(q.lock_path) == 0o600
    assert not [p for p in q.dir.iterdir() if p.name.endswith(".tmp")]


def test_lock_preesistente_largo_viene_ristretto(tmp_path):
    qdir = tmp_path / "q"
    qdir.mkdir()
    lock = qdir / "index.json.lock"
    lock.write_text("x")
    os.chmod(lock, 0o664)
    Quarantine(qdir)
    assert _mode(lock) == 0o600
    assert lock.read_text() == "x", "il lock non va troncato"


def test_directory_symlink_rifiutata(tmp_path):
    vera = tmp_path / "vera"
    vera.mkdir()
    os.chmod(vera, 0o755)
    link = tmp_path / "q"
    link.symlink_to(vera)
    with pytest.raises(PrivateFileError):
        Quarantine(link)
    assert _mode(vera) == 0o755


# --- indice corrotto o anomalo ---------------------------------------------

def test_indice_corrotto_messo_da_parte_non_cancellato(tmp_path):
    q = Quarantine(tmp_path / "q")
    entry = q.quarantine_file(_infetto(tmp_path))
    q.index_path.write_text('[{"original_path": "/x", troncato')

    assert q.list_entries() == []
    backups = q.corrupt_backups()
    assert len(backups) == 1
    assert "troncato" in backups[0].read_text(), "il contenuto va conservato"
    assert json.loads(q.index_path.read_text()) == []
    # Il file quarantenato resta al suo posto, visibile come orfano.
    assert q.orphans() == [Path(entry.quarantined_path)]


def test_indice_corrotto_non_blocca_nuove_quarantene(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.index_path.write_bytes(b"\xff\xfe non utf-8")
    entry = q.quarantine_file(_infetto(tmp_path))
    assert q.list_entries() == [entry]
    assert len(q.corrupt_backups()) == 1


@pytest.mark.parametrize("contenuto", [
    '{"non": "una lista"}',
    '[42]',
    '[{"original_path": "/x"}]',
    '[{"original_path": 1, "quarantined_path": "/q/a", "timestamp": 0}]',
])
def test_indice_strutturalmente_invalido(tmp_path, contenuto):
    q = Quarantine(tmp_path / "q")
    q.index_path.write_text(contenuto)
    assert q.list_entries() == []
    assert len(q.corrupt_backups()) == 1


def test_campi_sconosciuti_tollerati(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.index_path.write_text(json.dumps([{
        "original_path": "/a", "quarantined_path": str(q.dir / "x"),
        "timestamp": 1.0, "campo_futuro": True,
    }]))
    [e] = q.list_entries()
    assert e.signature is None and e.original_mode is None
    assert q.corrupt_backups() == []


def test_indice_troppo_grande(tmp_path, monkeypatch):
    monkeypatch.setattr(qmod, "MAX_INDEX_BYTES", 64)
    q = Quarantine(tmp_path / "q")
    q.index_path.write_text("[" + ", ".join(["{}"] * 50) + "]")
    assert q.list_entries() == []
    assert len(q.corrupt_backups()) == 1


def test_indice_mancante_ricreato(tmp_path):
    q = Quarantine(tmp_path / "q")
    q.index_path.unlink()
    assert q.list_entries() == []
    assert q.index_path.exists()


def test_delete_rifiuta_voce_fuori_dalla_quarantena(tmp_path):
    q = Quarantine(tmp_path / "q")
    esterno = tmp_path / "documento-importante"
    esterno.write_text("dati")
    q.index_path.write_text(json.dumps([{
        "original_path": "/a", "quarantined_path": str(esterno),
        "signature": None, "timestamp": 1.0,
    }]))
    with pytest.raises(QuarantineError, match="non è nella directory"):
        q.delete(str(esterno))
    assert esterno.read_text() == "dati"


# --- sostituzione durante la quarantena -----------------------------------

def test_salvataggio_concorrente_non_perde_dati(tmp_path):
    """
    Un editor salva con "temporaneo + rinomina" mentre il file viene
    messo in quarantena: rename() sposta la versione NUOVA. Prima veniva
    cancellata; ora torna al suo posto.
    """
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path, "documento.odt", b"versione infetta")
    rename_reale = os.rename

    def salvataggio_editor(src, dst):
        nuovo = tmp_path / ".documento.odt.tmp"
        nuovo.write_bytes(b"versione appena salvata")
        rename_reale(nuovo, src)
        return rename_reale(src, dst)

    with patch("klamav_py.quarantine.os.rename", side_effect=salvataggio_editor):
        with pytest.raises(QuarantineError, match="rimesso al suo posto"):
            q.quarantine_file(vittima)

    assert vittima.read_bytes() == b"versione appena salvata"
    assert q.list_entries() == [] and q.orphans() == []


def test_sostituzione_con_nome_rioccupato_non_cancella(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    rename_reale = os.rename

    def gara(src, dst):
        nuovo = tmp_path / "nuovo"
        nuovo.write_bytes(b"sostituto")
        rename_reale(nuovo, src)
        esito = rename_reale(src, dst)
        Path(src).write_bytes(b"terzo file")   # il nome torna occupato
        return esito

    with patch("klamav_py.quarantine.os.rename", side_effect=gara):
        with pytest.raises(QuarantineError, match="lasciato in"):
            q.quarantine_file(vittima)

    [orfano] = q.orphans()
    assert orfano.read_bytes() == b"sostituto"
    assert vittima.read_bytes() == b"terzo file"


# --- altri filesystem (EXDEV) ---------------------------------------------

def _rename_con_exdev_verso(qdir: Path):
    rename_reale = os.rename

    def rename(src, dst):
        if Path(dst).parent == qdir:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return rename_reale(src, dst)
    return rename


def test_quarantena_su_altro_filesystem(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path, contenuto=b"X" * 3_000_000)
    os.chmod(vittima, 0o755)

    with patch("klamav_py.quarantine.os.rename", side_effect=_rename_con_exdev_verso(q.dir)):
        entry = q.quarantine_file(vittima, "Eicar-Test-Signature")

    dest = Path(entry.quarantined_path)
    assert dest.read_bytes() == b"X" * 3_000_000
    assert _mode(dest) == 0o400
    assert not vittima.exists()
    assert entry.original_mode == 0o755
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".klamav-quarantena-")]


def test_altro_filesystem_con_sostituzione(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path, contenuto=b"infetto")
    exdev = _rename_con_exdev_verso(q.dir)
    rename_reale = os.rename

    def rename(src, dst):
        if Path(dst).name.startswith(".klamav-quarantena-"):
            nuovo = tmp_path / "nuovo"
            nuovo.write_bytes(b"salvato dall'utente")
            rename_reale(nuovo, src)
        return exdev(src, dst)

    with patch("klamav_py.quarantine.os.rename", side_effect=rename):
        with pytest.raises(QuarantineError, match="rimesso al suo posto"):
            q.quarantine_file(vittima)

    assert vittima.read_bytes() == b"salvato dall'utente"
    assert q.list_entries() == [] and q.orphans() == []


def test_ripristino_su_altro_filesystem(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path, contenuto=b"contenuto")
    os.chmod(vittima, 0o750)
    entry = q.quarantine_file(vittima)
    replace_reale = os.replace

    def replace(src, dst):
        if Path(dst) == vittima:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return replace_reale(src, dst)

    with patch("klamav_py.quarantine.os.replace", side_effect=replace):
        target = q.restore(entry.quarantined_path)

    assert target.read_bytes() == b"contenuto"
    assert _mode(target) == 0o750
    assert not Path(entry.quarantined_path).exists()
    assert q.list_entries() == []


def test_ripristino_normale_ripristina_permessi(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    os.chmod(vittima, 0o755)
    entry = q.quarantine_file(vittima)
    target = q.restore(entry.quarantined_path)
    assert _mode(target) == 0o755 and target.read_bytes() == b"INFETTO"


def test_ripristino_con_file_mancante_non_lascia_segnaposto(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    entry = q.quarantine_file(vittima)
    Path(entry.quarantined_path).unlink()
    with pytest.raises(QuarantineError, match="non disponibile"):
        q.restore(entry.quarantined_path)
    assert not vittima.exists()
