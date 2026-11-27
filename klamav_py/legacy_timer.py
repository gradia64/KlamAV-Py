"""Pulizia della scansione programmata di sistema (klamav-scan.timer).

Il timer utente di systemd è stato tolto dopo la 0.1.21: resta solo la
pianificazione interna della GUI, attivabile e configurabile dall'utente
nella pagina Pianificazione. Il pacchetto nuovo non installa più le unit,
ma chi aggiorna si ritrova i resti creati fuori da dpkg/pacman:

- il collegamento di abilitazione in ~/.config/systemd/user/timers.target.wants
  (systemctl --user enable), che punta a una unit che non esiste più;
- i drop-in scritti dalla GUI (50-klamav-py.conf, prima riga HEADER) per
  klamav-scan.service e klamav-scan.timer;
- il marcatore della scansione del timer interrotta (timer-scan.json);
- il timer ancora caricato nel manager utente fino al daemon-reload.

L'abilitazione globale del .deb (/etc/systemd/user) la toglie il postinst.

Si pulisce solo quando la unit del timer non è più installata: con un
pacchetto vecchio ancora presente (versione di sviluppo lanciata accanto)
il timer è suo e non si tocca. I file non nostri (drop-in scritti a mano,
override.conf) restano dove sono: si nominano nell'avviso. Nessun Qt: la
GUI chiama cleanup() fuori dal suo thread.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

UNIT_DIR = Path("/usr/lib/systemd/user")
TIMER_NAME = "klamav-scan.timer"
SERVICE_NAME = "klamav-scan.service"
DROPIN_NAME = "50-klamav-py.conf"
HEADER = "# Generato da KlamAV-Py: non modificare a mano."
STATE_FILE = Path(".local/share/klamav-py/timer-scan.json")  # relativo alla home
SYSTEMCTL = "/usr/bin/systemctl"


@dataclass
class Cleanup:
    removed: list[Path] = field(default_factory=list)
    foreign: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def notice(self) -> str | None:
        """Testo unico per la notifica, la pagina Pianificazione e stderr;
        None se non c'era niente di nostro da togliere né da segnalare."""
        if not self.removed and not self.foreign:
            return None
        text = (
            "La scansione programmata di sistema (klamav-scan.timer) non esiste più: "
            "le scansioni automatiche si attivano e si configurano nella pagina "
            "Pianificazione, e girano mentre KlamAV-Py è aperto (anche nella tray)."
        )
        if self.removed:
            text += " Tolti i resti della configurazione: " + ", ".join(
                str(p) for p in self.removed) + "."
        if self.foreign:
            text += (" Restano file che non sono stati scritti da KlamAV-Py, ora "
                     "inutili: " + ", ".join(str(p) for p in self.foreign) + ".")
        if self.problems:
            text += " Problemi: " + "; ".join(self.problems) + "."
        return text


def _config_dir(config_home: Path | None) -> Path:
    if config_home is None:
        xdg = os.environ.get("XDG_CONFIG_HOME", "")
        config_home = Path(xdg) if xdg and os.path.isabs(xdg) else Path.home() / ".config"
    return config_home / "systemd" / "user"


def _is_ours(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readline().rstrip("\n") == HEADER
    except OSError:
        return False


def _remove(path: Path, out: Cleanup) -> None:
    try:
        path.unlink()
        out.removed.append(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        out.problems.append(f"{path}: {exc.strerror or exc}")


def _systemctl_user(*args: str) -> str | None:
    """None se riuscito, altrimenti il motivo (solo un avviso)."""
    try:
        r = subprocess.run([SYSTEMCTL, "--user", *args], capture_output=True,
                           text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"systemctl --user {args[0]}: {exc}"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip() or f"uscita {r.returncode}"
        return f"systemctl --user {args[0]}: {detail}"
    return None


def cleanup(*, config_home: Path | None = None, home: Path | None = None,
            unit_dir: Path = UNIT_DIR, systemctl=_systemctl_user) -> Cleanup | None:
    """Toglie i resti del timer di sistema. None se la unit è ancora
    installata (non tocca nulla) o se non c'era niente da segnalare."""
    if (unit_dir / TIMER_NAME).exists():
        return None
    config = _config_dir(config_home)
    out = Cleanup()

    link = config / "timers.target.wants" / TIMER_NAME
    if link.is_symlink() and Path(os.readlink(link)) == unit_dir / TIMER_NAME:
        _remove(link, out)

    for unit in (SERVICE_NAME, TIMER_NAME):
        directory = config / f"{unit}.d"
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name == DROPIN_NAME and entry.is_file() and _is_ours(entry):
                _remove(entry, out)
            else:
                out.foreign.append(entry)
        try:
            directory.rmdir()  # solo se vuota
        except OSError:
            pass

    state = (home or Path.home()) / STATE_FILE
    if state.is_file():
        _remove(state, out)

    if out.removed:
        # Il timer resta caricato nel manager utente fino al daemon-reload,
        # e scatterebbe ancora una volta: si ferma prima. Lo stop fallisce
        # anche quando non era caricato (sessione già riavviata): non è un
        # problema da riportare.
        systemctl("stop", TIMER_NAME, SERVICE_NAME)
        problem = systemctl("daemon-reload")
        if problem:
            out.problems.append(problem)
    if not out.removed and not out.foreign:
        return None
    return out
