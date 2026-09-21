"""
Worker per il controllo aggiornamenti via GitHub Releases API.
Gira in QThread separato per non bloccare la UI.
"""

from __future__ import annotations

import json
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
            self.error.emit(f"Errore HTTP {exc.code} dal server GitHub")
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
        def parse(v: str):
            parts = []
            for p in v.split("."):
                num = ""
                for ch in p:
                    if ch.isdigit():
                        num += ch
                    else:
                        break
                parts.append(int(num) if num else 0)
            return parts

        pa, pb = parse(a), parse(b)
        for x, y in zip(pa, pb):
            if x != y:
                return 1 if x > y else -1
        if len(pa) != len(pb):
            return 1 if len(pa) > len(pb) else -1
        return 0
