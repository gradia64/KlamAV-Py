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
aggiunge directory, --quarantine-all disattiva la regola. Valgono anche
fra le opzioni globali, per --acknowledge: la regola si costruisce in un
punto solo (build_policy), e la presa visione va chiesta con le stesse
opzioni della scansione.

Segnalazioni già valutate: un file che la policy lascia al suo posto e di
cui l'utente ha registrato la presa visione (--acknowledge, vedi
acknowledged.py) non conta fra gli infetti. Resta una riga «GIÀ
VALUTATO» a ogni scansione; se il contenuto cambia torna a essere
segnalato.

Codici di uscita, in ordine di precedenza — utili per `OnFailure=` in
systemd o per script di monitoraggio:
  2 = errore di esecuzione (clamd irraggiungibile, percorso inesistente,
      collegamento rotto, né directory né file regolare, non leggibile,
      ecc.): la scansione non parte o si interrompe;
  1 = infezioni trovate, anche se ci sono stati guasti di I/O;
  2 = guasti di I/O senza infezioni: errori di lettura di file o
      sottocartelle che non sono permessi né entry sparite (EIO, ESTALE,
      ETIMEDOUT di un disco o di un mount guasto, vedi
      clamd_client.is_io_fault). Parte dell'albero non è stata controllata
      e la scansione non vale come pulita;
  0 = altrimenti.

Gli errori per permessi, i file spariti durante la scansione e gli errori
di comunicazione con clamd su un singolo file non cambiano il codice. Una
sottocartella con permessi negati ha una riga su stderr (e nel log degli
errori) e un contatore a parte nel riepilogo, con il suggerimento di
--exclude; una sottocartella guasta è un errore come quelli dei file.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
import time
from collections import Counter
from pathlib import Path

from . import __version__
from .acknowledged import (
    HASH_PREFIX_LEN,
    AckRegistry,
    RegistryError,
    ScanAcknowledgements,
    SCOPE_NOTE,
    hash_file,
    recovery_notice,
)
from .clamd_client import (
    DEFAULT_MAX_STREAM_SIZE,
    DEFAULT_SOCKET,
    ClamdEndpoint,
    ClamdError,
    ClamdUnavailable,
    UnreadableRoot,
    root_problem,
    unreadable_dir_line,
)
from .private_files import open_private_for_write
from .quarantine import Quarantine, QuarantineError
from .quarantine_location import decide as decide_quarantine_dir
from .quarantine_policy import QuarantinePolicy, default_report_only_dirs
from .scan_exclusions import decide as decide_exclusion


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
    parser.add_argument(
        "--acknowledge",
        metavar="PERCORSO",
        type=Path,
        action="append",
        default=[],
        help=(
            "Registra la presa visione di un file segnalato ma non spostato "
            "(firma euristica o archivio di posta), dopo averlo verificato: le "
            "scansioni successive non lo contano più fra gli infetti finché il "
            "contenuto non cambia. Il file viene riscansionato: pulito o da "
            "quarantena, niente registrazione. Ripetibile"
        ),
    )
    parser.add_argument(
        "--list-acknowledged",
        action="store_true",
        help="Elenca le prese visione registrate, con il prefisso dell'hash da usare per revocarle",
    )
    parser.add_argument(
        "--unacknowledge",
        metavar="VALORE",
        action="append",
        default=[],
        help=(
            "Revoca una presa visione: VALORE è il percorso del file o il prefisso "
            "dell'hash mostrato da --list-acknowledged. Ripetibile"
        ),
    )
    # Regola «solo segnalazione» anche fra le opzioni globali: --acknowledge
    # non ha un comando, e deve poter ricostruire la stessa policy della
    # scansione (vedi build_policy). Con scan valgono in entrambe le
    # posizioni e si sommano; dest diversi, perché i valori del
    # sottocomando sostituirebbero quelli globali invece di aggiungersi.
    parser.add_argument(
        "--report-only",
        dest="report_only_global",
        metavar="DIR",
        type=Path,
        action="append",
        default=[],
        help=(
            "Come l'opzione di scan, per --acknowledge: passa le stesse cartelle "
            "usate in scansione, perché la presa visione valga per i file sotto di "
            "esse (ripetibile)"
        ),
    )
    parser.add_argument(
        "--quarantine-all",
        dest="quarantine_all_global",
        action="store_true",
        help="Come l'opzione di scan, per --acknowledge: nessun rilevamento è solo segnalato",
    )
    # Non obbligatorio: le opzioni delle prese visione funzionano senza
    # comando. main() chiede un comando se manca anche quelle.
    sub = parser.add_subparsers(dest="command")

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
            "escluse non vengono nemmeno lette. Errore se non è una "
            "directory o se contiene il percorso da scansionare. La "
            "directory di quarantena (--quarantine) è esclusa "
            "automaticamente."
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


def build_policy(args: argparse.Namespace) -> QuarantinePolicy:
    """
    La regola «solo segnalazione» dagli argomenti: cartelle predefinite più
    le --report-only (globali e di scan), disattivata da --quarantine-all.
    Unica costruzione per scan e --acknowledge: prima --acknowledge usava
    le sole cartelle predefinite, e un file sotto una --report-only era
    «solo segnalazione» in scansione ma rifiutato come «da quarantena»
    dalla presa visione.
    """
    report_only = [*args.report_only_global, *getattr(args, "report_only", [])]
    quarantine_all = args.quarantine_all_global or getattr(args, "quarantine_all", False)
    return QuarantinePolicy(
        default_report_only_dirs() + [p.expanduser() for p in report_only],
        enabled=not quarantine_all,
    )


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


def _prepare_exclusions(raws: list[Path], scan_root: Path) -> list[Path] | None:
    """
    Valida --exclude con la stessa regola delle Impostazioni della GUI
    (scan_exclusions.decide) e ritorna le directory risolte, o None dopo
    aver stampato il motivo (uscita 2).

    Come per --quarantine, un percorso relativo è risolto sulla directory
    corrente. Con un file come percorso da scansionare non c'è radice da
    confrontare: _iter_files restituisce il file senza guardare le
    esclusioni, quindi restano solo i controlli sul formato.
    """
    roots = {"percorso da scansionare": scan_root} if scan_root.is_dir() else {}
    dirs: list[Path] = []
    for raw in raws:
        decision = decide_exclusion(str(raw), roots=roots, cwd=Path.cwd())
        if decision.error:
            print(f"Esclusione non utilizzabile: {decision.error}", file=sys.stderr)
            return None
        for warning in decision.warnings:
            print(f"ATTENZIONE: {warning}", file=sys.stderr)
        dirs.append(decision.path)
    return dirs


def cmd_scan(args: argparse.Namespace) -> int:
    scan_root_input = args.path.expanduser()
    # Prima della traversata e della connessione a clamd, e prima di
    # resolve(), che renderebbe un symlink rotto un percorso inesistente
    # qualunque: os.walk non percorrerebbe una radice illeggibile, e
    # l'uscita sarebbe 0 senza aver controllato nulla. Vedi
    # clamd_client.root_problem.
    problem = root_problem(scan_root_input)
    if problem:
        print(f"Percorso {problem}", file=sys.stderr)
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
    exclude_dirs = _prepare_exclusions(args.exclude, scan_root)
    if exclude_dirs is None:
        return 2
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
    policy = build_policy(args)

    scanned = 0
    infections = 0
    errors = 0
    # Errori che sono guasti di I/O (ScanResult.io_fault): contati anche fra
    # gli errori, decidono l'uscita 2 senza rilevamenti.
    io_faults = 0
    too_large = 0
    report_only = 0
    unreadable_dirs = 0
    # Segnalazioni nuove per cui si può registrare la presa visione, e
    # quelle già valutate (non contate fra gli infetti).
    acknowledgeable = 0
    acknowledged = 0
    acks = ScanAcknowledgements()
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

    def on_unreadable_dir(path: Path, exc: OSError) -> None:
        # Non è un errore di scansione (vedi clamd_client._iter_files): le
        # EACCES permanenti (bind mount di Docker, cartelle create con sudo)
        # darebbero sempre gli stessi errori e nasconderebbero quelli veri.
        nonlocal unreadable_dirs
        unreadable_dirs += 1
        reason = exc.strerror or str(exc)
        print(unreadable_dir_line(path, reason), file=sys.stderr)
        if log_fh:
            log_fh.write(f"{path}\tcartella non leggibile: {reason}\n")

    try:
        for result in client.scan_stream(
            scan_root,
            persistent=not args.no_persistent,
            session_batch_size=args.session_batch_size,
            exclude_dirs=exclude_dirs,
            max_stream_size=max_stream_size,
            on_unreadable_dir=on_unreadable_dir,
        ):
            scanned += 1
            if result.infected:
                # La policy si applica anche senza --quarantine: decide
                # quali rilevamenti possono avere una presa visione. Il
                # registro si consulta solo dopo il suo «solo segnalazione».
                decision = policy.decide(Path(result.path), result.signature)
                if not decision.quarantine:
                    identity = acks.identify(Path(result.path))
                    if acks.already_evaluated(identity, result.signature):
                        acknowledged += 1
                        # Anche con --quiet: la riga resta nel journal a ogni
                        # scansione.
                        print(f"GIÀ VALUTATO: {result.path} ({result.signature})")
                        continue
                    acknowledgeable += 1
                infections += 1
                print(f"INFETTO: {result.path} ({result.signature})")
                if quarantine:
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
                if result.io_fault:
                    io_faults += 1
                error_categories[_error_category(result.signature or "")] += 1
                print(f"ERRORE su {result.path}: {result.signature}", file=sys.stderr)
                if log_fh:
                    log_fh.write(f"{result.path}\t{result.signature}\n")
            elif not args.quiet:
                print(f"OK: {result.path}")
    except UnreadableRoot as exc:
        # Permessi cambiati fra la sonda iniziale e la traversata.
        print(f"Percorso {exc}", file=sys.stderr)
        return 2
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
        acks.finish()
        for problem in acks.problems:
            print(f"ATTENZIONE: {problem}", file=sys.stderr)

    print(f"\n{scanned} file scansionati, {infections} infetti, {errors} errori.")
    if too_large:
        print(f"{too_large} file oltre StreamMaxLength, non verificati (vedi clamd.conf).")
    if unreadable_dirs:
        print(
            f"{unreadable_dirs} cartelle non leggibili, non controllate (elencate sopra): "
            "se sono attese, escludile con --exclude."
        )
    if report_only:
        print(f"{report_only} infetti solo segnalati e non spostati.")
    if acknowledgeable:
        print(
            f"Dopo averli verificati: eliminali, oppure registra la presa visione "
            f"con «klamav-py --acknowledge PERCORSO» ({acknowledgeable} "
            "segnalazioni) per non contarli più finché il contenuto non cambia."
        )
    if acknowledged:
        print(f"{acknowledged} segnalazioni già valutate (--list-acknowledged per l'elenco).")

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

    if io_faults:
        # Ultima riga, anche quando l'uscita è 1 per i rilevamenti: un
        # guasto non deve sparire dietro un'infezione.
        print(
            f"\nATTENZIONE: {io_faults} guasti di I/O (elencati sopra fra gli errori): "
            "parte dell'albero non è stata controllata, la scansione non vale come pulita."
        )

    if infections:
        return 1
    if io_faults:
        return 2
    return 0


def _acknowledge_one(raw: Path, client, policy: QuarantinePolicy, registry: AckRegistry) -> bool:
    """Riscansiona `raw` e registra la presa visione solo se è infetto con
    esito «solo segnalazione». Così la presa visione verifica lo stato
    attuale e non si fida di un percorso fornito a mano. False, con il
    motivo su stderr, se non registra nulla."""
    path = raw.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path

    def refuse(reason: str) -> bool:
        print(f"{raw}: {reason}. Nessuna presa visione registrata.", file=sys.stderr)
        return False

    try:
        st = os.lstat(path)
    except OSError as exc:
        return refuse(f"non accessibile ({exc.strerror or exc})")
    if stat.S_ISLNK(st.st_mode):
        return refuse("è un collegamento simbolico; indica il file vero")
    if not stat.S_ISREG(st.st_mode):
        return refuse("non è un file regolare")
    try:
        before = hash_file(path)
        results = list(client.scan_stream(path, persistent=False))
        after = hash_file(path)
    except (ClamdError, OSError) as exc:
        return refuse(f"verifica non riuscita ({exc})")
    if len(results) != 1:
        return refuse("verifica non riuscita (nessun esito da clamd)")
    (result,) = results
    if result.too_large:
        return refuse("oltre StreamMaxLength, clamd non l'ha verificato")
    if result.status == "ERROR":
        return refuse(f"verifica non riuscita ({result.signature})")
    if not result.infected:
        return refuse("clamd non lo rileva come infetto")
    decision = policy.decide(path, result.signature)
    if decision.quarantine:
        if not policy.enabled:
            return refuse(
                f"rilevamento da quarantena ({result.signature}): con --quarantine-all "
                "nessun rilevamento è solo segnalato, quindi la presa visione non si applica"
            )
        return refuse(
            f"rilevamento da quarantena ({result.signature}): la presa visione vale solo "
            "per i rilevamenti che non vengono spostati (firme euristiche, archivi di "
            "posta, cartelle --report-only). Se la scansione usa --report-only, passa "
            "le stesse cartelle anche a --acknowledge"
        )
    if before != after:
        return refuse("il file è cambiato durante la verifica")
    try:
        entry = registry.acknowledge(after.sha256, result.signature, path)
    except (RegistryError, OSError) as exc:
        return refuse(f"registro non scrivibile ({exc})")
    print(f"Presa visione registrata: {entry.sha256[:HASH_PREFIX_LEN]}  {entry.signature}  {path}")
    return True


def _unacknowledge_one(value: str, registry: AckRegistry) -> bool:
    path = Path(value).expanduser()
    try:
        if os.path.lexists(path):
            if path.is_symlink():
                raise RegistryError(f"{value} è un collegamento simbolico; indica il file vero")
            try:
                sha = hash_file(path).sha256
            except OSError as exc:
                raise RegistryError(f"impossibile leggere {value}: {exc}") from exc
            found = registry.match_hash(sha)
            if not found:
                raise RegistryError(f"nessuna presa visione per il contenuto attuale di {value}")
        else:
            found = registry.match_prefix(value)
        registry.revoke(e.key for e in found)
    except (RegistryError, OSError) as exc:
        print(f"Revoca non eseguita: {exc}", file=sys.stderr)
        return False
    for e in found:
        print(f"Presa visione revocata: {e.sha256[:HASH_PREFIX_LEN]}  {e.signature}  {e.first_path}")
    return True


def _list_acknowledged(registry: AckRegistry) -> bool:
    try:
        entries = registry.entries()
    except OSError as exc:
        print(f"Registro delle prese visione non leggibile: {exc}", file=sys.stderr)
        return False
    if not entries:
        print("Nessuna presa visione registrata.")
        return True
    for e in entries:
        print(
            f"{e.sha256[:HASH_PREFIX_LEN]}  {e.signature}  {e.first_path}  "
            f"presa visione {time.strftime('%Y-%m-%d', time.localtime(e.acknowledged_at))}, "
            f"ultimo riscontro {time.strftime('%Y-%m-%d', time.localtime(e.last_seen))}"
        )
    return True


def cmd_acknowledgements(args: argparse.Namespace) -> int:
    """--acknowledge, --unacknowledge e --list-acknowledged, in quest'ordine.
    Uscita 0 se tutto è andato, 2 se almeno un valore è stato rifiutato:
    in quel caso quel valore non ha toccato il registro.

    Un registro non valido trovato da una qualunque delle tre operazioni è
    messo da parte (.corrupt-*) con un avviso su stderr, una volta sola.
    Non cambia il codice di uscita: l'operazione chiesta è riuscita sul
    registro nuovo, come per --list-acknowledged e per le scansioni, che
    escono per i rilevamenti e non per lo stato del registro."""
    registry = AckRegistry()
    ok = True
    if args.acknowledge:
        client = args.endpoint.new_client()
        policy = build_policy(args)
        recorded = False
        for raw in args.acknowledge:
            done = _acknowledge_one(raw, client, policy, registry)
            recorded = recorded or done
            ok = done and ok
        if recorded:
            print(SCOPE_NOTE)
    for value in args.unacknowledge:
        ok = _unacknowledge_one(value, registry) and ok
    if args.list_acknowledged:
        ok = _list_acknowledged(registry) and ok
    if registry.last_recovery is not None:
        print(f"ATTENZIONE: {recovery_notice(*registry.last_recovery)}", file=sys.stderr)
    return 0 if ok else 2


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
    acks = args.acknowledge or args.unacknowledge or args.list_acknowledged
    if acks and args.command is not None:
        parser.error("le opzioni delle prese visione non si combinano con un comando")
    if acks:
        return cmd_acknowledgements(args)
    if args.command is None:
        parser.error("serve un comando (scan o ping) o un'opzione delle prese visione")
    if args.command == "scan":
        return cmd_scan(args)
    if args.command == "ping":
        if args.report_only_global or args.quarantine_all_global:
            parser.error("--report-only e --quarantine-all valgono per scan e --acknowledge")
        return cmd_ping(args)
    parser.error("comando sconosciuto")
    return 2


if __name__ == "__main__":
    sys.exit(main())
