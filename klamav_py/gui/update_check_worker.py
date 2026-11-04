"""
Worker per il controllo aggiornamenti via GitHub Releases API.
Gira in QThread separato per non bloccare la UI.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from PySide6.QtCore import QThread, Signal


@dataclass(frozen=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    release_url: str
    release_url_trusted: bool
    release_notes: str
    published_at: str
    has_update: bool


# Stadi di pre-release riconosciuti, nell'ordine: 0.1.12a1 < 0.1.12b1 <
# 0.1.12rc1 < 0.1.12. Un suffisso sconosciuto conta come pre-release del
# livello più basso: una versione con un suffisso qualsiasi non supera mai
# la stessa senza suffisso.
_PRE_STAGES = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "pre": 3, "c": 3, "rc": 3}
_PRE_RE = re.compile(r"^[-_.]?([a-z]*)[-_.]?(\d*)")


def version_key(version: str) -> tuple:
    """
    Chiave ordinabile per le versioni dei tag (0.1.12, v0.1.12,
    0.1.12-rc1, 0.1.12rc1, 0.1.12+deb1).

    Prima i suffissi venivano scartati: 0.1.12-rc1 e 0.1.12 risultavano
    uguali, quindi chi aveva installato la candidata non veniva avvisato
    della versione finale. I componenti numerici si confrontano con gli
    zeri finali ignorati (0.1 == 0.1.0); i metadati di build dopo "+" non
    contano.
    """
    text = version.strip().lower().lstrip("v").split("+", 1)[0]
    match = re.match(r"^(\d+(?:\.\d+)*)(.*)$", text)
    if not match:
        return ((0,), 1, 0, 0)
    release = [int(p) for p in match.group(1).split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    rest = match.group(2)
    if not rest:
        return (tuple(release), 1, 0, 0)  # finale: dopo ogni pre-release
    pre = _PRE_RE.match(rest)
    stage = _PRE_STAGES.get(pre.group(1), -1) if pre else -1
    number = int(pre.group(2)) if pre and pre.group(2) else 0
    return (tuple(release), 0, stage, number)


def github_http_error(exc: urllib.error.HTTPError, now: float | None = None) -> str:
    """
    Messaggio per una risposta di errore dell'API di GitHub.

    Senza autenticazione GitHub concede 60 richieste all'ora per indirizzo
    IP, contate su tutto ciò che esce da quell'indirizzo (altri programmi,
    altri utenti dietro lo stesso NAT dell'operatore): il controllo di
    KlamAV-Py può trovarlo esaurito senza averlo consumato. Oltre il limite
    GitHub risponde 403 (o 429) con X-RateLimit-Remaining: 0 e l'ora di
    azzeramento in X-RateLimit-Reset (epoch), oppure con Retry-After in
    secondi. Un 403 senza quelle intestazioni è un rifiuto vero e non va
    presentato come limite.
    """
    headers = exc.headers or {}
    remaining = (headers.get("X-RateLimit-Remaining") or "").strip()
    retry_after = (headers.get("Retry-After") or "").strip()
    limited = exc.code == 429 or (exc.code == 403 and (remaining == "0" or bool(retry_after)))
    if not limited:
        return f"Errore HTTP {exc.code} dal server GitHub"
    now = time.time() if now is None else now
    reset = None
    try:
        reset_header = (headers.get("X-RateLimit-Reset") or "").strip()
        if remaining == "0" and reset_header:
            reset = float(reset_header)
        elif retry_after:
            # Retry-After può essere anche una data HTTP: illeggibile qui,
            # quindi «riprova più tardi».
            reset = now + float(retry_after)
    except ValueError:
        reset = None
    # Un orario già passato o oltre un giorno non è plausibile (orologio
    # sbagliato, intestazione strana): meglio non indicarne uno.
    if reset is None or reset != reset or reset <= now or reset - now > 24 * 3600:
        return "Limite di richieste a GitHub raggiunto, riprova più tardi"
    return (
        "Limite di richieste a GitHub raggiunto (60 all'ora per indirizzo IP): "
        f"riprova dopo le {time.strftime('%H:%M', time.localtime(reset))}"
    )


class UpdateCheckWorker(QThread):
    """
    Controlla l'ultima release su GitHub confrontando la versione corrente.

    Emette:
        - check_finished(UpdateInfo)  -- sempre, anche se nessun aggiornamento
        - error(str)                  -- se la richiesta fallisce
    """

    check_finished = Signal(object)  # UpdateInfo
    error = Signal(str)

    GITHUB_API_URL = "https://api.github.com/repos/gradia64/KlamAV-Py/releases/latest"
    # Unico prefisso accettato per il link cliccabile nella GUI.
    TRUSTED_RELEASE_PREFIX = "https://github.com/gradia64/KlamAV-Py/releases/"
    REQUEST_TIMEOUT = 15
    # Una release GitHub pesa pochi KB: 256 KB sono un margine ampio e
    # impediscono di leggere in memoria una risposta arbitrariamente grande.
    MAX_RESPONSE_BYTES = 256 * 1024

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self.current_version = current_version

    def run(self) -> None:
        try:
            req = urllib.request.Request(
                self.GITHUB_API_URL,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"KlamAV-Py/{self.current_version}",
                },
            )
            with urllib.request.urlopen(req, timeout=self.REQUEST_TIMEOUT) as resp:
                raw = resp.read(self.MAX_RESPONSE_BYTES + 1)

            if len(raw) > self.MAX_RESPONSE_BYTES:
                self.error.emit("Risposta troppo grande dal server GitHub")
                return

            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                self.error.emit("Risposta non valida dal server GitHub")
                return

            # `or ""`: .get(k, default) non copre il caso di chiave presente
            # con valore JSON null.
            latest_tag = self._str_field(data, "tag_name").lstrip("v")
            release_url = self._str_field(data, "html_url")
            release_notes = self._str_field(data, "body") or "(nessuna nota di rilascio)"
            published_at = self._str_field(data, "published_at")[:10]

            if not latest_tag:
                self.error.emit("Risposta GitHub priva del numero di versione")
                return

            has_update = self._version_compare(latest_tag, self.current_version) > 0

            info = UpdateInfo(
                current_version=self.current_version,
                latest_version=latest_tag,
                release_url=release_url,
                release_url_trusted=release_url.startswith(self.TRUSTED_RELEASE_PREFIX),
                release_notes=release_notes,
                published_at=published_at,
                has_update=has_update,
            )
            self.check_finished.emit(info)

        except urllib.error.HTTPError as exc:
            self.error.emit(github_http_error(exc))
        except urllib.error.URLError as exc:
            self.error.emit(f"Impossibile raggiungere GitHub: {exc.reason}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.error.emit("Risposta non valida dal server GitHub")
        except Exception as exc:  # noqa: BLE001
            self.error.emit(f"Errore imprevisto: {exc}")

    @staticmethod
    def _str_field(data: dict, key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _version_compare(a: str, b: str) -> int:
        """-1, 0 o 1 come a <, = o > b. Vedi version_key."""
        ka, kb = version_key(a), version_key(b)
        return (ka > kb) - (ka < kb)
