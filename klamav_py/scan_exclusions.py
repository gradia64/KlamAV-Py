"""
Validazione delle directory escluse dalle scansioni programmate.

Unica fonte della regola, usata dalle Impostazioni della GUI, dal
generatore del drop-in della unit klamav-scan.service e dalla CLI
(--exclude), sul modello di quarantine_location.py. Il modulo non importa
nulla di Qt.

Una sola lista vale per entrambe le scansioni programmate (timer systemd,
radice la home; scheduler interno, radice schedule_target): le due sono
alternative, e se le esclusioni cambiassero passando dall'una all'altra
la copertura cambierebbe in silenzio. Per questo decide() riceve le
radici come parametro e ogni esclusione è confrontata con tutte.

Cosa si salva e cosa si confronta sono due forme diverse:

- stored: expanduser(), assoluta, NON risolta. È quella che finisce nelle
  Impostazioni e nell'ExecStart del drop-in, e che si risolve di nuovo a
  ogni scansione (la CLI lo fa già). Salvare la forma risolta fisserebbe
  la destinazione di un symlink al momento della configurazione.
- path: resolve(), solo per i confronti, perché il matching in
  clamd_client._iter_files è letterale (is_relative_to) e la traversata
  parte da una radice risolta.

Due livelli di esito:

- error: bloccante (vuoto, caratteri di controllo, relativo senza cwd,
  non una directory, uguale a una radice o che la contiene);
- warnings: mai bloccanti (non esiste ancora, fuori da una radice e quindi
  senza effetto su quella scansione).

Le esclusioni di file sono un errore e non un avviso: _iter_files sfoltisce
solo le directory, i file vengono comunque scansionati, quindi
un'esclusione di file verrebbe ignorata senza segnalazioni.

Ordine della pipeline, come in quarantine_location: expanduser() prima di
tutto, poi il rifiuto (o l'ancoraggio a cwd) dei relativi, resolve(), e
solo dopo i confronti.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# Stessa espressione della quarantena: la regola sui caratteri non diverge.
from .quarantine_location import _CONTROL_CHARS


@dataclass(frozen=True)
class ExclusionDecision:
    stored: str | None
    path: Path | None
    error: str | None = None
    warnings: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return self.error is None


def _labels_by_root(roots: Mapping[str, Path]) -> dict[Path, list[str]]:
    """Radici risolte -> etichette, nell'ordine dato. Due scansioni con la
    stessa radice (schedule_target predefinito = home) producono un solo
    avviso invece di due identici."""
    grouped: dict[Path, list[str]] = {}
    for label, root in roots.items():
        grouped.setdefault(Path(root).expanduser().resolve(), []).append(label)
    return grouped


def _join(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " e " + labels[-1]


def decide(raw: str, *, roots: Mapping[str, Path], cwd: Path | None = None) -> ExclusionDecision:
    """
    Valuta una directory da escludere scritta dall'utente.

    roots: etichetta breve -> radice della scansione (es.
    {"timer systemd": home, "pianificazione interna": target}). cwd: se indicata,
    un percorso relativo è ancorato lì (CLI: in una shell la directory
    corrente è significativa); se None è un errore (GUI: lanciata da
    Dolphin o dal menu, la directory corrente è arbitraria).
    """
    if not raw or not raw.strip():
        return ExclusionDecision(None, None, error="Directory da escludere non indicata.")
    if _CONTROL_CHARS.search(raw):
        return ExclusionDecision(
            None, None, error="Il percorso contiene caratteri di controllo (a capo, tabulazioni)."
        )

    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        if cwd is None:
            return ExclusionDecision(
                None,
                None,
                error=(
                    f"Il percorso deve essere assoluto: «{expanded}» verrebbe "
                    "interpretato rispetto a una directory di lavoro arbitraria."
                ),
            )
        expanded = Path(cwd) / expanded

    stored = str(expanded)
    path = expanded.resolve()
    by_root = _labels_by_root(roots)

    for root, labels in by_root.items():
        if root == path or root.is_relative_to(path):
            return ExclusionDecision(
                stored,
                path,
                error=(
                    f"«{path}» contiene {root}, da cui parte la scansione "
                    f"({_join(labels)}): verrebbe esclusa per intero."
                ),
            )

    warnings: list[str] = []
    try:
        st = os.stat(path)
    except FileNotFoundError:
        warnings.append(
            f"«{path}» non esiste: verrà esclusa quando sarà creata. "
            "Controlla che il percorso sia scritto correttamente."
        )
    except OSError as exc:
        warnings.append(f"Impossibile verificare «{path}»: {exc.strerror}.")
    else:
        if not stat.S_ISDIR(st.st_mode):
            return ExclusionDecision(
                stored,
                path,
                error=(
                    f"«{path}» non è una directory: si possono escludere solo "
                    "directory, i singoli file verrebbero scansionati comunque."
                ),
            )

    for root, labels in by_root.items():
        if not path.is_relative_to(root):
            warnings.append(
                f"«{path}» è fuori da {root}: non ha effetto sulla scansione "
                f"che parte da lì ({_join(labels)})."
            )

    return ExclusionDecision(stored, path, warnings=tuple(warnings))
