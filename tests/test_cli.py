"""
Test per le funzioni pure della CLI (parsing/raggruppamento errori).
Non lanciano clamd né toccano il filesystem reale.
"""

from __future__ import annotations

from klamav_py.cli import _error_category, build_parser


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
