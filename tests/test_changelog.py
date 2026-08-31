"""
Coerenza fra le fonti che dichiarano la versione.

klamav_py.__version__ è la fonte di verità; tests/test_cli.py verifica
già l'allineamento con debian/changelog e che pyproject.toml non
ridichiari la versione. Qui si coprono le due fonti rimaste, entrambe
lette da esseri umani e da nessun processo di build — cioè quelle che
invecchiano in silenzio:

  - CHANGELOG.md, che è il changelog per gli utenti di qualunque
    distribuzione (debian/changelog non lo legge chi installa da AUR);
  - arch/PKGBUILD, la cui pkgver deve seguire ogni rilascio.

Il caso reale che motiva questo file: CHANGELOG.md è rimasto fermo a
"0.1.3 (in lavorazione)" per tre rilasci, mentre debian/changelog era
allineato perché dch lo tocca a ogni release.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from klamav_py import __version__

RADICE = Path(__file__).resolve().parent.parent


def _versione_in_cima(testo: str) -> str | None:
    """Prima intestazione '## <versione>' incontrata, senza revisione Debian."""
    for riga in testo.splitlines():
        match = re.match(r"^##\s+(\d+\.\d+\.\d+)(?:-\d+)?\b", riga)
        if match:
            return match.group(1)
    return None


def test_changelog_md_allineato():
    changelog = RADICE / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md non presente (sorgente non completo)")

    testo = changelog.read_text(encoding="utf-8")
    versione = _versione_in_cima(testo)

    assert versione is not None, (
        "nessuna intestazione '## <versione>' trovata in CHANGELOG.md: "
        "il formato atteso è '## 0.1.5 — 2026-08-30'"
    )
    assert versione == __version__, (
        f"CHANGELOG.md documenta la {versione} ma __version__ è "
        f"{__version__}: aggiungere la voce mancante prima del rilascio "
        "(tools/changelog-stub.sh genera la bozza da debian/changelog)"
    )


LARGHEZZA_MASSIMA = 100


def test_changelog_md_righe_non_riflusse():
    """
    Il file ha già subito un incollaggio da testo impaginato che ha perso
    gli spazi di fine riga: "sono inordine anti-cronologico", "conservate
    infondo", "ricreata proattivamentedopo". Erano decine di occorrenze.

    Le parole fuse in sé non sono rilevabili senza un dizionario — sono
    minuscola-minuscola, indistinguibili da una parola vera. La CAUSA
    invece si vede benissimo: quel testo era su righe da 206 e fino a 910
    caratteri, perché l'incollaggio aveva riflusso i paragrafi su una
    riga sola. Un changelog scritto a mano e a capo stretto non produce
    righe simili.

    Il limite è volutamente largo (100): non è uno stile da imporre, è
    una rete per un solo tipo di incidente.
    """
    changelog = RADICE / "CHANGELOG.md"
    if not changelog.exists():
        pytest.skip("CHANGELOG.md non presente (sorgente non completo)")

    lunghe = [
        (n, len(riga))
        for n, riga in enumerate(changelog.read_text(encoding="utf-8").splitlines(), 1)
        if len(riga) > LARGHEZZA_MASSIMA and not riga.lstrip().startswith("http")
    ]
    assert not lunghe, (
        "righe molto lunghe in CHANGELOG.md, sintomo di testo incollato e "
        "riflusso (che perde gli spazi di fine riga): "
        + ", ".join(f"riga {n} ({l} caratteri)" for n, l in lunghe)
    )


def test_pkgbuild_arch_allineato():
    pkgbuild = RADICE / "arch" / "PKGBUILD"
    if not pkgbuild.exists():
        pytest.skip("arch/PKGBUILD non presente (sorgente non completo)")

    testo = pkgbuild.read_text(encoding="utf-8")
    match = re.search(r"^pkgver=(\S+)$", testo, flags=re.MULTILINE)

    assert match, "pkgver non trovata in arch/PKGBUILD"
    assert match.group(1) == __version__, (
        f"arch/PKGBUILD dichiara pkgver={match.group(1)} ma __version__ è "
        f"{__version__}: il tarball scaricato da AUR non corrisponderebbe "
        "alla versione rilasciata"
    )


def test_pkgbuild_installa_file_esistenti():
    """
    package() installa file per percorso letterale. Se un file viene
    rinominato nel repo e il PKGBUILD non segue, makepkg fallisce solo
    sulla macchina dell'utente, non qui.

    È già successo: il rename di klamav-icon.svg in klamav-py.svg ha
    lasciato il PKGBUILD a puntare a un percorso assente dal tag.
    """
    pkgbuild = RADICE / "arch" / "PKGBUILD"
    if not pkgbuild.exists():
        pytest.skip("arch/PKGBUILD non presente (sorgente non completo)")

    testo = pkgbuild.read_text(encoding="utf-8")
    # sorgenti di `install -Dm<mode> <sorgente> <destinazione>`
    sorgenti = re.findall(
        r"install\s+-Dm\d+\s+\\?\s*([^\s\\\"']+)", testo
    )
    assert sorgenti, "nessuna riga 'install -Dm...' trovata in arch/PKGBUILD"

    mancanti = [s for s in sorgenti if not (RADICE / s).exists()]
    assert not mancanti, (
        "arch/PKGBUILD installa file che non esistono nel repo: "
        f"{', '.join(mancanti)}"
    )
