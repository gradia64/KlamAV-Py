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
