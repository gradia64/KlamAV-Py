"""
Quando un file infetto NON va messo in quarantena automaticamente.

Logica pura, condivisa da GUI (ScanWorker) e CLI: la stessa scansione
deve decidere allo stesso modo da qualunque parte parta.

Due casi, entrambi "segnala ma non spostare":

1. Firme euristiche (prefisso "Heuristics."): indicano un contenuto
   sospetto, non un malware identificato. L'esempio che ha motivato la
   regola: Heuristics.Phishing.Email.SpoofedDomain su singole email
   dell'archivio di KMail.

2. Archivi di posta e dati di applicazioni che tengono un indice proprio
   dei file. Spostarne un file rompe l'applicazione (Akonadi ha ancora
   nel database elementi che puntano al file sparito) e, nel caso dei
   formati mbox (Thunderbird), un solo messaggio sospetto farebbe finire
   in quarantena l'intera cartella di posta.

La quarantena manuale (pulsante nella GUI) resta sempre possibile: la
regola riguarda solo la decisione automatica.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

REPORT_ONLY_SIGNATURE_PREFIXES: tuple[str, ...] = ("Heuristics.",)

# Relativi alla home dell'utente.
DEFAULT_REPORT_ONLY_DIRS: tuple[str, ...] = (
    ".local/share/akonadi",            # KDE PIM: database e file_db_data
    ".local/share/local-mail",         # KMail: cartelle locali (maildir)
    ".thunderbird",                    # Thunderbird (mbox o maildir)
    ".var/app/org.mozilla.Thunderbird",  # Thunderbird flatpak
    ".local/share/evolution/mail",     # Evolution
    "Maildir",                         # maildir classica
)

REASON_HEURISTIC = "firma euristica: solo segnalazione"
REASON_MAIL_STORE = "archivio di posta o dati di un'applicazione: solo segnalazione"


@dataclass(frozen=True)
class QuarantineDecision:
    quarantine: bool
    reason: Optional[str] = None  # motivo per cui NON si mette in quarantena


def default_report_only_dirs(home: Optional[Path] = None) -> list[Path]:
    base = Path(home) if home is not None else Path.home()
    return [base / rel for rel in DEFAULT_REPORT_ONLY_DIRS]


def _resolve(p: Path) -> Path:
    try:
        return Path(p).expanduser().resolve()
    except OSError:
        return Path(p).expanduser().absolute()


class QuarantinePolicy:
    def __init__(
        self,
        report_only_dirs: Optional[Iterable[Path]] = None,
        signature_prefixes: Sequence[str] = REPORT_ONLY_SIGNATURE_PREFIXES,
        enabled: bool = True,
    ) -> None:
        dirs = default_report_only_dirs() if report_only_dirs is None else report_only_dirs
        # Risolte una volta: le directory possono non esistere (resolve
        # non è strict), e il confronto va fatto in forma canonica come
        # per le esclusioni della scansione.
        self._dirs = [_resolve(d) for d in dirs]
        self._prefixes = tuple(signature_prefixes)
        self.enabled = enabled

    @property
    def report_only_dirs(self) -> list[Path]:
        return list(self._dirs)

    def decide(self, path: Path, signature: Optional[str]) -> QuarantineDecision:
        if not self.enabled:
            return QuarantineDecision(True)
        if signature and signature.startswith(self._prefixes):
            return QuarantineDecision(False, REASON_HEURISTIC)
        resolved = _resolve(path)
        if any(resolved.is_relative_to(d) for d in self._dirs):
            return QuarantineDecision(False, REASON_MAIL_STORE)
        return QuarantineDecision(True)
