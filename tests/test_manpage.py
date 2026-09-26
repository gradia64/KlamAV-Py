"""
Coerenza delle man page in docs/man/ con il codice e fra le traduzioni.

Le man page elencano opzioni e comandi a mano: senza questo test una
flag rinominata o aggiunta nel parser, o aggiornata in una sola lingua,
diverge in silenzio. Il confronto è in entrambe le direzioni.

Per klamav-py la verità è build_parser() (nessuna dipendenza da Qt).
Per klamav-py-gui il parser è costruito dentro main() e il modulo
importa PySide6: le opzioni si leggono dall'AST, come negli altri test
statici del progetto, così il test gira anche senza Qt.

Le righe ".\\" [TCP] " sono documentazione pronta ma commentata: finché
lo sono, --tcp non deve esistere nel parser; quando il commit TCP le
attiva, il test pretende che il parser la accetti.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

import pytest

from klamav_py.cli import add_endpoint_arguments, build_parser

ROOT = Path(__file__).resolve().parent.parent
MAN_DIR = ROOT / "docs" / "man"
GUI_APP = ROOT / "klamav_py" / "gui" / "app.py"

# Pagina -> lingue attese. Una lingua nuova va aggiunta qui: un file in
# più in docs/man senza voce qui fa fallire test_nessuna_pagina_sconosciuta.
PAGES = {
    "klamav-py": ["", ".it"],
    "klamav-py-gui": ["", ".it"],
}

TCP_PREFIX = '.\\" [TCP] '
# Riga che segue .TP: ".B --opzione" o ".BI --opzione ..."
_OPT_RE = re.compile(r'^\.BI? ((?:\\-){2}[a-z][a-z\\-]*)')
# Riga che segue .TP: ".B comando" o ".BI comando ..." (minuscole, niente
# trattini iniziali: esclude codici di uscita, variabili d'ambiente, file)
_CMD_RE = re.compile(r'^\.BI? ([a-z][a-z-]*)(?:\s|$)')


def _files() -> list[Path]:
    return [MAN_DIR / f"{page}{lang}.1" for page, langs in PAGES.items() for lang in langs]


def _read(path: Path, tcp: bool = False) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if tcp:
        lines = [l[len(TCP_PREFIX):] if l.startswith(TCP_PREFIX) else l for l in lines]
    return lines


def _tagged(lines: list[str], regex: re.Pattern) -> set[str]:
    """Primo token delle righe che seguono un .TP e corrispondono a regex."""
    found = set()
    for prev, line in zip(lines, lines[1:]):
        if prev.strip() == ".TP":
            m = regex.match(line)
            if m:
                found.add(m.group(1).replace("\\-", "-"))
    return found


def documented_options(path: Path, tcp: bool = False) -> set[str]:
    return _tagged(_read(path, tcp), _OPT_RE)


def documented_commands(path: Path) -> set[str]:
    return _tagged(_read(path), _CMD_RE)


def _subparsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def cli_options() -> set[str]:
    parser = build_parser()
    parsers = [parser, *_subparsers(parser).values()]
    return {
        opt
        for p in parsers
        for action in p._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }


def _options_of(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }


def gui_options() -> set[str]:
    tree = ast.parse(GUI_APP.read_text(encoding="utf-8"))
    opts = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if name == "add_argument":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and str(arg.value).startswith("--"):
                    opts.add(arg.value)
        elif name == "add_endpoint_arguments":
            # --socket/--tcp arrivano dalla funzione condivisa con la CLI
            # (che non importa Qt): si leggono dal parser che costruisce.
            shared = argparse.ArgumentParser()
            add_endpoint_arguments(shared, None)
            opts |= _options_of(shared)
    return opts


# -- sintassi roff ------------------------------------------------------

@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_file_esiste(path):
    assert path.is_file(), f"man page mancante: {path}"


@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_commenti_roff_integri(path):
    # '."' (backslash perso in un copia-incolla) è una chiamata a una
    # macro inesistente: groff la salta in silenzio, ma la riga non è più
    # un commento e i blocchi [TCP] non sono più riconoscibili.
    rotte = [n for n, l in enumerate(_read(path), 1) if l.startswith('."')]
    assert not rotte, f"{path.name}: commenti senza backslash alle righe {rotte}"


def test_nessuna_pagina_sconosciuta():
    attese = {p.name for p in _files()}
    presenti = {p.name for p in MAN_DIR.glob("*.1")}
    assert presenti == attese


# -- coerenza col codice ------------------------------------------------

@pytest.mark.parametrize("lang", PAGES["klamav-py"])
def test_opzioni_cli(lang):
    assert documented_options(MAN_DIR / f"klamav-py{lang}.1") == cli_options()


@pytest.mark.parametrize("lang", PAGES["klamav-py"])
def test_comandi_cli(lang):
    assert documented_commands(MAN_DIR / f"klamav-py{lang}.1") == set(_subparsers(build_parser()))


@pytest.mark.parametrize("lang", PAGES["klamav-py-gui"])
def test_opzioni_gui(lang):
    assert documented_options(MAN_DIR / f"klamav-py-gui{lang}.1") == gui_options()


# -- coerenza fra traduzioni --------------------------------------------

@pytest.mark.parametrize("page", PAGES)
def test_traduzioni_allineate(page):
    paths = [MAN_DIR / f"{page}{lang}.1" for lang in PAGES[page]]
    riferimento, *altre = paths
    for p in altre:
        # Stesse opzioni anche con i blocchi [TCP] attivati: una lingua
        # aggiornata e l'altra no emergerebbe solo al commit TCP.
        assert documented_options(p, tcp=True) == documented_options(riferimento, tcp=True), p.name
        # Stesso numero di blocchi [TCP] contigui: il numero di righe può
        # variare con la traduzione, quello dei blocchi no.
        assert _tcp_blocks(p) == _tcp_blocks(riferimento), p.name


def _tcp_blocks(path: Path) -> int:
    blocchi, dentro = 0, False
    for line in _read(path):
        is_tcp = line.startswith(TCP_PREFIX)
        blocchi += is_tcp and not dentro
        dentro = is_tcp
    return blocchi


# -- versione -----------------------------------------------------------
# Come per CHANGELOG.md e PKGBUILD in test_changelog.py: la versione nel
# piè di pagina segue __version__, quindi il bump la deve aggiornare.

@pytest.mark.parametrize("path", _files(), ids=lambda p: p.name)
def test_versione_th(path):
    from klamav_py import __version__

    th = next(l for l in _read(path) if l.startswith(".TH "))
    assert f'"KlamAV-Py {__version__}"' in th, f"{path.name}: {th}"
