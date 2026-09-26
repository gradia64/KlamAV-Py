"""
CLI di scansione. Pensata per due usi:

  1. interattivo:      klamav-py scan /percorso/da/controllare
  2. da systemd timer: klamav-py scan %h --quarantine DIR --quiet
     (vedi la unit utente in debian/)

La directory di quarantena (--quarantine) è sempre esclusa
automaticamente dall'attraversamento: i file già gestiti non devono
essere ri-rilevati (e ri-quarantenati) a ogni scansione che la copre.

Con --quarantine, le firme euristiche (Heuristics.*) e i file negli
archivi di posta (KMail/Akonadi, Thunderbird, Evolution, Maildir) sono
solo segnalati, non spostati: vedi quarantine_policy.py. --report-only
aggiunge directory, --quarantine-all disattiva la regola.

Codici di uscita: 0 = pulito, 1 = infezioni trovate, 2 = errore di
esecuzione (clamd irraggiungibile, path inesistente, ecc.) — utile per
`OnFailure=` in systemd o per script di monitoraggio.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

from . import __version__
from .clamd_client import (
    DEFAULT_MAX_STREAM_SIZE,
    DEFAULT_SOCKET,
    ClamdEndpoint,
    ClamdError,
    ClamdUnavailable,
)
from .private_files import open_private_for_write
from .quarantine import Quarantine, QuarantineError
from .quarantine_location import decide as decide_quarantine_dir
from .quarantine_policy import QuarantinePolicy, default_report_only_dirs


def _socket_endpoint(value: str) -> ClamdEndpoint:
    try:
        return ClamdEndpoint.unix(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _tcp_endpoint(value: str) -> ClamdEndpoint:
    try:
        return ClamdEndpoint.parse_cli(value)
    except ValueError as exc:
        # ArgumentTypeError invece di ValueError: argparse mostra il motivo
        # ("porta fuori intervallo…") invece di un generico "invalid value".
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_endpoint_arguments(parser: argparse.ArgumentParser, default: ClamdEndpoint | None) -> None:
    """
    --socket e --tcp, condivisi da CLI e GUI: scrivono lo stesso
    ClamdEndpoint in args.endpoint, così il resto del programma non sa (e
    non deve sapere) quale dei due è stato usato. Mutuamente esclusivi e
    senza ripiego automatico: una configurazione sbagliata fallisce in modo
    visibile, e un ripiego verso TCP, in chiaro, sarebbe un peggioramento
    di sicurezza.
    """
    endpoint = parser.add_mutually_exclusive_group()
    endpoint.add_argument(
        "--socket",
        dest="endpoint",
        metavar="PERCORSO",
        type=_socket_endpoint,
        help=f"Percorso del socket Unix di clamd (default: {DEFAULT_SOCKET})",
    )
    endpoint.add_argument(
        "--tcp",
        dest="endpoint",
        metavar="HOST[:PORTA]",
        type=_tcp_endpoint,
        help=(
            "Collegati a clamd via TCP invece che tramite socket Unix (porta "
            "predefinita 3310; per IPv6 [indirizzo]:porta). I contenuti dei "
            "file scansionati viaggiano in chiaro: solo localhost o reti fidate."
        ),
    )
    parser.set_defaults(endpoint=default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klamav-py", description="Scanner ClamAV via clamd")
    # Deliberatamente senza "-V" abbreviato: resta libero per usi futuri
    # e non rischia collisioni con un eventuale "-v" verbose.
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    add_endpoint_arguments(parser, default=ClamdEndpoint())
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
        "--report-only",
        metavar="DIR",
        type=Path,
        action="append",
        default=[],
        help=(
            "Con --quarantine: i file infetti sotto questa directory sono solo "
            "segnalati, non spostati (ripetibile). Si aggiunge agli archivi di "
            "posta già protetti di default."
        ),
    )
    scan.add_argument(
        "--quarantine-all",
        action="store_true",
        help=(
            "Con --quarantine: sposta anche le firme euristiche e i file negli "
            "archivi di posta, che di default sono solo segnalati"
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
    scan.add_argument("--quiet", action="store_true",
        help="Nasconde i file puliti; stampa infezioni, errori e riepilogo")
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


def _prepare_quarantine_dir(raw: Path, scan_root: Path) -> Path | None:
    """
    Valida --quarantine con la stessa regola delle Impostazioni della GUI
    (quarantine_location.decide) e ritorna la directory risolta, o None
    dopo aver stampato il motivo (uscita 2).

    Due differenze volute rispetto alla GUI:
    - un percorso relativo è risolto sulla directory corrente: in una
      shell la cwd è significativa, in una GUI lanciata dal menu no;
    - una directory volatile (/tmp, /var/tmp, /run, /dev) è un errore solo
      sotto systemd (INVOCATION_ID, impostata per ogni servizio): lì
      PrivateTmp la rende privata e la distrugge a fine scansione, file
      infetti compresi. In primo piano /tmp è quella reale e visibile, e
      basta un avviso.
    """
    raw = raw.expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    decision = decide_quarantine_dir(str(raw))
    if decision.error:
        print(f"Quarantena non utilizzabile: {decision.error}", file=sys.stderr)
        return None
    path = decision.path

    if decision.volatile:
        if os.environ.get("INVOCATION_ID"):
            print(f"Quarantena non utilizzabile: {decision.volatile}", file=sys.stderr)
            return None
        print(f"ATTENZIONE: {decision.volatile}", file=sys.stderr)
    for warning in decision.warnings:
        print(f"ATTENZIONE: {warning}", file=sys.stderr)

    # La quarantena è esclusa dall'attraversamento: se contenesse la radice
    # della scansione, l'esclusione la svuoterebbe e l'uscita sarebbe 0
    # senza aver controllato un solo file.
    if scan_root == path or scan_root.is_relative_to(path):
        print(
            f"Quarantena non utilizzabile: «{path}» contiene il percorso da "
            f"scansionare ({scan_root}), che verrebbe escluso per intero.",
            file=sys.stderr,
        )
        return None

    if decision.loose_mode is not None:
        print(
            f"Nota: «{path}» ha permessi {decision.loose_mode:o}, ristretti a 700 "
            "per l'uso come quarantena.",
            file=sys.stderr,
        )
    return path


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
        quarantine_dir = _prepare_quarantine_dir(args.quarantine, scan_root)
        if quarantine_dir is None:
            return 2
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

    client = args.endpoint.new_client()
    # Controllo preliminare: scan_stream NON solleva eccezioni se clamd è
    # irraggiungibile, trasforma il fallimento di connessione in un
    # risultato ERROR per ogni file (giusto per un errore su un singolo
    # file, sbagliato per "clamd spento"). Senza questo PING, clamd giù,
    # socket senza permessi o host TCP sbagliato davano N errori e uscita
    # 0, e la scansione programmata non notificava nulla.
    # clamd che sparisce A SCANSIONE INIZIATA è gestito più sotto
    # (ClamdUnavailable dal client).
    where = args.endpoint.describe()
    try:
        alive = client.ping()
    except (ClamdError, OSError) as exc:
        print(f"clamd non raggiungibile su {where}: {exc}", file=sys.stderr)
        return 2
    if not alive:
        print(f"clamd su {where} non ha risposto correttamente al PING", file=sys.stderr)
        return 2
    # Il costruttore crea la directory: un errore qui (permessi, symlink,
    # directory di altri) usciva come traceback con codice 1, cioè
    # "infezioni trovate", e OnFailure notificava la cosa sbagliata.
    try:
        quarantine = Quarantine(quarantine_dir) if quarantine_dir else None
    except (OSError, QuarantineError) as exc:
        print(f"Quarantena non utilizzabile: {exc}", file=sys.stderr)
        return 2
    policy = QuarantinePolicy(
        default_report_only_dirs() + [p.expanduser() for p in args.report_only],
        enabled=not args.quarantine_all,
    )

    scanned = 0
    infections = 0
    errors = 0
    too_large = 0
    report_only = 0
    error_categories: Counter[str] = Counter()
    # Il log degli errori elenca percorsi di file dell'utente: 0600, niente
    # symlink, niente file preesistenti di altri utenti (es. pre-creato in
    # /tmp per leggerne il contenuto). Vedi private_files.py.
    log_fh = None
    if args.log_errors:
        try:
            log_fh = open_private_for_write(args.log_errors.expanduser())
        except OSError as exc:
            print(f"Impossibile aprire il log degli errori: {exc}", file=sys.stderr)
            return 2

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
                    decision = policy.decide(Path(result.path), result.signature)
                    if not decision.quarantine:
                        report_only += 1
                        print(f"  -> NON messo in quarantena ({decision.reason})")
                    else:
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
    except ClamdUnavailable as exc:
        # clamd sparito a scansione iniziata (i riavvii brevi sono già
        # assorbiti dai nuovi tentativi del client): quanto scansionato
        # finora è stato stampato, ma il risultato non vale come "pulito".
        print(
            f"\nclamd non più raggiungibile su {exc.where} dopo {scanned} file: {exc}. "
            "Scansione incompleta.",
            file=sys.stderr,
        )
        return 2
    except ClamdError as exc:
        print(
            f"Errore di comunicazione con clamd ({args.endpoint.describe()}): {exc}",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        # Tutto OSError, non solo ConnectionRefused/FileNotFound: permessi
        # negati sul socket (utente fuori dal gruppo clamav), host TCP non
        # risolvibile, rete irraggiungibile, timeout. Prima questi casi
        # uscivano come traceback con codice 1, cioè "infezioni trovate".
        # Gli errori di lettura dei singoli file non arrivano qui: il
        # client li trasforma in risultati ERROR.
        print(f"clamd non raggiungibile su {args.endpoint.describe()}: {exc}", file=sys.stderr)
        return 2
    finally:
        if log_fh:
            log_fh.close()

    print(f"\n{scanned} file scansionati, {infections} infetti, {errors} errori.")
    if too_large:
        print(f"{too_large} file oltre StreamMaxLength, non verificati (vedi clamd.conf).")
    if report_only:
        print(f"{report_only} infetti solo segnalati e non spostati: verificali manualmente.")

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
    where = args.endpoint.describe()
    try:
        alive = args.endpoint.new_client().ping()
    except (ClamdError, OSError) as exc:
        print(f"clamd non raggiungibile su {where}: {exc}", file=sys.stderr)
        return 2
    print(f"clamd attivo su {where}" if alive else f"clamd su {where} non ha risposto correttamente")
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
