"""
Intento conservato quando l'operazione non si conclude dopo lo spostamento.

Prima _clear_intent era nel finally di quarantine_file: se lo spostamento
riusciva e poi la scrittura dell'indice falliva (disco pieno, EIO),
l'intento veniva cancellato e il file restava in quarantena senza voce,
non ripristinabile dalla GUI. Ora l'intento si toglie solo con la voce
nell'indice, o quando il file non è stato toccato; il prossimo recupero
completa la voce. La GUI ripete il recupero a ogni «Aggiorna» (e al
caricamento dopo un'operazione della pagina), senza riavvio.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from klamav_py.quarantine import Quarantine, QuarantineError

ENOSPC = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


class Crash(BaseException):
    pass


def _infetto(tmp_path: Path) -> Path:
    d = tmp_path / "home"
    d.mkdir(exist_ok=True)
    f = d / "virus.exe"
    f.write_bytes(b"X5O!P%@AP")
    os.chmod(f, 0o644)
    return f


def _servizio(q: Quarantine) -> list[str]:
    return sorted(p.name for p in q.dir.iterdir() if p.name.startswith("."))


def _in_quarantena(q: Quarantine) -> list[Path]:
    return [p for p in q.dir.iterdir() if p.name[0].isdigit()]


def _fallisce(q: Quarantine, vittima: Path, target: str, **kw) -> Exception:
    with patch(target, **kw):
        with pytest.raises(Exception) as info:
            q.quarantine_file(vittima, "Eicar-Test-Signature")
    return info.value


def _exdev_verso(qdir: Path):
    rename_reale = os.rename

    def rename(src, dst):
        if Path(dst).parent == qdir:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return rename_reale(src, dst)
    return rename


def _assert_messaggio(exc: Exception, vittima: Path) -> None:
    assert isinstance(exc, QuarantineError), f"{type(exc).__name__}: {exc}"
    testo = str(exc)
    assert str(vittima) in testo and "in quarantena" in testo
    assert "prossimo recupero" in testo


def _assert_completata_dal_recupero(qdir: Path, vittima: Path) -> None:
    q2 = Quarantine(qdir)
    (dest,) = _in_quarantena(q2)
    assert q2.recovered == [(dest, "completata", str(vittima))]
    assert [e.original_path for e in q2.list_entries()] == [str(vittima)]
    assert _servizio(q2) == [] and q2.orphans() == []
    assert stat.S_IMODE(os.lstat(dest).st_mode) == 0o400
    assert q2.restore(str(dest)) == vittima
    assert vittima.read_bytes() == b"X5O!P%@AP"


def test_indice_non_scritto_dopo_lo_spostamento(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    exc = _fallisce(q, vittima, "klamav_py.quarantine.Quarantine._write_index", side_effect=ENOSPC)

    _assert_messaggio(exc, vittima)
    assert not vittima.exists() and len(_in_quarantena(q)) == 1
    assert any(n.endswith(".intent") for n in _servizio(q)), "intento cancellato"
    assert q.list_entries() == []
    _assert_completata_dal_recupero(q.dir, vittima)


def test_indice_non_scritto_dopo_la_copia_fra_filesystem(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    with patch("klamav_py.quarantine.os.rename", _exdev_verso(q.dir)):
        exc = _fallisce(q, vittima, "klamav_py.quarantine.Quarantine._write_index",
                        side_effect=ENOSPC)

    _assert_messaggio(exc, vittima)
    assert not vittima.exists()
    assert [n for n in os.listdir(vittima.parent)] == []
    nomi = _servizio(q)
    assert any(n.endswith(".intent") for n in nomi) and any(n.endswith(".copy") for n in nomi)
    _assert_completata_dal_recupero(q.dir, vittima)


def test_sola_lettura_non_applicata_dopo_lo_spostamento(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    exc = _fallisce(q, vittima, "klamav_py.quarantine.os.fchmod", side_effect=ENOSPC)

    _assert_messaggio(exc, vittima)
    assert any(n.endswith(".intent") for n in _servizio(q))
    _assert_completata_dal_recupero(q.dir, vittima)


def test_originale_rinominato_non_rimosso_dopo_la_copia(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    unlink_reale = os.unlink

    def unlink(p, *a, **k):
        if Path(p).name.startswith(".klamav-quarantena-"):
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return unlink_reale(p, *a, **k)

    with patch("klamav_py.quarantine.os.rename", _exdev_verso(q.dir)):
        exc = _fallisce(q, vittima, "klamav_py.quarantine.os.unlink", new=unlink)

    _assert_messaggio(exc, vittima)
    assert len(os.listdir(vittima.parent)) == 1  # l'originale col nome temporaneo
    _assert_completata_dal_recupero(q.dir, vittima)
    assert os.listdir(vittima.parent) == [vittima.name]


def test_spostamento_fallito_nessun_intento(tmp_path):
    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    exc = _fallisce(q, vittima, "klamav_py.quarantine.os.rename",
                    side_effect=OSError(errno.EACCES, os.strerror(errno.EACCES)))

    assert isinstance(exc, QuarantineError) and "impossibile spostare" in str(exc)
    assert vittima.read_bytes() == b"X5O!P%@AP"
    assert _servizio(q) == [] and _in_quarantena(q) == [] and q.list_entries() == []


def test_aggiorna_della_gui_completa_la_voce(tmp_path):
    pytest.importorskip("PySide6")
    from klamav_py.gui.quarantine_loader import load_quarantine

    q = Quarantine(tmp_path / "q")
    vittima = _infetto(tmp_path)
    # Interruzione dopo lo spostamento, nello stesso processo della GUI:
    # l'intento resta, l'oggetto Quarantine della pagina è lo stesso.
    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)), \
            patch.object(Quarantine, "_write_index", side_effect=Crash):
        with pytest.raises(Crash):
            q.quarantine_file(vittima, "Eicar-Test-Signature")
    assert q.list_entries() == []

    snapshot = load_quarantine(q.dir, q)
    assert snapshot.quarantine is q
    assert [e.original_path for e in snapshot.entries] == [str(vittima)]
    assert snapshot.orphans == [] and _servizio(q) == []
