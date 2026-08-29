"""
Test per _decode_ipc_payload: la parte pura (nessuna dipendenza da
QLocalServer/QLocalSocket reali) della gestione dei dati ricevuti dal
socket IPC single-instance.

Nota sul limite di dimensione (_IPC_MAX_PAYLOAD_BYTES): il troncamento
avviene lato lettura dal socket (client.read(N) in _on_ipc_connection,
non testato qui perché richiede un QLocalServer/QLocalSocket reali) —
qui si verifica solo che _decode_ipc_payload gestisca correttamente un
payload già troncato a quella dimensione, incluso il caso limite in cui
il troncamento cade a metà di un carattere UTF-8 multi-byte.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from klamav_py.gui.main_window import _IPC_MAX_PAYLOAD_BYTES, _decode_ipc_payload


def test_payload_vuoto_ritorna_none():
    assert _decode_ipc_payload(b"") is None


def test_payload_percorso_valido():
    assert _decode_ipc_payload("/home/utente/file.txt".encode("utf-8")) == "/home/utente/file.txt"


def test_payload_percorso_con_caratteri_utf8():
    # Percorsi con caratteri accentati/non-ASCII sono legittimi (es. nomi
    # di cartelle in italiano): non devono essere respinti come "malformati".
    assert _decode_ipc_payload("/home/utente/città/report.pdf".encode("utf-8")) == "/home/utente/città/report.pdf"


def test_payload_utf8_malformato_ritorna_none_senza_eccezioni():
    # Sequenza di byte non valida come UTF-8 (0xff non è un lead byte
    # valido in nessuna sequenza UTF-8): non deve propagare
    # UnicodeDecodeError al chiamante (l'event loop Qt).
    assert _decode_ipc_payload(b"\xff\xfe\x00\x01") is None


def test_payload_troncato_a_meta_carattere_multibyte_ritorna_none():
    # Simula il caso limite di un payload legittimo troncato esattamente
    # a metà di un carattere multi-byte dal limite di lettura del socket:
    # "città" codificato UTF-8, troncato di un byte alla fine (spezza la
    # "à", 2 byte in UTF-8). Deve fallire in modo pulito, non con
    # un'eccezione non gestita né con un percorso corrotto silenzioso.
    completo = "/home/città".encode("utf-8")
    troncato = completo[:-1]
    assert _decode_ipc_payload(troncato) is None


def test_limite_dimensione_e_generoso_ma_non_illimitato():
    # PATH_MAX su Linux è 4096: il limite deve essere pensato per
    # percorsi legittimi (anche con margine UTF-8), non per payload da
    # megabyte pensati per un attacco DoS.
    assert 4096 <= _IPC_MAX_PAYLOAD_BYTES <= 8192
