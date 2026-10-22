"""
Intento della quarantena pubblicato solo quando è completo e bloccato.

Prima _write_intent creava `.<nome>.intent` con O_EXCL e solo dopo prendeva
il flock: un recupero concorrente (un'altra istanza di Quarantine: GUI, CLI,
timer, Real-Time) poteva aprirlo in quella finestra, prendere il lock,
leggere un file vuoto, trattarlo come un intento scritto a metà e
cancellarlo. L'operazione continuava senza intento, e un'interruzione fra
spostamento e indice lasciava un orfano.

La gara si riproduce in modo deterministico: open_private_fd del modulo è
avvolta in modo che, subito dopo la creazione dell'intento e prima del
flock, giri il recupero di un'altra istanza. Due descrittori aperti con
open() separati sono due descrizioni di file: il flock fra loro si
comporta come fra due processi.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import klamav_py.quarantine as qmod
from klamav_py.quarantine import INTENT_SUFFIX, Quarantine, QuarantineError


def _infetto(tmp_path: Path) -> Path:
    d = tmp_path / "home"
    d.mkdir(exist_ok=True)
    f = d / "virus.exe"
    f.write_bytes(b"X5O!P%@AP")
    return f


def _pending_suffix() -> str:
    suffix = getattr(qmod, "INTENT_PENDING_SUFFIX", None)
    assert suffix is not None, "manca il nome temporaneo dell'intento"
    assert not suffix.endswith(INTENT_SUFFIX)
    return suffix


def _recupero_dopo_la_creazione(altro: Quarantine, volte: int):
    """open_private_fd che, per le prime `volte` creazioni di un intento,
    esegue il recupero di `altro` fra la creazione e il flock."""
    reale = qmod.open_private_fd
    chiamate = []

    def open_private_fd(path, flags, *args, **kwargs):
        fd = reale(path, flags, *args, **kwargs)
        name = Path(path).name
        if flags & os.O_EXCL and name.startswith(".") and INTENT_SUFFIX in name:
            chiamate.append(name)
            if len(chiamate) <= volte:
                altro.recover_interrupted()
        return fd
    return open_private_fd, chiamate


def test_recupero_concorrente_non_toglie_l_intento_di_un_operazione_viva(tmp_path):
    q = Quarantine(tmp_path / "q")
    altro = Quarantine(q.dir)
    vittima = _infetto(tmp_path)
    hook, chiamate = _recupero_dopo_la_creazione(altro, volte=1)

    visto = []
    sposta_reale = Quarantine._move_into_quarantine

    def sposta(self, fd, st, path, dest, staging):
        visto.append(self._intent_path(dest).exists())
        return sposta_reale(self, fd, st, path, dest, staging)

    with patch.object(qmod, "open_private_fd", hook), \
            patch.object(Quarantine, "_move_into_quarantine", sposta):
        entry = q.quarantine_file(vittima, "Eicar-Test-Signature")

    assert chiamate, "il punto di aggancio non è stato raggiunto"
    assert visto == [True], "intento sparito prima dello spostamento"
    assert [e.quarantined_path for e in q.list_entries()] == [entry.quarantined_path]
    assert altro.recovered == []
    assert sorted(p.name for p in q.dir.iterdir() if p.name.startswith(".")) == []


def test_temporaneo_tolto_a_ogni_tentativo_file_non_toccato(tmp_path):
    _pending_suffix()
    q = Quarantine(tmp_path / "q")
    altro = Quarantine(q.dir)
    vittima = _infetto(tmp_path)
    hook, chiamate = _recupero_dopo_la_creazione(altro, volte=1000)

    with patch.object(qmod, "open_private_fd", hook):
        with pytest.raises(QuarantineError, match="intento"):
            q.quarantine_file(vittima)

    assert len(chiamate) > 1
    assert vittima.read_bytes() == b"X5O!P%@AP"
    assert q.list_entries() == []
    assert sorted(p.name for p in q.dir.iterdir() if p.name.startswith(".")) == []


def test_temporaneo_senza_lock_tolto_dal_recupero(tmp_path):
    q = Quarantine(tmp_path / "q")
    pending = q.dir / f".123_abc{_pending_suffix()}"
    pending.write_text('{"original_path": "/x"')

    q2 = Quarantine(q.dir)
    assert not pending.exists()
    assert q2.recovered == [] and q2.orphans() == []


def test_temporaneo_con_lock_tenuto_non_toccato(tmp_path):
    q = Quarantine(tmp_path / "q")
    pending = q.dir / f".123_abc{_pending_suffix()}"
    pending.write_text("")
    fd = os.open(pending, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        q2 = Quarantine(q.dir)
        assert pending.exists()
        assert q2.recovered == [] and q2.orphans() == []
    finally:
        os.close(fd)
    q2.recover_interrupted()
    assert not pending.exists()


def test_pubblicazione_non_sovrascrive_un_intento_esistente(tmp_path):
    _pending_suffix()
    q = Quarantine(tmp_path / "q")
    dest = q.dir / "123_abc"
    esistente = q._intent_path(dest)
    esistente.write_text("altro intento")

    with pytest.raises(FileExistsError):
        q._write_intent(dest, {"x": 1})

    assert esistente.read_text() == "altro intento"
    assert sorted(p.name for p in q.dir.iterdir() if p.name.startswith(".")) == [esistente.name]
