"""
CLI di scansione. Pensata per due usi:

  1. interattivo:      klamav-py scan /percorso/da/controllare
  2. da systemd timer: klamav-py scan /home --quarantine --quiet
     (vedi le unit utente in debian/ e systemd/)

La directory di quarantena (--quarantine) è sempre esclusa
automaticamente dall'attraversamento: i file già gestiti non devono
essere ri-rilevati (e ri-quarantenati) a ogni scansione che la copre.

Codici di uscita: 0 = pulito, 1 = infezioni trovate, 2 = errore di
esecuzione (clamd irraggiungibile, path inesistente, ecc.) — utile per
`OnFailure=` in systemd o per script di monitoraggio.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .clamd_client import DEFAULT_MAX_STREAM_SIZE, ClamdClient, ClamdError
from .quarantine import Quarantine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klamav-py", description="Scanner ClamAV via clamd")
    # Deliberatamente senza "-V" abbreviato: resta libero per usi futuri
    # e non rischia collisioni con un eventuale "-v" verbose.
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--socket",
        default="/run/clamav/clamd.ctl",
        help="Percorso del socket Unix di clamd (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Scansiona un file o una directory")
    scan.add_argument("path", type=Path)
    scan.add_argument(
        "--quarantine",
        metavar="DIR",
        type=Path,
        help="Se specificato, sposta i file infetti in questa directory",
    )
    scan.add_argument(
        "--exclude",
        metavar="DIR",
        type=Path,
        action="append",
        default=[],
        help=(
            "Directory da escludere dall'attraversamento ricorsivo "
            "(ripetibile: una volta per ogni directory). Le directory "
            "escluse non vengono nemmeno lette. La directory di "
            "quarantena (--quarantine) è esclusa automaticamente."
        ),
    )
    scan.add_argument(
        "--max-stream-size",
        metavar="BYTES",
        type=int,
        default=None,
        help=(
            "Soglia del pre-check dimensionale in byte: i file oltre "
            "questa dimensione non vengono inviati a clamd (risparmia "
            "sessione e I/O) e sono segnalati come 'non verificati'. "
            "Default: 25MB, allineato a StreamMaxLength di clamd.conf. "
            "0 disattiva il pre-check."
        ),
    )
    scan.add_argument("--quiet", action="store_true", help="Stampa solo le infezioni trovate")
    scan.add_argument(
        "--no-persistent",
        action="store_true",
        help="Apre una nuova connessione a clamd per ogni file invece di riusare una sessione IDSESSION "
        "(più lento, utile solo per isolare problemi legati alla sessione persistente)",
    )
    scan.add_argument(
        "--session-batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Numero di file dopo cui la sessione persistente viene richiusa e riaperta (default: %(default)s)",
    )
    scan.add_argument(
        "--log-errors",
        metavar="FILE",
        type=Path,
        help="Scrive il dettaglio di ogni errore (path e motivo) su questo file, uno per riga",
    )

    sub.add_parser("ping", help="Verifica che clamd risponda")

    return parser


def _error_category(signature: str) -> str:
    """
    Raggruppa i messaggi di errore per tipo, ripulendoli dai dettagli
    specifici del singolo file (path, numero di errno) così da poter
    contare quanti errori sono "dello stesso tipo" invece di vedere 43
    righe tutte diverse.
    """
    # normalizza "[Errno N] <messaggio>" -> "<messaggio>"
    cleaned = re.sub(r"^\[Errno \d+\]\s*", "", signature)
    # normalizza eventuali path assoluti dentro il messaggio
    cleaned = re.sub(r"/\S+", "<path>", cleaned)
    return cleaned.strip()


def cmd_scan(args: argparse.Namespace) -> int:
    scan_root_input = args.path.expanduser()
    if not scan_root_input.exists():
        print(f"Percorso inesistente: {args.path}", file=sys.stderr)
        return 2

    # resolve() su TUTTI i percorsi coinvolti: il matching delle
    # esclusioni in clamd_client._iter_files confronta path letterali
    # (is_relative_to), quindi scan root ed esclusioni devono essere
    # nella stessa forma canonica — altrimenti un'esclusione può non
    # matchare per un dettaglio di forma (slash finale, symlink,
    # '~' non espanso). Il resolve() del root cambia anche i path
    # mostrati nei risultati (es. /home/utente invece di un symlink):
    # su sistemi tipici sono identici.
    scan_root = scan_root_input.resolve()
    exclude_dirs = [Path(p).expanduser().resolve() for p in args.exclude]
    if args.quarantine:
        quarantine_dir = args.quarantine.expanduser().resolve()
        # L'esclusione della directory di quarantena va SEMPRE aggiunta,
        # indipendentemente da --exclude: è il dato gestito
        # dall'applicazione stessa, ri-rilevarlo a ogni scansione che
        # lo copre (es. scansione della home) gonfia per sempre
        # infetti/errori con fantasmi già gestiti.
        exclude_dirs.append(quarantine_dir)
    else:
        quarantine_dir = None

    # Soglia del pre-check: None da argparse = default della libreria
    # (DEFAULT_MAX_STREAM_SIZE, allineato a StreamMaxLength di
    # clamd.conf); valore <= 0 esplicito = pre-check disattivato
    # (max_stream_size=None nel client: TOO_LARGE riconosciuto solo
    # dalla risposta letterale di clamd).
    if args.max_stream_size is None:
        max_stream_size = DEFAULT_MAX_STREAM_SIZE
    elif args.max_stream_size <= 0:
        max_stream_size = None
    else:
        max_stream_size = args.max_stream_size

    client = ClamdClient(unix_socket=str(args.socket))
    quarantine = Quarantine(quarantine_dir) if quarantine_dir else None

    scanned = 0
    infections = 0
    errors = 0
    too_large = 0
    error_categories: Counter[str] = Counter()
    log_fh = args.log_errors.open("w", encoding="utf-8") if args.log_errors else None

    try:
        for result in client.scan_stream(
            scan_root,
            persistent=not args.no_persistent,
            session_batch_size=args.session_batch_size,
            exclude_dirs=exclude_dirs,
            max_stream_size=max_stream_size,
        ):
            scanned += 1
            if result.infected:
                infections += 1
                print(f"INFETTO: {result.path} ({result.signature})")
                if quarantine:
                    try:
                        entry = quarantine.quarantine_file(Path(result.path), result.signature)
                        print(f"  -> messo in quarantena: {entry.quarantined_path}")
                    except Exception as exc:  # noqa: BLE001 - vogliamo continuare comunque
                        print(f"  -> quarantena fallita: {exc}", file=sys.stderr)
            elif result.too_large:
                # Non è un malfunzionamento: il file supera semplicemente
                # StreamMaxLength (clamd.conf) e non è stato verificato,
                # non va confuso con un errore generico nel riepilogo.
                too_large += 1
                if not args.quiet:
                    print(f"NON VERIFICATO (troppo grande): {result.path}")
            elif result.status == "ERROR":
                errors += 1
                error_categories[_error_category(result.signature or "")] += 1
                print(f"ERRORE su {result.path}: {result.signature}", file=sys.stderr)
                if log_fh:
                    log_fh.write(f"{result.path}\t{result.signature}\n")
            elif not args.quiet:
                print(f"OK: {result.path}")
    except ClamdError as exc:
        print(f"Errore di comunicazione con clamd: {exc}", file=sys.stderr)
        return 2
    except (ConnectionRefusedError, FileNotFoundError) as exc:
        print(f"clamd non raggiungibile su {args.socket}: {exc}", file=sys.stderr)
        return 2
    finally:
        if log_fh:
            log_fh.close()

    print(f"\n{scanned} file scansionati, {infections} infetti, {errors} errori.")
    if too_large:
        print(f"{too_large} file oltre StreamMaxLength, non verificati (vedi clamd.conf).")

    # Entry saltate: non sono né scansionate né "non verificate" (un
    # symlink o un socket non ha contenuto proprio), quindi restano
    # fuori dai contatori principali e dall'invariante
    # scanned = clean + infetti + errori + troppo grandi. Le mostriamo
    # comunque come informazione diagnostica, non in --quiet.
    if client.skipped and not args.quiet:
        totale_saltati = sum(client.skipped.values())
        dettaglio = ", ".join(f"{n} {tipo}" for tipo, n in client.skipped.most_common())
        print(f"{totale_saltati} voci saltate ({dettaglio}).")

    if error_categories:
        print("\nDettaglio errori per tipo:")
        for category, count in error_categories.most_common():
            print(f"  {count:5d}  {category}")

    if infections:
        return 1

    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    client = ClamdClient(unix_socket=str(args.socket))
    try:
        alive = client.ping()
    except (ClamdError, OSError) as exc:
        print(f"clamd non raggiungibile: {exc}", file=sys.stderr)
        return 2
    print("clamd attivo" if alive else "clamd non ha risposto correttamente")
    return 0 if alive else 2


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "ping":
        return cmd_ping(args)
    parser.error("comando sconosciuto")
    return 2


if __name__ == "__main__":
    sys.exit(main())
