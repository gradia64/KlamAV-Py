"""
Test per la logica di parsing di ClamdClient. Non aprono nessuna
connessione reale a clamd: testano solo i metodi statici che
interpretano le risposte testuali del protocollo.
"""

from __future__ import annotations

import pytest

from klamav_py.clamd_client import ClamdClient, ClamdError


def test_parse_ok_con_path():
    result = ClamdClient._parse_result_line("/tmp/pulito.txt: OK", fallback_path="/tmp/pulito.txt")
    assert result.status == "OK"
    assert result.path == "/tmp/pulito.txt"
    assert not result.infected


def test_parse_found_con_firma():
    result = ClamdClient._parse_result_line(
        "/tmp/eicar.txt: Eicar-Test-Signature FOUND", fallback_path="/tmp/eicar.txt"
    )
    assert result.status == "FOUND"
    assert result.infected
    assert result.path == "/tmp/eicar.txt"
    assert result.signature == "Eicar-Test-Signature"


def test_parse_ok_con_stream_usa_fallback_path():
    # Con INSTREAM clamd risponde "stream: OK" perché non conosce il
    # path reale: qui deve sempre usare il fallback_path passato da chi
    # ha inviato lo stream.
    result = ClamdClient._parse_result_line("stream: OK", fallback_path="/home/utente/file.bin")
    assert result.status == "OK"
    assert result.path == "/home/utente/file.bin"


def test_parse_found_con_stream_usa_fallback_path():
    result = ClamdClient._parse_result_line(
        "stream: Eicar-Test-Signature FOUND", fallback_path="/home/utente/file.bin"
    )
    assert result.infected
    assert result.path == "/home/utente/file.bin"
    assert result.signature == "Eicar-Test-Signature"


def test_parse_error():
    result = ClamdClient._parse_result_line(
        "/tmp/broken.txt: Permesso negato ERROR", fallback_path="/tmp/broken.txt"
    )
    assert result.status == "ERROR"
    assert not result.infected
    assert not result.too_large


def test_parse_stream_size_limit_exceeded_classificato_come_too_large():
    # Messaggio reale che clamd manda quando un file supera
    # StreamMaxLength (clamd.conf): NON deve finire nel bucket generico
    # "ERROR", perché non è un malfunzionamento — è un file non verificato.
    result = ClamdClient._parse_result_line(
        "INSTREAM size limit exceeded. ERROR", fallback_path="/tmp/file_enorme.bin"
    )
    assert result.status == "TOO_LARGE"
    assert result.too_large
    assert not result.infected
    assert result.path == "/tmp/file_enorme.bin"


def test_parse_error_generico_non_diventa_too_large():
    # Un errore che casualmente contenesse la parola "size" ma non il
    # messaggio esatto non deve essere riclassificato per sbaglio.
    result = ClamdClient._parse_result_line(
        "/tmp/x: Some other error message ERROR", fallback_path="/tmp/x"
    )
    assert result.status == "ERROR"
    assert not result.too_large


def test_parse_risposta_non_riconosciuta():
    with pytest.raises(ClamdError):
        ClamdClient._parse_result_line("qualcosa di inatteso", fallback_path="/tmp/x")


def test_strip_session_id_con_prefisso():
    assert ClamdClient._strip_session_id("3: stream: OK") == "stream: OK"


def test_strip_session_id_senza_prefisso():
    # Senza un prefisso numerico valido, la stringa va lasciata intatta.
    assert ClamdClient._strip_session_id("stream: OK") == "stream: OK"
