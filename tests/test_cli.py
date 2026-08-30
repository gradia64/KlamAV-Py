"""
Test per le funzioni pure della CLI (parsing/raggruppamento errori).
Non lanciano clamd né toccano il filesystem reale.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from klamav_py import __version__
from klamav_py.cli import _error_category, build_parser, main


def test_error_category_rimuove_errno():
    assert _error_category("[Errno 13] Permission denied") == "Permission denied"


def test_error_category_normalizza_path():
    cleaned = _error_category("impossibile aprire /home/utente/file segreto.txt")
    assert "/home/utente" not in cleaned
    assert "<path>" in cleaned


def test_error_category_raggruppa_stesso_tipo():
    a = _error_category("[Errno 13] Permission denied: '/home/a/x.txt'")
    b = _error_category("[Errno 13] Permission denied: '/home/b/y.txt'")
    assert a == b


def test_parser_scan_richiede_path():
    parser = build_parser()
    args = parser.parse_args(["scan", "/tmp"])
    assert args.command == "scan"
    assert str(args.path) == "/tmp"
    assert args.no_persistent is False
    assert args.session_batch_size == 500


def test_parser_ping():
    parser = build_parser()
    args = parser.parse_args(["ping"])
    assert args.command == "ping"


def test_cmd_scan_conta_too_large_separatamente(tmp_path, monkeypatch, capsys):
    # I file oltre StreamMaxLength non devono finire nel conteggio
    # "errori": sono un caso a parte (non verificati), non un
    # malfunzionamento. Qui si finge una scan_stream() che restituisce
    # un mix dei tre casi, senza toccare clamd davvero.
    # **kwargs nella firma del fake: la CLI ora passa anche
    # exclude_dirs/max_stream_size a scan_stream (e potrà passare
    # altri parametri in futuro) senza rompere questo test.
    from klamav_py.cli import cmd_scan
    from klamav_py.clamd_client import ClamdClient, ScanResult

    def fake_scan_stream(self, path, **kwargs):
        yield ScanResult(path="/tmp/pulito.txt", status="OK")
        yield ScanResult(
            path="/tmp/enorme.bin",
            status="TOO_LARGE",
            signature="INSTREAM size limit exceeded. ERROR",
        )
        yield ScanResult(path="/tmp/rotto.txt", status="ERROR", signature="Permission denied")

    monkeypatch.setattr(ClamdClient, "scan_stream", fake_scan_stream)

    parser = build_parser()
    args = parser.parse_args(["scan", str(tmp_path), "--quiet"])
    exit_code = cmd_scan(args)

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "3 file scansionati, 0 infetti, 1 errori." in captured.out
    assert "1 file oltre StreamMaxLength, non verificati" in captured.out


def test_flag_version_stampa_versione_ed_esce(capsys):
    """
    argparse con action="version" stampa su stdout ed esce con codice 0
    sollevando SystemExit: va intercettato, non è un errore.
    """
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert __version__ in out
    assert "klamav-py" in out


def test_version_coerente_con_changelog_debian():
    """
    __version__ è la fonte mostrata da --version e nella GUI. Se diverge
    dalla parte upstream di debian/changelog, --version mente al primo
    che la usa per un bug report — e nessuno se ne accorge.

    Confronta solo la parte upstream: il suffisso di revisione Debian
    ("-2" in "0.1.5-2") è legittimo che esista solo nel changelog.
    """
    changelog = Path(__file__).resolve().parent.parent / "debian" / "changelog"
    if not changelog.exists():
        pytest.skip("debian/changelog non presente (sorgente non completo)")

    prima_riga = changelog.read_text(encoding="utf-8").splitlines()[0]
    match = re.match(r"^\S+ \(([^)]+)\)", prima_riga)
    assert match, f"prima riga di debian/changelog non riconosciuta: {prima_riga!r}"

    upstream = match.group(1).split("-")[0]
    assert upstream == __version__, (
        f"debian/changelog dice {upstream}, klamav_py.__version__ dice "
        f"{__version__}: allinearli prima del rilascio"
    )


def test_pyproject_non_duplica_la_versione():
    """
    pyproject.toml deve derivare la versione da klamav_py.__version__,
    non ridichiararla. Era una terza fonte di verità (dopo __init__.py e
    debian/changelog) ed è rimasta indietro alla 0.1.4 mentre le altre
    due erano già alla 0.1.5 — esattamente il disallineamento che una
    versione duplicata rende inevitabile.
    """
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    if not pyproject.exists():
        pytest.skip("pyproject.toml non presente (sorgente non completo)")

    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        pytest.skip("tomllib non disponibile su questa versione di Python")

    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data["project"]

    assert "version" not in project, (
        "pyproject.toml dichiara una versione statica: usare "
        'dynamic = ["version"] con [tool.setuptools.dynamic] '
        "version = { attr = \"klamav_py.__version__\" }"
    )
    assert "version" in project.get("dynamic", [])
    assert (
        data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        == "klamav_py.__version__"
    )
