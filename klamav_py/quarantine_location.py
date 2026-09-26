"""
Validazione della directory di quarantena scelta dall'utente.

Unica fonte della regola, usata dal salvataggio delle Impostazioni, dal
generatore del drop-in della unit klamav-scan.service e dalla CLI
(--quarantine): le stringhe di motivo sono condivise, quindi la regola non
può divergere fra i consumatori. Come quarantine_policy.py, il modulo non
importa nulla di Qt.

Perché serve (vedi anche klamav-py(1), SICUREZZA): la unit di scansione ha
PrivateTmp=true. Una quarantena in /var/tmp finirebbe nella /var/tmp
privata del servizio, che systemd elimina a fine scansione insieme
all'indice: file infetti distrutti senza errori né notifiche. Il 226/NAMESPACE
non scatta, perché la directory nel namespace esiste davvero. La regola
quindi rifiuta le directory volatili prima che arrivino alla unit.

Tre livelli di esito, perché i consumatori li trattano diversamente:

- error: sempre bloccante (percorso relativo, caratteri di controllo,
  non una directory, directory di un altro utente);
- volatile: bloccante per le Impostazioni e per la CLI eseguita da systemd,
  solo avviso per la CLI in primo piano (lì /tmp è quella reale e visibile);
- warnings: mai bloccanti (tmpfs o ramfs montati altrove: persi al riavvio,
  ma possono essere una scelta consapevole).

Ordine della pipeline, non intercambiabile: expanduser() prima di tutto
(Path("~/x").resolve() darebbe <cwd>/~/x), rifiuto dei relativi invece di
risolverli sulla cwd (arbitraria per una GUI lanciata da Dolphin o dal
menu), resolve(), e solo dopo tutti i confronti: così /var/run finisce su
/run e un symlink sotto ~ che punta fuori viene giudicato per la sua
destinazione reale.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Stesso percorso dell'ExecStart di klamav-scan.service (%h/...): la
# coerenza è verificata da tests/test_quarantine_location.py.
DEFAULT_QUARANTINE_SUBDIR = Path(".local/share/klamav-py/quarantine")

UNIT_PATH = Path("/usr/lib/systemd/user/klamav-scan.service")

# Directory volatili rifiutate sempre, indipendentemente dalla unit:
# /tmp e /var/tmp (PrivateTmp, e comunque ripulite), /run (tmpfs intero:
# copre /run/user/UID, cioè $XDG_RUNTIME_DIR, e /var/run dopo il resolve),
# /dev (/dev/shm è l'unico punto scrivibile ed è tmpfs). Elenco statico
# invece di $XDG_RUNTIME_DIR: la regola non dipende dall'ambiente del
# processo che valida.
STATIC_VOLATILE = (Path("/tmp"), Path("/var/tmp"), Path("/run"), Path("/dev"))

VOLATILE_FSTYPES = frozenset({"tmpfs", "ramfs"})

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_TRUE = frozenset({"1", "yes", "true", "on"})


@dataclass(frozen=True)
class LocationDecision:
    path: Path | None
    error: str | None = None
    volatile: str | None = None
    warnings: tuple[str, ...] = ()
    is_default: bool = False
    outside_home: bool = False
    # Permessi attuali di una directory esistente più larghi di 0700:
    # ensure_private_dir() li restringerebbe, e per una directory scelta
    # dall'utente (magari condivisa) va chiesta conferma prima.
    loose_mode: int | None = None

    @property
    def usable(self) -> bool:
        """Accettabile per le Impostazioni e per la unit systemd."""
        return self.error is None and self.volatile is None


def default_quarantine_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / DEFAULT_QUARANTINE_SUBDIR


# -- direttive della unit ------------------------------------------------

def _directive_paths(value: str) -> list[Path]:
    """Percorsi di InaccessiblePaths=/TemporaryFileSystem=: prefissi
    "-" e "+" (opzionalità, radice) e suffisso ":opzioni" rimossi."""
    paths = []
    for token in value.split():
        token = token.lstrip("-+")
        token = token.split(":", 1)[0]
        if token.startswith("/"):
            paths.append(Path(token))
    return paths


def hidden_paths_from_unit(text: str) -> set[Path]:
    """
    Percorsi che le direttive di sandbox della unit rendono privati,
    volatili o inaccessibili al servizio. Solo le direttive che producono
    fallimenti SILENZIOSI: ProtectSystem=strict o ProtectHome=read-only
    danno EROFS, cioè un errore visibile, e restano fuori.
    """
    hidden: set[Path] = set()
    in_service = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            in_service = line == "[Service]"
            continue
        if not in_service or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "PrivateTmp" and value.lower() in _TRUE:
            hidden |= {Path("/tmp"), Path("/var/tmp")}
        elif key == "PrivateDevices" and value.lower() in _TRUE:
            hidden.add(Path("/dev"))
        elif key == "ProtectHome" and value.lower() in _TRUE | {"tmpfs"}:
            hidden |= {Path("/home"), Path("/root"), Path("/run/user")}
        elif key in ("InaccessiblePaths", "TemporaryFileSystem"):
            hidden.update(_directive_paths(value))
    return hidden


def installed_unit_hidden_paths(unit: Path = UNIT_PATH) -> set[Path]:
    """Come hidden_paths_from_unit() sulla unit installata; insieme vuoto
    se manca (es. esecuzione dal sorgente): restano le regole statiche."""
    try:
        return hidden_paths_from_unit(unit.read_text(encoding="utf-8"))
    except OSError:
        return set()


# -- file system montati -------------------------------------------------

def _unescape_mount(field: str) -> str:
    # mountinfo codifica spazio, tab, newline e backslash in ottale (\040).
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[tuple[Path, str]]:
    """(punto di montaggio, tipo di file system) da /proc/self/mountinfo."""
    mounts = []
    for line in text.splitlines():
        fields = line.split()
        try:
            sep = fields.index("-")
            mounts.append((Path(_unescape_mount(fields[4])), fields[sep + 1]))
        except (ValueError, IndexError):
            continue
    return mounts


def fstype_of(path: Path, mounts: Iterable[tuple[Path, str]]) -> str | None:
    """Tipo del file system che contiene path: il montaggio più lungo che
    ne è prefisso. Vale anche per percorsi non ancora esistenti."""
    best: tuple[int, str] | None = None
    for mountpoint, fstype in mounts:
        if path == mountpoint or path.is_relative_to(mountpoint):
            depth = len(mountpoint.parts)
            if best is None or depth >= best[0]:
                best = (depth, fstype)
    return best[1] if best else None


def _read_mountinfo() -> str:
    try:
        return Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# -- decisione -----------------------------------------------------------

def _under(path: Path, roots: Iterable[Path]) -> Path | None:
    for root in roots:
        for form in {root, root.resolve()}:
            if path == form or path.is_relative_to(form):
                return root
    return None


def decide(
    raw: str,
    *,
    home: Path | None = None,
    unit_hidden: Iterable[Path] | None = None,
    mountinfo: str | None = None,
    volatile_roots: Iterable[Path] = STATIC_VOLATILE,
) -> LocationDecision:
    """
    Valuta la directory di quarantena scritta dall'utente.

    home, unit_hidden, mountinfo e volatile_roots sono iniettabili per i
    test (le cui directory temporanee stanno sotto /tmp); di default si
    usano la home reale, la unit installata, /proc/self/mountinfo e
    l'elenco statico.
    """
    if not raw or not raw.strip():
        return LocationDecision(None, error="Directory di quarantena non indicata.")
    if _CONTROL_CHARS.search(raw):
        return LocationDecision(
            None, error="Il percorso contiene caratteri di controllo (a capo, tabulazioni)."
        )

    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        return LocationDecision(
            None,
            error=(
                f"Il percorso deve essere assoluto: «{expanded}» verrebbe "
                "interpretato rispetto a una directory di lavoro arbitraria."
            ),
        )

    path = expanded.resolve()
    home_dir = (home or Path.home()).resolve()
    is_default = path == default_quarantine_dir(home_dir).resolve()
    outside_home = not (path == home_dir or path.is_relative_to(home_dir))

    # La quarantena è esclusa dalla scansione (cli.py): se contenesse la
    # home, cioè la radice della scansione programmata, l'esclusione
    # svuoterebbe la scansione intera. Vale per la home stessa, /home e /.
    if home_dir == path or home_dir.is_relative_to(path):
        return LocationDecision(
            path,
            error=(
                f"«{path}» contiene la home, che la scansione programmata "
                "controlla: usa una sua sottodirectory o una directory separata."
            ),
            outside_home=outside_home,
        )

    hidden = set(installed_unit_hidden_paths() if unit_hidden is None else unit_hidden)
    root = _under(path, volatile_roots) or _under(path, hidden)
    volatile = None
    if root is not None:
        volatile = (
            f"«{path}» è sotto {root}, che per la scansione programmata è "
            "temporanea o inaccessibile: i file messi in quarantena verrebbero "
            "persi senza alcun avviso."
        )

    warnings: list[str] = []
    fstype = fstype_of(path, parse_mountinfo(_read_mountinfo() if mountinfo is None else mountinfo))
    if volatile is None and fstype in VOLATILE_FSTYPES:
        warnings.append(
            f"«{path}» è su un file system {fstype}: il contenuto della quarantena "
            "va perso a ogni riavvio."
        )

    loose_mode = None
    try:
        st = os.stat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return LocationDecision(
            path, error=f"Impossibile verificare «{path}»: {exc.strerror}.", volatile=volatile
        )
    else:
        if not stat.S_ISDIR(st.st_mode):
            return LocationDecision(path, error=f"«{path}» esiste e non è una directory.", volatile=volatile)
        if st.st_uid != os.getuid():
            return LocationDecision(path, error=f"«{path}» appartiene a un altro utente.", volatile=volatile)
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            loose_mode = mode

    return LocationDecision(
        path,
        volatile=volatile,
        warnings=tuple(warnings),
        is_default=is_default,
        outside_home=outside_home,
        loose_mode=loose_mode,
    )
