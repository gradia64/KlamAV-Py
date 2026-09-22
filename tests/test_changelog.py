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
import subprocess
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


def _campo_srcinfo(testo: str, chiave: str) -> str | None:
    """Valore di 'chiave = valore' in .SRCINFO (righe rientrate)."""
    for riga in testo.splitlines():
        nome, sep, valore = riga.partition("=")
        if sep and nome.strip() == chiave:
            return valore.strip()
    return None


def _tag_esiste(versione: str) -> bool:
    """True se il tag v<versione> esiste in questo clone."""
    try:
        res = subprocess.run(
            ["git", "-C", str(RADICE), "tag", "--list", f"v{versione}"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0 and res.stdout.strip() != ""


def test_srcinfo_allineato():
    """
    .SRCINFO è il file da cui AUR e gli helper (yay, paru) leggono la
    versione: se resta indietro, gli utenti Arch non vedono
    l'aggiornamento anche se il PKGBUILD è corretto. È successo con la
    0.1.7, dove era rimasto alla 0.1.6 fino a poco prima della
    pubblicazione.
    """
    srcinfo = RADICE / "arch" / ".SRCINFO"
    if not srcinfo.exists():
        pytest.skip("arch/.SRCINFO non presente (sorgente non completo)")

    testo = srcinfo.read_text(encoding="utf-8")
    pkgver = _campo_srcinfo(testo, "pkgver")
    assert pkgver == __version__, (
        f"arch/.SRCINFO dichiara pkgver={pkgver} ma __version__ è "
        f"{__version__}: rigenerarlo con makepkg --printsrcinfo"
    )

    sorgente = _campo_srcinfo(testo, "source") or ""
    assert f"v{__version__}.tar.gz" in sorgente, (
        f"arch/.SRCINFO scarica {sorgente!r}, che non è il tag "
        f"v{__version__}: rigenerarlo con makepkg --printsrcinfo"
    )


def test_srcinfo_e_pkgbuild_hanno_lo_stesso_checksum():
    """
    .SRCINFO è generato dal PKGBUILD, ma a mano: con la 0.1.7 il
    checksum era stato aggiornato solo nel PKGBUILD e .SRCINFO era
    rimasto a SKIP, cioè AUR avrebbe installato il tarball senza
    verificarne l'integrità.
    """
    srcinfo = RADICE / "arch" / ".SRCINFO"
    pkgbuild = RADICE / "arch" / "PKGBUILD"
    if not (srcinfo.exists() and pkgbuild.exists()):
        pytest.skip("arch/.SRCINFO o arch/PKGBUILD non presenti")

    da_srcinfo = _campo_srcinfo(srcinfo.read_text(encoding="utf-8"), "sha256sums")
    match = re.search(
        r"^sha256sums=\(\s*'([^']+)'", pkgbuild.read_text(encoding="utf-8"), flags=re.MULTILINE
    )
    assert match, "sha256sums non trovato in arch/PKGBUILD"
    assert da_srcinfo == match.group(1), (
        f"arch/.SRCINFO ha sha256sums={da_srcinfo} ma il PKGBUILD ha "
        f"{match.group(1)}: rigenerare .SRCINFO con makepkg --printsrcinfo"
    )


def test_checksum_reale_quando_il_tag_esiste():
    """
    Il checksum del tarball esiste solo DOPO il tag, quindi fra il commit
    di rilascio e il tag è legittimo che sia SKIP. Da quando il tag
    esiste non lo è più: pubblicare su AUR con SKIP significa installare
    senza verifica di integrità. Il test si attiva da solo al momento
    giusto, senza marker da ricordare.
    """
    pkgbuild = RADICE / "arch" / "PKGBUILD"
    if not pkgbuild.exists():
        pytest.skip("arch/PKGBUILD non presente (sorgente non completo)")
    if not _tag_esiste(__version__):
        pytest.skip(f"il tag v{__version__} non esiste ancora: SKIP è legittimo")

    testo = pkgbuild.read_text(encoding="utf-8")
    match = re.search(r"^sha256sums=\(\s*'([^']+)'", testo, flags=re.MULTILINE)
    assert match, "sha256sums non trovato in arch/PKGBUILD"
    assert match.group(1) != "SKIP", (
        f"il tag v{__version__} esiste ma arch/PKGBUILD ha ancora "
        "sha256sums=('SKIP'): calcolarlo dal tarball del tag "
        "(updpkgsums, oppure curl | sha256sum) prima di pubblicare su AUR"
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
