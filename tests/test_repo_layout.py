"""
Nessun riferimento alla vecchia unit di sistema (0.1.16).

La unit di sistema systemd/klamav-scan.service è stata tolta nella 0.1.9,
ma systemd/klamav-scan.timer era rimasto (né il .deb né il pacchetto Arch
lo installavano) e il README descriveva ancora una «unit systemd di
sistema» nel repository. Le unit spedite sono quelle utente in debian/.
Il CHANGELOG e il suo archivio raccontano la storia e restano esclusi.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STALE = "systemd/klamav-scan"
HISTORY = {"CHANGELOG.md", "docs/CHANGELOG-archive.md", "tests/test_repo_layout.py"}
SKIP_DIRS = {".git", "venv", "build", "dist", "__pycache__", "klamav_py.egg-info"}


def _tracked() -> list[str]:
    """File del repository: git ls-files, oppure (sorgente senza .git, per
    esempio nella build di un pacchetto) i file sotto ROOT fuori dalle
    directory generate."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout
        return out.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return [str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


def stale_references(files: dict[str, str]) -> list[str]:
    """Percorsi (fuori dalla storia) che nominano la vecchia unit."""
    return sorted(name for name, text in files.items()
                  if name not in HISTORY and STALE in text)


def _read(name: str) -> str | None:
    try:
        return (ROOT / name).read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return None  # binari, file tolti dal working tree


def test_nessun_riferimento_alla_vecchia_unit_di_sistema():
    files = {n: t for n in _tracked() if (t := _read(n)) is not None}
    assert "README.md" in files, "il controllo non sta guardando il repository"
    assert stale_references(files) == []
    assert not (ROOT / "systemd").exists()


def test_controllo_vede_un_riferimento():
    files = {"README.md": "unit in systemd/klamav-scan.service",
             "debian/rules": "install systemd/klamav-scan.timer",
             "CHANGELOG.md": "tolto systemd/klamav-scan.timer"}
    assert stale_references(files) == ["README.md", "debian/rules"]
