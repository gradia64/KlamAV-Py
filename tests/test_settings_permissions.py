"""
Permessi del file di configurazione QSettings (audit 0.1.8).

QSettings lo crea con 0666 & ~umask (0644 con umask 022) e contiene le
cartelle monitorate dal Real-Time, il target delle scansioni pianificate
e il percorso di quarantena: gli stessi dati che il resto
dell'applicazione tiene a 0600 (vedi private_files.py).

I test girano in un sottoprocesso con HOME dedicata perché QSettings
risolve il percorso del file all'avvio del processo.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

RADICE = Path(__file__).resolve().parent.parent

SCRIPT = textwrap.dedent(
    """
    import os, sys
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.umask(0o022)
    from PySide6.QtCore import QCoreApplication, QSettings
    app = QCoreApplication([])
    from klamav_py.gui.main_window import _migrate_legacy_settings, APP_NAME
    _migrate_legacy_settings()
    s = QSettings(APP_NAME, APP_NAME)
    # Una scrittura vera dopo la migrazione: QSaveFile non deve perdere i permessi.
    s.setValue("schedule_target", "/home/utente/Privato")
    s.sync()
    print(s.fileName())
    print(",".join(sorted(s.allKeys())))
    """
)


def _esegui(home: Path) -> tuple[str, list[str]]:
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(RADICE))
    env.pop("XDG_CONFIG_HOME", None)
    res = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        capture_output=True, text=True, timeout=120, env=env,
    )
    assert res.returncode == 0, res.stderr
    righe = res.stdout.strip().splitlines()
    return righe[0], (righe[1].split(",") if len(righe) > 1 and righe[1] else [])


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.mark.timeout(180)
def test_installazione_nuova_conf_0600(tmp_path):
    conf, _ = _esegui(tmp_path)
    assert _mode(Path(conf)) == 0o600


@pytest.mark.timeout(180)
def test_conf_preesistente_0644_viene_ristretto(tmp_path):
    d = tmp_path / ".config" / "KlamAV-Py"
    d.mkdir(parents=True)
    conf = d / "KlamAV-Py.conf"
    conf.write_text("[General]\nsocket=/run/clamav/clamd.ctl\n")
    os.chmod(conf, 0o644)
    _, chiavi = _esegui(tmp_path)
    assert _mode(conf) == 0o600
    assert "socket" in chiavi, "le impostazioni esistenti non devono andare perse"


@pytest.mark.timeout(180)
def test_conf_legacy_ristretto_e_migrato(tmp_path):
    d = tmp_path / ".config" / "KlamAV"
    d.mkdir(parents=True)
    legacy = d / "KlamAV.conf"
    legacy.write_text("[General]\nrealtime_paths=/home/utente/Privato\n")
    os.chmod(legacy, 0o644)
    conf, chiavi = _esegui(tmp_path)
    assert _mode(legacy) == 0o600
    assert _mode(Path(conf)) == 0o600
    assert "realtime_paths" in chiavi
    assert "Privato" in legacy.read_text(), "il file legacy non va modificato nel contenuto"


@pytest.mark.timeout(180)
def test_legacy_assente_non_viene_creato(tmp_path):
    _esegui(tmp_path)
    assert not (tmp_path / ".config" / "KlamAV").exists()


@pytest.mark.timeout(180)
def test_legacy_ristretto_anche_con_migrazione_gia_fatta(tmp_path):
    """
    Il conf nuovo già popolato (installazione che ha già girato una
    versione >= 0.1.4) non deve saltare la restrizione del legacy:
    prima della correzione l'hardening del legacy stava DOPO l'early
    return della migrazione e il file — ancora 0644 — non veniva mai
    toccato.
    """
    nuovo = tmp_path / ".config" / "KlamAV-Py"
    nuovo.mkdir(parents=True)
    conf = nuovo / "KlamAV-Py.conf"
    conf.write_text("[General]\nsocket=/run/clamav/clamd.ctl\n")
    os.chmod(conf, 0o644)

    legacy_dir = tmp_path / ".config" / "KlamAV"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / "KlamAV.conf"
    legacy.write_text("[General]\nrealtime_paths=/home/utente/Privato\n")
    os.chmod(legacy, 0o644)

    _, chiavi = _esegui(tmp_path)

    assert _mode(legacy) == 0o600
    assert _mode(conf) == 0o600
    assert "socket" in chiavi
    # Conf nuovo già configurato: la migrazione non riparte, le chiavi
    # del legacy non devono finire nel conf nuovo...
    assert "realtime_paths" not in chiavi
    # ...e il legacy resta intatto nel contenuto.
    assert "Privato" in legacy.read_text()
