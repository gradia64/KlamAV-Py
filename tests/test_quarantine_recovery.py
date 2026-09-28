"""
Quarantena interrotta da un crash: l'intento scritto prima di toccare il
file permette a recover_interrupted() (eseguita anche alla creazione di
Quarantine) di completare o annullare l'operazione.

Prima un crash fra lo spostamento e la scrittura dell'indice lasciava il
file in quarantena senza voce (non ripristinabile dalla UI), e con la copia
fra filesystem l'originale infetto poteva restare nella sua directory sotto
un nome nascosto.

Il crash si simula con un'eccezione BaseException che nessun gestore del
modulo intercetta per ripulire, e con _clear_intent ridotto alla chiusura
del descrittore: alla morte del processo il kernel rilascia il lock ma
l'intento resta su disco.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from klamav_py.quarantine import Quarantine


class Crash(BaseException):
    pass


def _infetto(tmp_path: Path, contenuto: bytes = b"X5O!P%@AP") -> Path:
    d = tmp_path / "home"
    d.mkdir(exist_ok=True)
    f = d / "virus.exe"
    f.write_bytes(contenuto)
    os.chmod(f, 0o755)
    return f


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _crash_during(q: Quarantine, path: Path, **patches):
    """quarantine_file interrotta: nessuna pulizia dell'intento."""
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)):
        ctx = [patch(target, **kw) for target, kw in patches.items()]
        for c in ctx:
            c.start()
        try:
            with pytest.raises(Crash):
                q.quarantine_file(path, "Eicar-Test-Signature")
        finally:
            for c in reversed(ctx):
                c.stop()


def _intenti(q: Quarantine):
    return sorted(p.name for p in q.dir.iterdir() if p.name.startswith("."))


def _quarantined_files(q: Quarantine):
    return [p for p in q.dir.iterdir() if p.name[0].isdigit()]


def _exdev_verso(qdir: Path, crash_on=None):
    rename_reale = os.rename

    def rename(src, dst):
        if Path(dst).parent == qdir:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        if crash_on is not None and crash_on(Path(src), Path(dst)):
            raise Crash()
        return rename_reale(src, dst)
    return rename


# -- stesso filesystem ---------------------------------------------------

def test_crash_prima_dell_indice_completato_al_riavvio(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    _crash_during(q, vittima, **{"klamav_py.quarantine.Quarantine._write_index": {"side_effect": Crash()}})

    assert not vittima.exists() and q.list_entries() == []
    (dest,) = _quarantined_files(q)
    assert q.orphans() == [dest]  # lo stato che prima restava per sempre

    q2 = Quarantine(q.dir)
    (entry,) = q2.list_entries()
    assert entry.original_path == str(vittima) and entry.quarantined_path == str(dest)
    assert entry.signature == "Eicar-Test-Signature" and entry.original_mode == 0o755
    assert q2.recovered == [(dest, "completata")]
    assert q2.orphans() == [] and _intenti(q2) == []
    # Ripristinabile come qualunque altra voce.
    assert q2.restore(entry.quarantined_path).read_bytes() == b"X5O!P%@AP"


def test_crash_prima_della_sola_lettura(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    _crash_during(q, vittima, **{"klamav_py.quarantine.os.fchmod": {"side_effect": Crash()}})
    (dest,) = _quarantined_files(q)
    assert _mode(dest) == 0o755

    q2 = Quarantine(q.dir)
    assert _mode(dest) == 0o400 and len(q2.list_entries()) == 1


def test_crash_prima_dello_spostamento_non_lascia_tracce(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    _crash_during(q, vittima, **{"klamav_py.quarantine.os.rename": {"side_effect": Crash()}})
    assert _intenti(q) != []

    q2 = Quarantine(q.dir)
    assert vittima.exists() and q2.list_entries() == [] and q2.recovered == []
    assert _intenti(q2) == [] and _quarantined_files(q2) == []


def test_crash_dopo_l_indice_nessuna_voce_doppia(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)):
        q.quarantine_file(vittima, "sig")
    assert _intenti(q) != []  # crash fra indice e rimozione dell'intento

    q2 = Quarantine(q.dir)
    assert len(q2.list_entries()) == 1 and _intenti(q2) == []


# -- altro filesystem (EXDEV) --------------------------------------------

def test_copia_crash_prima_di_togliere_l_originale_annullata(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    rename = _exdev_verso(q.dir, crash_on=lambda src, dst: src == vittima)
    _crash_during(q, vittima, **{"klamav_py.quarantine.os.rename": {"side_effect": rename}})
    (dest,) = _quarantined_files(q)
    assert _mode(dest) == 0o400 and vittima.exists()

    q2 = Quarantine(q.dir)
    # L'originale resta: la prossima scansione lo rileva di nuovo.
    assert vittima.read_bytes() == b"X5O!P%@AP" and not dest.exists()
    assert q2.list_entries() == [] and q2.recovered[0][1].startswith("annullata")
    assert _intenti(q2) == []


def test_copia_incompleta_annullata(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    rename = _exdev_verso(q.dir, crash_on=lambda src, dst: src == vittima)
    _crash_during(q, vittima, **{"klamav_py.quarantine.os.rename": {"side_effect": rename}})
    (dest,) = _quarantined_files(q)
    os.chmod(dest, 0o600)  # come prima del fsync: copia non completa
    os.remove(vittima)  # anche se l'originale non si trova più

    q2 = Quarantine(q.dir)
    assert not dest.exists() and q2.list_entries() == []


def test_copia_crash_con_originale_nascosto_completata(tmp_path):
    # Lo scenario peggiore di prima: l'originale infetto restava nella sua
    # directory con un nome nascosto (.klamav-quarantena-<uuid>).
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    unlink_reale = os.unlink

    def unlink(p, *a, **k):
        if Path(p).name.startswith(".klamav-quarantena-"):
            raise Crash()
        return unlink_reale(p, *a, **k)

    _crash_during(q, vittima, **{
        "klamav_py.quarantine.os.rename": {"side_effect": _exdev_verso(q.dir)},
        "klamav_py.quarantine.os.unlink": {"side_effect": unlink},
    })
    (nascosto,) = list(vittima.parent.glob(".klamav-quarantena-*"))

    q2 = Quarantine(q.dir)
    assert not nascosto.exists() and not vittima.exists()
    (entry,) = q2.list_entries()
    assert Path(entry.quarantined_path).read_bytes() == b"X5O!P%@AP"
    assert q2.orphans() == [] and _intenti(q2) == []


# -- concorrenza e diagnostica -------------------------------------------

def test_operazione_in_corso_in_un_altro_processo_non_toccata(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    _crash_during(q, vittima, **{"klamav_py.quarantine.Quarantine._write_index": {"side_effect": Crash()}})
    (intent,) = [p for p in q.dir.iterdir() if p.name.endswith(".intent")]

    # Il lock tenuto da un'altra descrizione di file equivale a un altro
    # processo che sta ancora lavorando.
    fd = os.open(intent, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        q2 = Quarantine(q.dir)
        assert q2.recovered == [] and q2.list_entries() == [] and intent.exists()
    finally:
        os.close(fd)
    assert q2.recover_interrupted() == [(Path(_quarantined_files(q2)[0]), "completata")]


def test_intento_scritto_a_meta_ignorato(tmp_path):
    q = Quarantine(tmp_path / "q")
    (q.dir / ".123_abc.intent").write_text('{"original_pa')
    q2 = Quarantine(q.dir)
    assert q2.recovered == [] and _intenti(q2) == []


def test_file_di_servizio_non_sono_orfani(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    fd = os.open(vittima, os.O_RDONLY)
    st = os.fstat(fd)
    os.close(fd)
    dest = q.dir / "123_abc"
    intent_fd = q._write_intent(dest, {"x": 1, "dev": st.st_dev, "ino": st.st_ino})
    try:
        (q.dir / ".123_abc.copy").touch()
        assert q.orphans() == []
    finally:
        os.close(intent_fd)
