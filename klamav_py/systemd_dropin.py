"""
Drop-in utente per klamav-scan.service: porta nella scansione programmata
la directory di quarantena scelta nelle Impostazioni.

La unit spedita ha il percorso predefinito nell'ExecStart. Quando
l'utente sceglie un'altra directory, la GUI scrive

    ~/.config/systemd/user/klamav-scan.service.d/50-klamav-py.conf

e lo rimuove quando tutto torna ai valori predefiniti. Lo stesso vale per la
connessione a clamd: un socket diverso da quello predefinito diventa
--socket nell'ExecStart, TCP diventa --tcp più le due righe che consentono
la rete (PrivateNetwork=no, RestrictAddressFamilies con AF_INET/AF_INET6).
Un solo file, generato dallo stato completo: con più drop-in, ognuno
azzererebbe ExecStart= e vincerebbe l'ultimo in ordine alfabetico, perdendo
in silenzio gli altri.

Tre insidie che il modulo chiude, ognuna con i suoi test:

- ExecStart= vuoto prima della riga nuova: su Type=oneshot le ExecStart si
  SOMMANO, e senza azzeramento la scansione partirebbe due volte.
- Quoting: ExecStart non passa da una shell. systemd fa il suo word
  splitting, espande gli specificatori (%) e le variabili ($): un percorso
  con spazi, "%" o "$" va quotato con le regole di systemd, non di sh.
- Pulizia: il file si riconosce dall'intestazione e si cancella solo se è
  nostro; un file con lo stesso nome scritto a mano non viene mai toccato.

Niente Qt: lo usa la GUI, e i test girano senza PySide6.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .clamd_client import DEFAULT_SOCKET, ClamdEndpoint
from .private_files import ensure_private_dir, write_private_text
from .quarantine_location import DEFAULT_QUARANTINE_SUBDIR

UNIT_NAME = "klamav-scan.service"
TIMER_NAME = "klamav-scan.timer"
DROPIN_NAME = "50-klamav-py.conf"
HEADER = "# Generato da KlamAV-Py: non modificare a mano."

# Comando della unit spedita, con la quarantena come segnaposto: la
# coerenza con l'ExecStart di debian/klamav-py.klamav-scan.user.service è
# verificata dai test, quindi un cambio nella unit non può passare
# inosservato qui.
EXEC_BINARY = "/usr/bin/klamav-py"
EXEC_TEMPLATE = EXEC_BINARY + " {options}scan %h --quarantine {quarantine} --quiet"

# Percorso assoluto, niente PATH: stesso principio di trusted_binary() in
# freshclam_service.py.
SYSTEMCTL = "/usr/bin/systemctl"

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class DropinConflict(Exception):
    """Esiste un file con il nostro nome che non abbiamo scritto noi."""


# -- quoting -------------------------------------------------------------

def _check(value: str) -> None:
    if _CONTROL_CHARS.search(value):
        # Un a capo chiuderebbe la riga del drop-in e ne aprirebbe un'altra:
        # iniezione di direttive. quarantine_location lo rifiuta già, qui è
        # la seconda linea di difesa.
        raise ValueError("caratteri di controllo non ammessi in un valore systemd")


def quote_exec_arg(value: str) -> str:
    """
    Un argomento di ExecStart=, tra doppi apici.

    Backslash e apici escapati per il word splitting di systemd, "%"
    raddoppiato contro gli specificatori, "$" raddoppiato contro
    l'espansione delle variabili d'ambiente. Gli escape non si
    sovrappongono, quindi l'ordine in cui systemd li applica è irrilevante.
    """
    _check(value)
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    return f'"{escaped}"'


def quote_path_value(value: str) -> str:
    """
    Un percorso per ReadWritePaths= e simili: come quote_exec_arg() ma senza
    "$", perché qui systemd espande gli specificatori ma non le variabili.
    """
    _check(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


# Sottoinsieme delle regole di systemd sufficiente a rileggere ciò che
# scriviamo (e l'ExecStart della unit spedita). Serve ai test e come
# verifica di sé stesso: split_exec(quote_exec_arg(s)) == [s].
_CUNESCAPE = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r", "s": " "}


def _specifiers(value: str, home: str | None) -> str:
    def repl(m: re.Match) -> str:
        code = m.group(1)
        if code == "%":
            return "%"
        if code == "h" and home is not None:
            return home
        raise ValueError(f"specificatore %{code} non gestito")
    return re.sub(r"%(.)", repl, value)


def _split_words(value: str) -> list[str]:
    words: list[str] = []
    i, n = 0, len(value)
    while i < n:
        while i < n and value[i] in " \t":
            i += 1
        if i >= n:
            break
        buf: list[str] = []
        while i < n and value[i] not in " \t":
            c = value[i]
            if c in "\"'":
                i += 1
                while i < n and value[i] != c:
                    if value[i] == "\\":
                        if i + 1 >= n or value[i + 1] not in _CUNESCAPE:
                            raise ValueError(f"escape non valido in {value!r}")
                        buf.append(_CUNESCAPE[value[i + 1]])
                        i += 2
                    else:
                        buf.append(value[i])
                        i += 1
                if i >= n:
                    raise ValueError(f"virgolette non chiuse in {value!r}")
                i += 1
            elif c == "\\":
                if i + 1 >= n or value[i + 1] not in _CUNESCAPE:
                    raise ValueError(f"escape non valido in {value!r}")
                buf.append(_CUNESCAPE[value[i + 1]])
                i += 2
            else:
                buf.append(c)
                i += 1
        words.append("".join(buf))
    return words


def _variables(word: str) -> str:
    def repl(m: re.Match) -> str:
        if m.group(0) == "$$":
            return "$"
        raise ValueError(f"variabile {m.group(0)} non gestita")
    return re.sub(r"\$\$|\$\{?\w+\}?", repl, word)


def split_exec(value: str, home: str | None = None) -> list[str]:
    """argv che systemd ricava da un valore di ExecStart= (%h espanso con home)."""
    return [_variables(w) for w in _split_words(_specifiers(value, home))]


def split_paths(value: str, home: str | None = None) -> list[str]:
    """Percorsi che systemd ricava da un valore di ReadWritePaths=."""
    return [_specifiers(w, home) for w in _split_words(value)]


# -- generazione ---------------------------------------------------------

def _endpoint_options(endpoint: ClamdEndpoint | None) -> str:
    """Opzioni globali della CLI per l'endpoint; "" per quello predefinito."""
    if endpoint is None:
        return ""
    if endpoint.is_tcp:
        return f"--tcp {quote_exec_arg(endpoint.to_cli())} "
    if endpoint.unix_socket != DEFAULT_SOCKET:
        return f"--socket {quote_exec_arg(endpoint.unix_socket)} "
    return ""


def render_dropin(
    quarantine: Path | None,
    *,
    home: Path,
    endpoint: ClamdEndpoint | None = None,
) -> str | None:
    """
    Testo del drop-in per la directory di quarantena già validata e
    risolta (quarantine_location.decide()) e per l'endpoint di clamd delle
    Impostazioni. None quando non serve un drop-in: entrambi predefiniti.

    ReadWritePaths solo per le directory fuori dalla home: la unit ha già
    ReadWritePaths=%h, e allargare il sandbox quando non serve non ha senso.
    Il percorso è quello risolto al salvataggio: se in seguito un symlink
    viene ripuntato, il sandbox non lo segue finché non si salva di nuovo.

    La rete si apre solo con TCP, e sovrascrivendo (non togliendo) le due
    direttive della unit: RestrictAddressFamilies resta un filtro, esteso
    a AF_INET/AF_INET6, invece di tornare al default senza filtri.
    """
    home = home.resolve()
    default_quarantine = (home / DEFAULT_QUARANTINE_SUBDIR).resolve()
    options = _endpoint_options(endpoint)
    if (quarantine is None or quarantine == default_quarantine) and not options:
        return None
    quarantine = quarantine or default_quarantine
    if not quarantine.is_absolute():
        raise ValueError("la directory di quarantena deve essere un percorso assoluto e risolto")

    lines = [
        HEADER,
        "# Scritto e rimosso dalle Impostazioni di klamav-py-gui; vedi klamav-py-gui(1).",
        "",
        "[Service]",
        "# Azzeramento obbligatorio: su Type=oneshot le ExecStart si sommano.",
        "ExecStart=",
        "ExecStart="
        + EXEC_TEMPLATE.format(options=options, quarantine=quote_exec_arg(str(quarantine))),
    ]
    if not (quarantine == home or quarantine.is_relative_to(home)):
        lines.append("ReadWritePaths=" + quote_path_value(str(quarantine)))
    if endpoint is not None and endpoint.is_tcp:
        lines += [
            "# clamd via TCP: rete consentita solo per questo (vedi klamav-py(1), SICUREZZA).",
            "PrivateNetwork=no",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        ]
    return "\n".join(lines) + "\n"


# -- file system ---------------------------------------------------------

def dropin_path(config_home: Path | None = None) -> Path:
    base = config_home
    if base is None:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".config"
    return base / "systemd" / "user" / f"{UNIT_NAME}.d" / DROPIN_NAME


def is_ours(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readline().rstrip("\n") == HEADER
    except FileNotFoundError:
        return False


def sync_dropin(text: str | None, path: Path | None = None) -> bool:
    """
    Porta il drop-in allo stato voluto: scritto (atomico, 0600) se text non
    è None, rimosso altrimenti. Ritorna True se il file è cambiato.

    Solleva DropinConflict se al suo posto c'è un file non nostro, OSError
    per gli errori di scrittura: in entrambi i casi il chiamante non deve
    salvare le Impostazioni, o GUI e timer userebbero due quarantene diverse.
    """
    path = path or dropin_path()
    exists = path.exists() or path.is_symlink()
    if exists and not is_ours(path):
        raise DropinConflict(
            f"{path} esiste ma non è stato scritto da KlamAV-Py: rimuovilo o "
            "rinominalo, poi salva di nuovo le Impostazioni."
        )

    if text is None:
        if not exists:
            return False
        path.unlink()
        try:
            path.parent.rmdir()  # solo se vuota: altri drop-in restano
        except OSError:
            pass
        return True

    if exists and path.read_text(encoding="utf-8") == text:
        return False
    ensure_private_dir(path.parent)
    write_private_text(path, text)
    return True


# -- systemd utente ------------------------------------------------------

def _systemctl_user(*args: str, timeout: float = 10) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [SYSTEMCTL, "--user", *args],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def daemon_reload() -> str | None:
    """
    systemctl --user daemon-reload. Ritorna None se riuscito, altrimenti il
    motivo, da mostrare come AVVISO: senza manager utente (sessione SSH,
    sistema senza systemd) il drop-in viene letto al prossimo avvio del
    manager, e il salvataggio delle Impostazioni resta valido.
    """
    result = _systemctl_user("daemon-reload")
    if result is None:
        return "systemctl non disponibile"
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"uscita {result.returncode}"
    return None


def disable_timer() -> str | None:
    """
    systemctl --user disable --now klamav-scan.timer, per passare alla
    pianificazione interna della GUI (le due sono alternative). Ritorna
    None se riuscito, altrimenti il motivo.
    """
    result = _systemctl_user("disable", "--now", TIMER_NAME)
    if result is None:
        return "systemctl non disponibile"
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or f"uscita {result.returncode}"
    return None


def timer_enabled() -> bool | None:
    """
    True/False se klamav-scan.timer è abilitato o no, None se non si può
    sapere (niente manager utente): in quel caso la GUI non blocca nulla.
    """
    result = _systemctl_user("is-enabled", TIMER_NAME)
    if result is None:
        return None
    state = result.stdout.strip()
    if state in ("enabled", "enabled-runtime"):
        return True
    if state in ("disabled", "masked", "masked-runtime", "static", "indirect", "linked", "linked-runtime"):
        return False
    return None


# -- drop-in di altri ----------------------------------------------------
#
# Altri drop-in della stessa unit (tipicamente override.conf, creato da
# "systemctl --user edit") possono rendere inefficace il nostro senza che
# nulla lo segnali: un ExecStart= applicato DOPO il nostro lo sostituisce,
# e con esso quarantena e connessione impostate nella GUI; uno applicato
# PRIMA viene sostituito dal nostro, e sono le personalizzazioni di
# quel file a sparire. Con TCP conta anche PrivateNetwork: un "true"
# applicato dopo il nostro "no" richiude la rete.
#
# Il rilevamento avvisa e non blocca: la personalizzazione è dell'utente
# ed è intenzionale, ma non deve restare silenziosa.

# Directory dei drop-in utente in ordine di priorità: a parità di nome
# vince la prima (come systemd, che applica i drop-in in ordine di nome
# del file indipendentemente dalla directory).
_DROPIN_DIRS = (
    Path("~/.config/systemd/user"),
    Path("/etc/systemd/user"),
    Path("/usr/lib/systemd/user"),
)


class ForeignOverride:
    """Un drop-in non nostro che imposta direttive che usiamo anche noi."""

    def __init__(self, path: Path, keys: tuple[str, ...], after: bool, ours_active: bool) -> None:
        self.path = path
        self.keys = keys
        self.after = after  # applicato dopo il nostro drop-in
        self.ours_active = ours_active  # il nostro drop-in esiste

    def __repr__(self) -> str:
        return f"ForeignOverride({self.path}, {self.keys}, after={self.after})"

    def describe(self) -> str:
        wins = self.after or not self.ours_active
        effects = []
        if "ExecStart" in self.keys:
            effects.append(
                "la scansione programmata usa il comando di quel file, non la cartella "
                "di quarantena e la connessione a clamd impostate in KlamAV-Py"
                if wins
                else "le Impostazioni di KlamAV-Py ne sostituiscono il comando "
                "(ExecStart), quindi le personalizzazioni di quel comando non vengono "
                "applicate"
            )
        if "PrivateNetwork" in self.keys and wins:
            effects.append(
                "la sua impostazione PrivateNetwork impedisce alla scansione "
                "programmata di raggiungere clamd via TCP"
            )
        return f"«{self.path}»: " + "; ".join(effects) + "."


def dropin_paths() -> list[Path] | None:
    """
    Drop-in di klamav-scan.service nell'ordine in cui systemd li applica,
    da "systemctl --user show -p DropInPaths". None se il manager utente
    non risponde (sessione SSH, sistema senza systemd): il chiamante usa
    allora la lettura diretta delle directory.
    """
    result = _systemctl_user("show", "-p", "DropInPaths", "--value", UNIT_NAME)
    if result is None or result.returncode != 0:
        return None
    return [Path(p) for p in result.stdout.split()]


def _scan_dropin_dirs(dirs=_DROPIN_DIRS) -> list[Path]:
    by_name: dict[str, Path] = {}
    for base in dirs:
        d = base.expanduser() / f"{UNIT_NAME}.d"
        try:
            files = sorted(d.glob("*.conf"))
        except OSError:
            continue
        for f in files:
            by_name.setdefault(f.name, f)  # la directory più prioritaria vince
    return [by_name[name] for name in sorted(by_name)]


def _service_keys(path: Path) -> set[str]:
    """Direttive assegnate nella sezione [Service] di un drop-in."""
    keys: set[str] = set()
    in_service = False
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return keys
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        if in_service and "=" in line:
            keys.add(line.split("=", 1)[0].strip())
    return keys


def foreign_overrides(
    *,
    tcp: bool,
    ours: Path | None = None,
    paths: list[Path] | None = None,
) -> list[ForeignOverride]:
    """
    Drop-in non nostri che impostano ExecStart (sempre) o PrivateNetwork
    (solo con TCP, dove il nostro "no" deve restare l'ultimo). paths è
    iniettabile per i test; di default DropInPaths, o le directory lette
    direttamente se il manager utente non risponde.
    """
    ours = ours or dropin_path()
    if paths is None:
        paths = dropin_paths()
    if paths is None:
        paths = _scan_dropin_dirs()

    watched = {"ExecStart"} | ({"PrivateNetwork"} if tcp else set())
    ours_active = is_ours(ours)
    found = []
    for p in paths:
        if p.name == ours.name and (p == ours or is_ours(p)):
            continue
        keys = _service_keys(p) & watched
        after = p.name > ours.name
        if "PrivateNetwork" in keys and not (after or not ours_active):
            # Applicato prima del nostro "no": non ha effetto, niente avviso.
            keys.discard("PrivateNetwork")
        if keys:
            # systemd applica i drop-in per nome del file, qualunque sia
            # la directory: il confronto sul nome dice chi viene dopo.
            found.append(ForeignOverride(p, tuple(sorted(keys)), after, ours_active))
    return found
