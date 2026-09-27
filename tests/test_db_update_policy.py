"""
db_update_policy: quando il riavvio del freshclam locale aggiorna davvero
il database che clamd usa. La regola è pura (endpoint, versione riportata
da clamd, versione locale iniettabile): nessun clamd e nessun
/var/lib/clamav reale nei test.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from klamav_py.clamd_client import ClamdEndpoint
from klamav_py.db_freshness import DbInfo
from klamav_py.db_update_policy import (
    is_loopback_host,
    local_daily_version,
    parse_db_header,
    update_availability,
)

INFO = DbInfo("1.4.3", 27800, datetime(2026, 9, 27, 9, 0))


def _header(version: int | str) -> bytes:
    text = f"ClamAV-VDB:27 Sep 2026 09-00 +0000:{version}:2073800:90:X:X:raynman:1758963600"
    return text.encode().ljust(512, b" ")


# -- loopback ------------------------------------------------------------

@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "localhost.", "127.0.0.1", "127.1.2.3", "::1", "::ffff:127.0.0.1",
])
def test_loopback(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", [
    "10.0.0.5", "192.168.1.10", "fe80::1%eth0", "::", "nas.local", "localhost.example", "",
])
def test_non_loopback(host):
    # I nomi non si risolvono: anche uno che punta a 127.0.0.1 conta come remoto.
    assert not is_loopback_host(host)


# -- intestazione del database ------------------------------------------

def test_intestazione_valida():
    assert parse_db_header(_header(27800)) == 27800


@pytest.mark.parametrize("data", [b"", b"PK\x03\x04zip", _header("abc"), b"ClamAV-VDB:solo"])
def test_intestazione_non_riconosciuta(data):
    assert parse_db_header(data) is None


def test_versione_locale_cld_e_cvd(tmp_path):
    (tmp_path / "daily.cvd").write_bytes(_header(27790))
    (tmp_path / "daily.cld").write_bytes(_header(27800))
    assert local_daily_version(tmp_path) == 27800


def test_versione_locale_assente_o_illeggibile(tmp_path):
    assert local_daily_version(tmp_path) is None
    (tmp_path / "daily.cld").write_bytes(b"spazzatura")
    assert local_daily_version(tmp_path) is None


# -- regola ---------------------------------------------------------------

def _never():
    raise AssertionError("la versione locale non va letta in questo caso")


@pytest.mark.parametrize("info", [INFO, None])
def test_socket_unix_sempre_consentito(info):
    assert update_availability(ClamdEndpoint(), info, _never).allowed


@pytest.mark.parametrize("info", [INFO, None])
def test_tcp_remoto_mai(info):
    a = update_availability(ClamdEndpoint.tcp("10.0.0.5"), info, _never)
    assert not a.allowed and "10.0.0.5" in a.reason


def test_loopback_stesso_database_consentito():
    assert update_availability(ClamdEndpoint.tcp("127.0.0.1"), INFO, lambda: 27800).allowed


def test_loopback_database_diverso_bloccato():
    # clamd in un container con la porta mappata su localhost.
    a = update_availability(ClamdEndpoint.tcp("localhost"), INFO, lambda: 27795)
    assert not a.allowed
    assert "27800" in a.reason and "27795" in a.reason and "ricaricato" in a.reason


def test_loopback_senza_database_locale_bloccato():
    a = update_availability(ClamdEndpoint.tcp("::1"), INFO, lambda: None)
    assert not a.allowed and "/var/lib/clamav" in a.reason


def test_loopback_con_clamd_irraggiungibile_consentito():
    # Nessuna versione da confrontare: l'esito lo verifica il worker.
    assert update_availability(ClamdEndpoint.tcp("127.0.0.1"), None, _never).allowed
