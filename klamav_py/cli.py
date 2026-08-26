"""
CLI di scansione. Pensata per due usi:

  1. interattivo:      klamav-py scan /percorso/da/controllare
  2. da systemd timer: klamav-py scan /home --quarantine --quiet
     (vedi systemd/klamav-scan.service e klamav-scan.timer)

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

from .clamd_client import ClamdClient, ClamdError
from .quarantine import Quarantine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klamav-py", description="Scanner ClamAV via clamd")
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
    if not args.path.exists():
        print(f"Percorso inesistente: {args.path}", file=sys.stderr)
        return 2

    client = ClamdClient(unix_socket=str(args.socket))
    quarantine = Quarantine(args.quarantine) if args.quarantine else None

    scanned = 0
    infections = 0
    errors = 0
    error_categories: Counter[str] = Counter()
    log_fh = args.log_errors.open("w", encoding="utf-8") if args.log_errors else None

    try:
        for result in client.scan_stream(
            args.path,
            persistent=not args.no_persistent,
            session_batch_size=args.session_batch_size,
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
