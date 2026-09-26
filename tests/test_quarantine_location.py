"""
Test di quarantine_location: la regola che impedisce di mettere la
quarantena dove la scansione programmata la perderebbe in silenzio.

I test sulla unit leggono il file vero in debian/: se qualcuno aggiunge
alla unit una direttiva che nasconde percorsi, o cambia la quarantena
predefinita nell'ExecStart, questi test falliscono invece di lasciar
divergere la regola dal sandbox.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from klamav_py.quarantine_location import (
    DEFAULT_QUARANTINE_SUBDIR,
    STATIC_VOLATILE,
    decide,
    default_quarantine_dir,
    fstype_of,
    hidden_paths_from_unit,
    parse_mountinfo,
)

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "debian" / "klamav-py.klamav-scan.user.service"

# Nessun file system volatile, nessuna direttiva di unit e nessuna radice
# volatile statica, salvo dove il test li fornisce: i risultati non
# dipendono dalla macchina, e la home finta di tmp_path (sotto /tmp) non
# risulta volatile di suo.
MOUNTS_EXT4 = "22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n"
NEUTRAL = dict(unit_hidden=(), mountinfo=MOUNTS_EXT4, volatile_roots=())
# Home inesistente fuori da /tmp, per i test sulle radici statiche.
HOME_FUORI_TMP = Path("/srv/nessuno/home/utente")

non_root = pytest.mark.skipif(os.getuid() == 0, reason="da root ogni file è 'suo'")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home" / "utente"
    h.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _decide(raw, home, **kw):
    return decide(raw, home=home, **{**NEUTRAL, **kw})


# -- errori bloccanti ----------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   "])
def test_vuoto(raw, home):
    d = _decide(raw, home)
    assert d.error and not d.usable


@pytest.mark.parametrize("raw", ["/srv/a\nb", "/srv/a\tb", "/srv/\x00x", "/srv/\x7fx"])
def test_caratteri_di_controllo(raw, home):
    assert _decide(raw, home).error


def test_relativo_rifiutato_mostrando_il_percorso_espanso(home):
    d = _decide("quarantena", home)
    assert d.error and "«quarantena»" in d.error


def test_tilde_espansa_prima_del_resolve(home):
    # Path("~/q").resolve() darebbe <cwd>/~/q: l'ordine conta.
    d = _decide("~/q", home)
    assert d.usable and d.path == home / "q"


@pytest.mark.parametrize("rel", [".", "..", "../.."])
def test_home_e_suoi_antenati_rifiutati(rel, home):
    d = _decide(str(home / rel), home)
    assert d.error and "contiene la home" in d.error


def test_radice_rifiutata(home):
    assert _decide("/", home).error


def test_file_esistente_rifiutato(home):
    f = home / "file"
    f.write_text("x")
    assert "non è una directory" in _decide(str(f), home).error


@non_root
def test_directory_di_altro_utente_rifiutata(home):
    assert "altro utente" in _decide("/usr/share", home).error


# -- directory volatili --------------------------------------------------

@pytest.mark.parametrize("raw", [
    "/tmp/q", "/var/tmp/q", "/run/q", "/run/user/1000/q", "/dev/shm/q", "/tmp",
])
def test_statiche_volatili(raw):
    # volatile va riportato anche se c'è un errore (/tmp è di root): la
    # CLI in primo piano lo tratta come avviso e deve comunque vederlo.
    d = _decide(raw, HOME_FUORI_TMP, volatile_roots=STATIC_VOLATILE)
    assert d.volatile and not d.usable


@pytest.mark.skipif(not Path("/var/run").is_symlink(), reason="/var/run non è un symlink qui")
def test_var_run_risolto_su_run():
    d = _decide("/var/run/q", HOME_FUORI_TMP, volatile_roots=STATIC_VOLATILE)
    assert d.path.is_relative_to(Path("/run")) and d.volatile


def test_symlink_verso_directory_volatile(home, tmp_path):
    (home / "link").symlink_to("/var/tmp")
    d = _decide("~/link/q", home, volatile_roots=[Path("/var/tmp")])
    assert d.volatile and d.path == Path("/var/tmp/q")


def test_percorso_nascosto_dalla_unit(home):
    d = _decide("/srv/q", home, unit_hidden={Path("/srv")})
    assert d.volatile and "/srv" in d.volatile


def test_prefisso_non_confuso_con_nome_simile(home):
    # /tmpdati non è sotto /tmp: il confronto è per componenti, non stringhe.
    d = _decide("/tmpdati/q", HOME_FUORI_TMP, volatile_roots=STATIC_VOLATILE)
    assert d.volatile is None


def test_elenco_statico_invariato():
    # Rimuovere una voce riapre la perdita silenziosa: il test lo rende
    # una scelta esplicita.
    assert set(STATIC_VOLATILE) == {Path(p) for p in ("/tmp", "/var/tmp", "/run", "/dev")}


# -- tmpfs e ramfs altrove -----------------------------------------------

MOUNTS = (
    "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
    "30 22 0:40 / /mnt rw - tmpfs tmpfs rw\n"
    "31 30 8:2 / /mnt/dati rw - ext4 /dev/sdb1 rw\n"
    "32 22 0:41 / /media/my\\040disk rw - ramfs none rw\n"
)


def test_mountinfo_decodifica_gli_spazi():
    assert (Path("/media/my disk"), "ramfs") in parse_mountinfo(MOUNTS)


def test_vince_il_montaggio_piu_lungo():
    mounts = parse_mountinfo(MOUNTS)
    assert fstype_of(Path("/mnt/ramdisk/q"), mounts) == "tmpfs"
    assert fstype_of(Path("/mnt/dati/q"), mounts) == "ext4"
    assert fstype_of(Path("/srv/q"), mounts) == "ext4"


@pytest.mark.parametrize("raw", ["/mnt/ramdisk/q", "/media/my disk/q"])
def test_tmpfs_avviso_non_bloccante(raw, home):
    d = _decide(raw, home, mountinfo=MOUNTS)
    assert d.usable and d.warnings and "riavvio" in d.warnings[0]


def test_nessun_avviso_doppio_se_gia_volatile(home):
    d = _decide("/run/q", home, mountinfo="1 0 0:1 / /run rw - tmpfs tmpfs rw\n",
                volatile_roots=STATIC_VOLATILE)
    assert d.volatile and not d.warnings


# -- esiti informativi ---------------------------------------------------

@pytest.mark.parametrize("raw", [
    "~/.local/share/klamav-py/quarantine",
    "~/.local/share/klamav-py/quarantine/",
    "~/.local/share/klamav-py/../klamav-py/quarantine",
])
def test_riconosce_il_default_in_ogni_forma(raw, home):
    assert _decide(raw, home).is_default


def test_fuori_home(home):
    assert _decide("/srv/q", home).outside_home
    assert not _decide("~/q", home).outside_home


def test_symlink_in_home_verso_fuori_e_fuori_home(home, tmp_path):
    esterna = tmp_path / "esterna"
    esterna.mkdir()
    (home / "link").symlink_to(esterna)
    d = _decide("~/link/q", home)
    assert d.outside_home and d.path == esterna / "q"


@pytest.mark.parametrize("mode, atteso", [(0o755, 0o755), (0o750, 0o750), (0o700, None)])
def test_permessi_larghi_segnalati(mode, atteso, home):
    q = home / "q"
    q.mkdir()
    q.chmod(mode)
    assert _decide(str(q), home).loose_mode == atteso


def test_directory_inesistente_accettata(home):
    d = _decide("~/nuova/q", home)
    assert d.usable and d.loose_mode is None


# -- direttive della unit ------------------------------------------------

def test_parsing_direttive():
    text = """
[Unit]
PrivateDevices=yes
[Service]
# PrivateTmp=true
PrivateTmp=yes
PrivateDevices=true
InaccessiblePaths=-/opt/a /opt/b
TemporaryFileSystem=/var/lib/x:ro
ProtectHome=read-only
ProtectSystem=strict
"""
    assert hidden_paths_from_unit(text) == {
        Path("/tmp"), Path("/var/tmp"), Path("/dev"),
        Path("/opt/a"), Path("/opt/b"), Path("/var/lib/x"),
    }


def test_protecthome_tmpfs_nasconde_le_home():
    assert Path("/home") in hidden_paths_from_unit("[Service]\nProtectHome=tmpfs\n")


# Direttive di sandbox della unit spedita e come le tratta la regola.
# Una direttiva nuova che non compare qui fa fallire il test: va decisa
# esplicitamente, perché potrebbe nascondere percorsi (fallimento
# silenzioso) invece di renderli di sola lettura (fallimento visibile).
HIDING = {"PrivateTmp", "PrivateDevices", "ProtectHome", "InaccessiblePaths", "TemporaryFileSystem"}
LOUD_OR_UNRELATED = {
    "ProtectSystem", "ReadWritePaths", "ReadOnlyPaths",
    "ProtectKernelTunables", "ProtectKernelModules", "ProtectKernelLogs",
    "ProtectControlGroups", "ProtectHostname", "ProtectClock", "ProtectProc",
    "PrivateNetwork", "PrivateUsers", "PrivateIPC",
}
_SANDBOX_KEY = re.compile(r"^(Private|Protect|Temporary|Inaccessible|ReadOnly|ReadWrite|Bind)\w*(?==)")


def test_unit_spedita_nessuna_direttiva_non_classificata():
    keys = {m.group(0) for line in UNIT.read_text().splitlines() if (m := _SANDBOX_KEY.match(line.strip()))}
    assert keys <= HIDING | LOUD_OR_UNRELATED, keys - HIDING - LOUD_OR_UNRELATED


def test_unit_spedita_ogni_percorso_nascosto_e_rifiutato(home):
    hidden = hidden_paths_from_unit(UNIT.read_text())
    assert hidden, "la unit dovrebbe avere almeno PrivateTmp"
    for h in hidden:
        assert _decide(str(h / "q"), home, unit_hidden=hidden).volatile, h


def test_default_coincide_con_execstart_della_unit():
    # Il drop-in non viene scritto quando la directory è quella predefinita:
    # funziona solo se la unit usa davvero lo stesso percorso.
    exec_start = next(l for l in UNIT.read_text().splitlines() if l.startswith("ExecStart="))
    m = re.search(r"--quarantine (\S+)", exec_start)
    assert m and m.group(1) == f"%h/{DEFAULT_QUARANTINE_SUBDIR}"


def test_default_quarantine_dir(home):
    assert default_quarantine_dir(home) == home / DEFAULT_QUARANTINE_SUBDIR
