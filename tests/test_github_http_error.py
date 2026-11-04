"""
Messaggio del controllo aggiornamenti quando GitHub risponde con un errore.

Il 2026-11-04 il controllo mostrava «Errore HTTP 403 dal server GitHub»:
era il limite anonimo dell'API (60 richieste all'ora per indirizzo IP),
esaurito da altro traffico dello stesso IP. Il messaggio ora dice che è un
limite e a che ora riprovare; un 403 senza le intestazioni del limite
resta un errore HTTP.
"""

from __future__ import annotations

import time
import urllib.error
from email.message import Message

import pytest

pytest.importorskip("PySide6")

from klamav_py.gui.update_check_worker import UpdateCheckWorker, github_http_error  # noqa: E402

NOW = 1_800_000_000.0


def _http_error(code: int, **headers: str) -> urllib.error.HTTPError:
    msg = Message()
    for name, value in headers.items():
        msg[name.replace("_", "-")] = value
    return urllib.error.HTTPError(UpdateCheckWorker.GITHUB_API_URL, code, "x", msg, None)


def _hhmm(epoch: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch))


def test_limite_con_azzeramento_futuro():
    reset = NOW + 1800
    exc = _http_error(403, X_RateLimit_Remaining="0", X_RateLimit_Reset=str(int(reset)))
    assert github_http_error(exc, now=NOW) == (
        "Limite di richieste a GitHub raggiunto (60 all'ora per indirizzo IP): "
        f"riprova dopo le {_hhmm(reset)}"
    )


@pytest.mark.parametrize("code", [403, 429])
def test_retry_after(code):
    exc = _http_error(code, Retry_After="120")
    assert github_http_error(exc, now=NOW).endswith(f"riprova dopo le {_hhmm(NOW + 120)}")


def test_429_senza_intestazioni_e_un_limite():
    assert github_http_error(_http_error(429), now=NOW) == (
        "Limite di richieste a GitHub raggiunto, riprova più tardi"
    )


@pytest.mark.parametrize(
    "headers",
    [
        {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": str(int(NOW - 60))},  # passato
        {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "domani"},  # illeggibile
        {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": "nan"},
        {"X_RateLimit_Remaining": "0", "X_RateLimit_Reset": str(int(NOW + 2 * 86400))},  # oltre 24 h
        {"X_RateLimit_Remaining": "0"},  # orario assente
        {"Retry_After": "Wed, 04 Nov 2026 19:36:00 GMT"},  # data HTTP, non secondi
    ],
)
def test_limite_senza_orario_plausibile(headers):
    assert github_http_error(_http_error(403, **headers), now=NOW) == (
        "Limite di richieste a GitHub raggiunto, riprova più tardi"
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        # Richieste rimaste: il 403 non viene dal limite.
        {"X_RateLimit_Remaining": "42", "X_RateLimit_Reset": str(int(NOW + 600))},
    ],
)
def test_403_senza_limite_resta_errore_http(headers):
    assert github_http_error(_http_error(403, **headers), now=NOW) == (
        "Errore HTTP 403 dal server GitHub"
    )


def test_500():
    exc = _http_error(500, X_RateLimit_Remaining="0", X_RateLimit_Reset=str(int(NOW + 600)))
    assert github_http_error(exc, now=NOW) == "Errore HTTP 500 dal server GitHub"


def test_il_worker_emette_il_messaggio_del_limite(monkeypatch):
    reset = time.time() + 1800

    def fail(*_args, **_kwargs):
        # Eccezione nuova a ogni chiamata, mai tenuta in un locale del test:
        # il traceback porterebbe al frame di run() e quindi al QThread
        # (vedi qobjects_in_cycles in conftest.py).
        raise _http_error(403, X_RateLimit_Remaining="0", X_RateLimit_Reset=str(int(reset)))

    monkeypatch.setattr("urllib.request.urlopen", fail)
    worker = UpdateCheckWorker("0.1.19")
    messages: list[str] = []
    worker.error.connect(messages.append)
    worker.run()  # nel thread del test: niente rete, niente event loop
    assert messages == [
        "Limite di richieste a GitHub raggiunto (60 all'ora per indirizzo IP): "
        f"riprova dopo le {_hhmm(reset)}"
    ]
