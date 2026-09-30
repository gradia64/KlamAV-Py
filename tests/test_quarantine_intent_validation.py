"""
Intento di recupero non valido: .<nome>.intent ha la stessa fiducia di
index.json e si valida con le stesse regole prima di usarne un campo.

Prima il recupero usava i campi dell'intento così com'erano:
- staging puntato a un file dell'utente con dev/ino corrispondenti: il file
  veniva cancellato;
- "dev": Infinity: OverflowError non catturato dal costruttore, la GUI non
  si avviava più e la CLI usciva con 1 («infezioni trovate»);
- original_mode stringa: voce scritta nell'indice, che alla lettura dopo
  veniva messo da parte come corrotto, e le voci legittime diventavano
  orfane.

Un intento invalido si mette da parte come .<nome>.intent.corrupt-<...>,
mai cancellato, e dest resta dov'è (visibile in orphans()).

Gravità: robustezza, non sicurezza. Scrivere l'intento richiede già i
privilegi dell'utente sulla quarantena 0700.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from klamav_py.quarantine import Quarantine


class Crash(BaseException):
    pass


DEST = "1700000000_0123456789abcdef"


@pytest.fixture
def env(tmp_path):
    """Quarantena con una voce legittima già in indice e un file
    dell'utente fuori dalla quarantena."""
    q = Quarantine(tmp_path / "q")
    home = tmp_path / "home"
    home.mkdir()
    legit = home / "legit.exe"
    legit.write_bytes(b"infetto")
    q.quarantine_file(legit, "Legit-Sig")
    user_file = home / "tesi.odt"
    user_file.write_bytes(b"lavoro di mesi")
    return q, home, user_file


def _plant(q: Quarantine, record, copying: bool = True) -> Path:
    """Stato di una quarantena per copia interrotta: copia completa
    (0400) in quarantena, segno di copia e intento con `record`."""
    dest = q.dir / DEST
    dest.write_bytes(b"copia infetta")
    os.chmod(dest, 0o400)
    if copying:
        (q.dir / f".{DEST}.copy").touch()
    raw = record if isinstance(record, str) else json.dumps(record)
    (q.dir / f".{DEST}.intent").write_text(raw)
    return dest


def _record(home: Path, user_file: Path, **over) -> dict:
    st = os.lstat(user_file)
    rec = {
        "original_path": str(home / "virus.exe"),
        "quarantined_path": f"/altrove/{DEST}",
        "signature": "Eicar",
        "timestamp": 1700000000.0,
        "original_mode": 0o644,
        "staging": str(home / f".klamav-quarantena-{'a' * 32}"),
        "dev": st.st_dev,
        "ino": st.st_ino,
    }
    rec.update(over)
    return rec


def _assert_set_aside(q: Quarantine, dest: Path, user_file: Path):
    q2 = Quarantine(q.dir)  # non deve sollevare
    # Nessun file fuori dalla quarantena toccato.
    assert user_file.read_bytes() == b"lavoro di mesi"
    # Intento messo da parte, non cancellato e non più ripreso.
    assert not (q.dir / f".{DEST}.intent").exists()
    (aside,) = list(q.dir.glob(f".{DEST}.intent.corrupt-*"))
    assert aside.is_file()
    # Indice intatto: la voce legittima c'è, nessuna voce per l'intento,
    # nessun indice messo da parte.
    (entry,) = q2.list_entries()
    assert entry.signature == "Legit-Sig"
    assert q2.corrupt_backups() == []
    # dest resta dov'è, visibile come orfano; l'intento messo da parte no.
    assert dest.exists() and q2.orphans() == [dest]
    ((where, outcome),) = q2.recovered
    assert where == dest and outcome.startswith("non recuperata")
    # Una seconda istanza non ci riprova.
    assert Quarantine(q.dir).recovered == []
    return q2


def test_staging_fuori_posto_non_cancella_file_dell_utente(env):
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file, staging=str(user_file)))
    _assert_set_aside(q, dest, user_file)


def test_staging_in_altra_directory_con_nome_valido(env, tmp_path):
    q, home, user_file = env
    altrove = tmp_path / "altrove"
    altrove.mkdir()
    bersaglio = altrove / f".klamav-quarantena-{'b' * 32}"
    os.link(user_file, bersaglio)  # stesso inode del file dell'utente
    dest = _plant(q, _record(home, user_file, staging=str(bersaglio)))
    _assert_set_aside(q, dest, user_file)
    assert bersaglio.exists()


def test_dev_infinity(env):
    q, home, user_file = env
    rec = json.dumps(_record(home, user_file)).replace(
        f'"dev": {os.lstat(user_file).st_dev}', '"dev": Infinity')
    assert "Infinity" in rec
    dest = _plant(q, rec)
    _assert_set_aside(q, dest, user_file)


def test_nul_in_original_path(env):
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file, original_path=str(home) + "/vi\0rus"))
    _assert_set_aside(q, dest, user_file)


def test_original_mode_stringa_non_corrompe_l_indice(env):
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file, original_mode="0755"))
    _assert_set_aside(q, dest, user_file)


@pytest.mark.parametrize("over", [
    {"original_path": "relativo/virus.exe"},
    {"timestamp": True},
    {"original_mode": 0o10000},
    {"original_mode": True},
    {"ino": -1},
    {"dev": 1.0},
    {"signature": 12},
])
def test_altri_campi_non_validi(env, over):
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file, **over))
    _assert_set_aside(q, dest, user_file)


@pytest.mark.parametrize("raw", ["[1, 2]", '"stringa"', "null", "42"])
def test_json_non_oggetto(env, raw):
    q, home, user_file = env
    dest = _plant(q, raw)
    _assert_set_aside(q, dest, user_file)


def test_errore_imprevisto_nel_recupero(env):
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file))
    with patch.object(Quarantine, "_load_index", side_effect=RuntimeError("imprevisto")):
        q2 = Quarantine(q.dir)  # non deve sollevare
    ((where, outcome),) = q2.recovered
    assert where == dest and "imprevisto" in outcome
    assert not (q.dir / f".{DEST}.intent").exists()
    assert list(q.dir.glob(f".{DEST}.intent.corrupt-*"))
    assert dest.exists() and user_file.read_bytes() == b"lavoro di mesi"


def test_intento_valido_ancora_recuperato(env):
    # Controprova: la validazione non rifiuta l'intento che quarantine_file
    # scrive davvero (staging nella stessa directory, nome .klamav-quarantena-).
    q, home, user_file = env
    dest = _plant(q, _record(home, user_file))
    q2 = Quarantine(q.dir)
    assert q2.recovered == [(dest, "completata")]
    assert len(q2.list_entries()) == 2 and user_file.exists()


# -- staging sostituito prima della verifica dell'inode -------------------

def test_staging_sostituito_lasciato_per_il_recupero_manuale(tmp_path):
    q = Quarantine(tmp_path / "q")
    home = tmp_path / "home"
    home.mkdir()
    vittima = home / "virus.exe"
    vittima.write_bytes(b"X5O!P%@AP")

    rename_reale, unlink_reale = os.rename, os.unlink

    def rename(src, dst):
        if Path(dst).parent == q.dir:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
        return rename_reale(src, dst)

    def unlink(p, *a, **k):
        if Path(p).name.startswith(".klamav-quarantena-"):
            raise Crash()
        return unlink_reale(p, *a, **k)

    with patch.object(Quarantine, "_clear_intent", lambda self, dest, fd: os.close(fd)), \
            patch("klamav_py.quarantine.os.rename", side_effect=rename), \
            patch("klamav_py.quarantine.os.unlink", side_effect=unlink):
        with pytest.raises(Crash):
            q.quarantine_file(vittima, "Eicar")
    (staging,) = list(home.glob(".klamav-quarantena-*"))

    # Il salvataggio dell'applicazione: il file nuovo nasce PRIMA che il
    # vecchio sparisca, così non può riusarne l'inode.
    nuovo = home / ".virus.exe.tmp"
    nuovo.write_bytes(b"versione appena salvata")
    os.replace(nuovo, staging)

    q2 = Quarantine(q.dir)
    assert staging.read_bytes() == b"versione appena salvata"
    (entry,) = q2.list_entries()
    assert Path(entry.quarantined_path).read_bytes() == b"X5O!P%@AP"
    ((_, outcome),) = q2.recovered
    assert outcome.startswith("completata;") and str(staging) in outcome
    assert "recupero manuale" in outcome


# -- regole condivise con l'indice ---------------------------------------

@pytest.mark.parametrize("over", [
    {"original_mode": "0755"},
    {"original_mode": True},
    {"timestamp": float("nan")},
    {"original_path": "/a\0b"},
])
def test_indice_con_le_stesse_regole(tmp_path, over):
    q = Quarantine(tmp_path / "q")
    voce = {"original_path": "/home/u/a", "quarantined_path": str(q.dir / "x"),
            "timestamp": 1.0, "signature": None, "original_mode": 0o644}
    voce.update(over)
    q.index_path.write_text(json.dumps([voce]))
    assert q.list_entries() == [] and len(q.corrupt_backups()) == 1
