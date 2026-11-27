"""
Validazione della directory di quarantena scelta dall'utente.

Unica fonte della regola, usata dal salvataggio delle Impostazioni e dalla
CLI (--quarantine): le stringhe di motivo sono condivise, quindi la regola non
può divergere fra i consumatori. Come quarantine_policy.py, il modulo non
importa nulla di Qt.

Perché serve (vedi anche klamav-py(1), SICUREZZA): una quarantena in una
directory volatile (/tmp, /var/tmp, /run) sparisce con l'indice a un
riavvio, o prima se la CLI gira in un servizio systemd con PrivateTmp=true
(la /var/tmp privata si elimina a fine servizio): file infetti distrutti
senza errori né notifiche. La regola quindi rifiuta le directory volatili.
(Fino alla 0.1.21 leggeva anche le direttive di sandbox della unit
klamav-scan.service, tolta insieme al timer di sistema.)

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
from typing import Iterable, Mapping

DEFAULT_QUARANTINE_SUBDIR = Path(".local/share/klamav-py/quarantine")

# Directory volatili rifiutate sempre:
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
        """Accettabile per le Impostazioni e per la CLI sotto systemd."""
        return self.error is None and self.volatile is None


def default_quarantine_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / DEFAULT_QUARANTINE_SUBDIR


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


def root_inside(path: Path, roots: Mapping[str, Path]) -> str | None:
    """
    Motivo di rifiuto se la quarantena (già risolta) è uguale a una radice
    di scansione o la contiene, altrimenti None.

    La quarantena è esclusa dalla traversata (clamd_client._iter_files):
    una radice al suo interno produrrebbe una scansione di zero file,
    conclusa senza errori. Le radici si risolvono qui perché il confronto
    in _iter_files è letterale sulle forme canoniche.
    """
    for label, root in roots.items():
        resolved = Path(root).expanduser().resolve()
        if resolved == path or resolved.is_relative_to(path):
            return (
                f"«{path}» contiene {resolved}, da cui parte la scansione "
                f"({label}): la quarantena è esclusa dalla scansione, che "
                "non controllerebbe nulla. Usa una directory separata."
            )
    return None


def decide(
    raw: str,
    *,
    home: Path | None = None,
    roots: Mapping[str, Path] | None = None,
    unit_hidden: Iterable[Path] | None = None,
    mountinfo: str | None = None,
    volatile_roots: Iterable[Path] = STATIC_VOLATILE,
) -> LocationDecision:
    """
    Valuta la directory di quarantena scritta dall'utente.

    roots: radici di scansione ulteriori rispetto alla home (etichetta ->
    percorso), per esempio la cartella della pianificazione interna della
    GUI: la quarantena non può né coincidere con una di esse né contenerla.

    home, mountinfo e volatile_roots sono iniettabili per i test (le cui
    directory temporanee stanno sotto /tmp); di default si usano la home
    reale, /proc/self/mountinfo e l'elenco statico. unit_hidden: percorsi
    nascosti in più, trattati come volatili (vuoto di default).
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

    if roots:
        contained = root_inside(path, roots)
        if contained:
            return LocationDecision(path, error=contained, outside_home=outside_home)

    hidden = set(unit_hidden or ())
    root = _under(path, volatile_roots) or _under(path, hidden)
    volatile = None
    if root is not None:
        volatile = (
            f"«{path}» è sotto {root}, che è temporanea o inaccessibile: "
            "i file messi in quarantena verrebbero persi senza alcun avviso."
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
