"""
Riepilogo numerico di una scansione, condiviso da worker della GUI,
cronologia e CLI. Senza Qt.

Un solo oggetto invece di argomenti posizionali: i segnali progress e
finished_scan di ScanWorker e HistoryManager.add_entry passavano quattro
interi, e ogni contatore nuovo li faceva crescere (e con loro ogni slot).
Un campo nuovo ha un default, così chi non lo conosce resta valido; le voci
di cronologia scritte da versioni precedenti si leggono con from_entry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields


@dataclass(frozen=True)
class ScanTotals:
    # File passati da clamd, cioè puliti + infetti + errori + troppo grandi.
    scanned: int = 0
    infections: int = 0
    errors: int = 0
    # Oltre StreamMaxLength: non verificati, non un malfunzionamento.
    too_large: int = 0
    # Sottocartelle che non si sono potute leggere (vedi
    # clamd_client._iter_files): copertura mancante, ma fuori da «errori».
    unreadable_dirs: int = 0
    # Rilevamenti «solo segnalazione» già valutati dall'utente
    # (acknowledged.py): non contano fra gli infetti.
    acknowledged: int = 0
    # Errori che sono guasti di I/O (ScanResult.io_fault, regola in
    # clamd_client.is_io_fault): contati ANCHE in errors, come nella CLI.
    # Un sottoinsieme, non una categoria nuova: dice che parte dell'albero
    # non è stata controllata (io_fault_note).
    io_faults: int = 0

    def as_entry(self) -> dict:
        return asdict(self)

    @classmethod
    def from_entry(cls, entry: dict) -> "ScanTotals":
        """Dai campi di una voce di cronologia: quelli mancanti (voci di
        versioni precedenti) valgono 0, quelli non interi pure."""
        values = {}
        for f in fields(cls):
            value = entry.get(f.name, 0)
            values[f.name] = value if isinstance(value, int) and not isinstance(value, bool) else 0
        return cls(**values)


def io_fault_note(count: int) -> str:
    """
    Testo unico per i guasti di I/O di una scansione: ultima riga del
    riepilogo della CLI, stato e referto della scansione manuale della GUI,
    notifica e log della pianificazione interna. Dice che la scansione non
    vale come pulita anche quando l'esito della GUI è solo «con errori».
    """
    return (
        f"{count} guasti di I/O (elencati fra gli errori): parte dell'albero "
        "non è stata controllata, la scansione non vale come pulita."
    )
