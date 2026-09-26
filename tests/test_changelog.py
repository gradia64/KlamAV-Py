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

Dalla 0.1.10 il PKGBUILD non scarica più l'archivio del tag ma clona il
tag firmato (source git con ?signed, validpgpkeys): la garanzia non è
più un checksum ma la firma, e i test sul PKGBUILD verificano quella.

Il caso reale che motiva questo file: CHANGELOG.md è rimasto fermo a
"0.1.3 (in lavorazione)" per tre rilasci, mentre debian/changelog era
allineato perché dch lo tocca a ogni release.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from klamav_py import __version__

RADICE = Path(__file__).resolve().parent.parent

# Chiave primaria di rilascio del progetto (vedi README, "Verifica dei
# rilasci"). makepkg accetta anche le firme delle sue sottochiavi.
IMPRONTA_RILASCIO = "EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9"


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
    assert sorgente.startswith("klamav-py::git+") and sorgente.endswith(
        f"#tag=v{__version__}?signed"
    ), (
        f"arch/.SRCINFO usa la sorgente {sorgente!r}, che non è il tag firmato "
        f"v{__version__}: rigenerarlo con makepkg --printsrcinfo"
    )
    assert _campo_srcinfo(testo, "validpgpkeys") == IMPRONTA_RILASCIO, (
        "arch/.SRCINFO non dichiara la chiave di rilascio in validpgpkeys: "
        "rigenerarlo con makepkg --printsrcinfo"
    )


def test_srcinfo_e_pkgbuild_hanno_lo_stesso_checksum():
    """
    .SRCINFO è generato dal PKGBUILD, ma a mano: con la 0.1.7 il
    checksum era stato aggiornato solo nel PKGBUILD e .SRCINFO era
    rimasto a SKIP, cioè AUR avrebbe installato il tarball senza
    verificarne l'integrità. Con la sorgente git firmata entrambi sono
    SKIP (vedi test_pkgbuild_sorgente_firmata), ma devono restare uguali.
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


def test_pkgbuild_sorgente_firmata():
    """
    Sostituisce il vecchio "checksum reale quando il tag esiste": con la
    sorgente git la garanzia è la firma del tag, verificata da makepkg
    contro validpgpkeys. Il test impedisce che una modifica futura tolga
    ?signed o validpgpkeys: il pacchetto continuerebbe a costruirsi, ma
    senza verificare nulla, e nessuno se ne accorgerebbe.

    sha256sums deve restare SKIP: un checksum reale dell'albero git
    coprirebbe anche questo PKGBUILD, che sta nel repository, e quello
    scritto prima del tag non potrebbe mai essere giusto.
    """
    pkgbuild = RADICE / "arch" / "PKGBUILD"
    if not pkgbuild.exists():
        pytest.skip("arch/PKGBUILD non presente (sorgente non completo)")
    testo = pkgbuild.read_text(encoding="utf-8")

    sorgente = re.search(r'^source=\(\s*"([^"]+)"', testo, flags=re.MULTILINE)
    assert sorgente, "source non trovata in arch/PKGBUILD"
    assert sorgente.group(1).startswith("$pkgname::git+"), (
        f"arch/PKGBUILD usa {sorgente.group(1)!r}: la sorgente deve essere il "
        "repository git, non l'archivio del tag (che non è firmato)"
    )
    assert sorgente.group(1).endswith("#tag=v$pkgver?signed"), (
        f"arch/PKGBUILD usa {sorgente.group(1)!r}: senza '#tag=v$pkgver?signed' "
        "makepkg non verifica la firma del tag"
    )

    chiavi = re.search(r"^validpgpkeys=\(([^)]*)\)", testo, flags=re.MULTILINE)
    assert chiavi, "validpgpkeys non trovato in arch/PKGBUILD"
    assert re.findall(r"'([0-9A-F]{40})'", chiavi.group(1)) == [IMPRONTA_RILASCIO], (
        "validpgpkeys deve contenere solo l'impronta completa della chiave "
        f"primaria di rilascio {IMPRONTA_RILASCIO}"
    )

    checksum = re.search(r"^sha256sums=\(\s*'([^']+)'", testo, flags=re.MULTILINE)
    assert checksum and checksum.group(1) == "SKIP", (
        "con la sorgente git sha256sums deve essere SKIP: vedi il commento "
        "nel PKGBUILD"
    )


def test_chiave_di_rilascio_nel_repository():
    """
    arch/klamav-py-release-key.asc serve ad arch/test-local.sh --tag per
    verificare la firma nel container senza rete. Deve contenere la chiave
    di validpgpkeys: una chiave sbagliata farebbe fallire la prova solo
    al momento del rilascio.
    """
    chiave = RADICE / "arch" / "klamav-py-release-key.asc"
    if not chiave.exists():
        pytest.skip("arch/klamav-py-release-key.asc non presente")
    if not shutil.which("gpg"):
        pytest.skip("gpg non disponibile")

    # GNUPGHOME temporaneo: --show-keys non importa nulla, ma gpg può
    # comunque creare file nella home; il portachiavi dell'utente non va
    # toccato da un test.
    with tempfile.TemporaryDirectory() as gnupghome:
        res = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--show-keys", str(chiave)],
            capture_output=True, text=True, timeout=30,
            env={**os.environ, "GNUPGHOME": gnupghome},
        )
    assert res.returncode == 0, res.stderr
    impronte = [r.split(":")[9] for r in res.stdout.splitlines() if r.startswith("fpr:")]
    assert impronte and impronte[0] == IMPRONTA_RILASCIO, (
        f"arch/klamav-py-release-key.asc contiene {impronte[:1]}, non la chiave "
        f"di rilascio {IMPRONTA_RILASCIO}"
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
    # sorgenti di `install -Dm<mode> <sorgente> <destinazione>`, anche tra
    # virgolette: prima quelle erano saltate in silenzio, e con loro le man
    # page installate nel ciclo "for page in ...".
    grezze = re.findall(
        r"install\s+-Dm\d+\s+\\?\s*\"?([^\s\\\"']+)\"?", testo
    )
    assert grezze, "nessuna riga 'install -Dm...' trovata in arch/PKGBUILD"

    # Variabili dei cicli "for x in a b c; do": ogni sorgente che le usa
    # viene espansa con tutti i valori.
    cicli = {
        var: valori.split()
        for var, valori in re.findall(r"for\s+(\w+)\s+in\s+([^;\n]+);\s*do", testo)
    }
    sorgenti = []
    for s in grezze:
        usate = [v for v in cicli if f"${v}" in s or f"${{{v}}}" in s]
        if not usate:
            sorgenti.append(s)
            continue
        for valore in cicli[usate[0]]:
            sorgenti.append(s.replace(f"${{{usate[0]}}}", valore).replace(f"${usate[0]}", valore))
    assert not [s for s in sorgenti if "$" in s], (
        f"sorgenti con variabili non espandibili dal test: {sorgenti}"
    )

    mancanti = [s for s in sorgenti if not (RADICE / s).exists()]
    assert not mancanti, (
        "arch/PKGBUILD installa file che non esistono nel repo: "
        f"{', '.join(mancanti)}"
    )
