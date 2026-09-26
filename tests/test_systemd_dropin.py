"""
Test di systemd_dropin: quoting, drop-in generato e sua gestione su disco.

Tre verifiche indipendenti, ognuna copre il punto cieco delle altre:

- golden: i byte esatti del drop-in (cosa scriviamo);
- round-trip e parser della CLI: l'argv che systemd ne ricava è accettato
  da build_parser() con la directory giusta (cosa significa). Un golden
  da solo avrebbe fissato anche un flag sbagliato, come --quarantine-dir;
- systemd-analyze verify, se disponibile: la unit con il drop-in carica
  senza avvisi (cosa ne pensa systemd). Il binario viene sostituito con
  "true" nella copia verificata, perché verify controlla che esista.
"""

from __future__ import annotations

import os
import random
from types import SimpleNamespace
import shutil
import subprocess
from pathlib import Path

import pytest

from klamav_py.clamd_client import DEFAULT_SOCKET, ClamdEndpoint
from klamav_py.cli import build_parser
from klamav_py.quarantine_location import DEFAULT_QUARANTINE_SUBDIR
from klamav_py.systemd_dropin import (
    EXEC_BINARY,
    foreign_overrides,
    EXEC_TEMPLATE,
    HEADER,
    DropinConflict,
    dropin_path,
    is_ours,
    quote_exec_arg,
    quote_path_value,
    render_dropin,
    split_exec,
    split_paths,
    sync_dropin,
)

ROOT = Path(__file__).resolve().parent.parent
UNIT = ROOT / "debian" / "klamav-py.klamav-scan.user.service"
HOME = Path("/home/utente")

INSIDIOSI = [
    "/srv/quarantena",
    "/srv/Le Mie Cartelle/quarantena",
    "/srv/100% sicuro",
    "/srv/%h",
    "/srv/%%",
    "/srv/$HOME",
    "/srv/${HOME}",
    "/srv/$$",
    '/srv/a"b',
    "/srv/a'b",
    "/srv/a\\b",
    "/srv/a\\\\\"%$",
    "/srv/città/quarantena",
    "/srv/  doppio  spazio  ",
]


def _values(text: str, key: str) -> list[str]:
    return [l.split("=", 1)[1] for l in text.splitlines() if l.startswith(f"{key}=")]


# -- quoting -------------------------------------------------------------

@pytest.mark.parametrize("value, atteso", [
    ("/srv/q", '"/srv/q"'),
    ("/srv/a b", '"/srv/a b"'),
    ("/srv/100%", '"/srv/100%%"'),
    ("/srv/$HOME", '"/srv/$$HOME"'),
    ('/srv/a"b', '"/srv/a\\"b"'),
    ("/srv/a\\b", '"/srv/a\\\\b"'),
])
def test_quote_exec_golden(value, atteso):
    assert quote_exec_arg(value) == atteso


def test_quote_path_non_raddoppia_il_dollaro():
    # ReadWritePaths espande gli specificatori ma non le variabili.
    assert quote_path_value("/srv/100% $X") == '"/srv/100%% $X"'


@pytest.mark.parametrize("value", ["/srv/a\nb", "/srv/a\tb", "/srv/a\rb", "/srv/\x00", "/srv/\x7f"])
def test_caratteri_di_controllo_rifiutati(value):
    # Un a capo chiuderebbe la riga e ne aprirebbe un'altra nel drop-in.
    with pytest.raises(ValueError):
        quote_exec_arg(value)
    with pytest.raises(ValueError):
        quote_path_value(value)


@pytest.mark.parametrize("value", INSIDIOSI)
def test_round_trip(value):
    assert split_exec(f"/bin/x {quote_exec_arg(value)}") == ["/bin/x", value]
    assert split_paths(quote_path_value(value)) == [value]


def test_round_trip_casuale():
    rng = random.Random(1010)
    alfabeto = "ab /%$\"'\\hàé{}"
    for _ in range(2000):
        value = "/" + "".join(rng.choice(alfabeto) for _ in range(rng.randint(0, 12)))
        assert split_exec(quote_exec_arg(value)) == [value], value
        assert split_paths(quote_path_value(value)) == [value], value


# -- drop-in -------------------------------------------------------------

GOLDEN_FUORI_HOME = f"""{HEADER}
# Scritto e rimosso dalle Impostazioni di klamav-py-gui; vedi klamav-py-gui(1).

[Service]
# Azzeramento obbligatorio: su Type=oneshot le ExecStart si sommano.
ExecStart=
ExecStart=/usr/bin/klamav-py scan %h --quarantine "/srv/quarantena" --quiet
ReadWritePaths="/srv/quarantena"
"""


def test_golden_fuori_home():
    assert render_dropin(Path("/srv/quarantena"), home=HOME) == GOLDEN_FUORI_HOME


def test_dentro_home_senza_readwritepaths():
    # La unit ha già ReadWritePaths=%h: non si allarga il sandbox.
    text = render_dropin(HOME / "Quarantena", home=HOME)
    assert "ReadWritePaths" not in text


@pytest.mark.parametrize("q", [None, HOME / DEFAULT_QUARANTINE_SUBDIR])
def test_nessun_dropin_per_il_default(q):
    assert render_dropin(q, home=HOME) is None


def test_percorso_relativo_rifiutato():
    with pytest.raises(ValueError):
        render_dropin(Path("quarantena"), home=HOME)


def test_execstart_azzerato_prima_della_riga_nuova():
    exec_lines = _values(render_dropin(Path("/srv/q"), home=HOME), "ExecStart")
    assert exec_lines[0] == "" and len(exec_lines) == 2


@pytest.mark.parametrize("value", INSIDIOSI)
def test_argv_accettato_dal_parser_della_cli(value):
    q = Path(value)
    text = render_dropin(q, home=HOME)
    argv = split_exec(_values(text, "ExecStart")[1], home=str(HOME))
    assert argv[0] == EXEC_BINARY
    args = build_parser().parse_args(argv[1:])
    assert args.command == "scan"
    assert args.path == HOME
    assert args.quarantine == q
    assert args.quiet
    if not q.is_relative_to(HOME):
        assert split_paths(_values(text, "ReadWritePaths")[0]) == [value]


def test_template_coincide_con_la_unit_spedita():
    # Il drop-in deve cambiare SOLO la quarantena: stesso argv della unit
    # quando la quarantena è quella predefinita.
    unit_exec = _values(UNIT.read_text(), "ExecStart")
    assert len(unit_exec) == 1
    nostro = EXEC_TEMPLATE.format(options="", quarantine=f"%h/{DEFAULT_QUARANTINE_SUBDIR}")
    assert split_exec(nostro, home=str(HOME)) == split_exec(unit_exec[0], home=str(HOME))


# -- systemd-analyze verify ----------------------------------------------

@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze non disponibile")
@pytest.mark.parametrize("value", ["/srv/quarantena", "/srv/Le Mie Cartelle/100% $HOME \"x\" \\y"])
def test_systemd_analyze_verify(value, tmp_path):
    true = shutil.which("true")
    units = tmp_path / "units"
    dropin_dir = units / "klamav-scan.service.d"
    dropin_dir.mkdir(parents=True)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)

    # Mutazione deliberata e limitata al binario: i byte li fissa il golden,
    # l'argv il test sul parser; qui conta che systemd carichi il tutto.
    (units / "klamav-scan.service").write_text(UNIT.read_text().replace(EXEC_BINARY, true))
    (units / "klamav-scan-notify.service").write_text(
        f"[Service]\nType=oneshot\nExecStart={true}\n"
    )
    (dropin_dir / "50-klamav-py.conf").write_text(
        render_dropin(Path(value), home=HOME).replace(EXEC_BINARY, true)
    )

    env = {**os.environ, "SYSTEMD_UNIT_PATH": f"{units}:", "XDG_RUNTIME_DIR": str(runtime)}
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", "--man=no", str(units / "klamav-scan.service")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    output = proc.stdout + proc.stderr
    if "Failed to initialize manager" in output:
        pytest.skip(f"manager utente non inizializzabile qui: {output.strip()}")
    # verify esce 0 anche con "Ignoring unknown escape" o "path is not
    # absolute, ignoring": ogni riga che cita il drop-in è un errore.
    assert proc.returncode == 0, output
    assert "50-klamav-py.conf" not in output, output


# -- gestione su disco ---------------------------------------------------

@pytest.fixture
def path(tmp_path):
    return dropin_path(tmp_path / "config")


def test_percorso_rispetta_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert dropin_path() == tmp_path / "systemd/user/klamav-scan.service.d/50-klamav-py.conf"


def test_xdg_config_home_relativa_ignorata(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relativa")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert dropin_path().is_relative_to(tmp_path / ".config")


def test_scrittura_privata_e_idempotente(path):
    text = render_dropin(Path("/srv/q"), home=HOME)
    assert sync_dropin(text, path) is True
    assert path.read_text() == text and is_ours(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert sync_dropin(text, path) is False  # nessun cambiamento


def test_riscrittura_del_proprio_file(path):
    sync_dropin(render_dropin(Path("/srv/a"), home=HOME), path)
    assert sync_dropin(render_dropin(Path("/srv/b"), home=HOME), path) is True
    assert '"/srv/b"' in path.read_text()


def test_rimozione_e_pulizia_della_directory(path):
    sync_dropin(render_dropin(Path("/srv/q"), home=HOME), path)
    assert sync_dropin(None, path) is True
    assert not path.exists() and not path.parent.exists()
    assert sync_dropin(None, path) is False  # già assente


def test_rimozione_lascia_gli_altri_dropin(path):
    sync_dropin(render_dropin(Path("/srv/q"), home=HOME), path)
    altro = path.parent / "90-utente.conf"
    altro.write_text("[Service]\nNice=10\n")
    sync_dropin(None, path)
    assert altro.exists()


@pytest.mark.parametrize("contenuto", ["[Service]\nNice=5\n", "", "# Generato da qualcun altro\n"])
def test_file_non_nostro_mai_toccato(path, contenuto):
    path.parent.mkdir(parents=True)
    path.write_text(contenuto)
    for text in (render_dropin(Path("/srv/q"), home=HOME), None):
        with pytest.raises(DropinConflict):
            sync_dropin(text, path)
    assert path.read_text() == contenuto


def test_intestazione_riconosciuta_solo_in_prima_riga(path):
    path.parent.mkdir(parents=True)
    path.write_text(f"[Service]\n{HEADER}\n")
    assert not is_ours(path)


# -- systemctl --user ----------------------------------------------------

import klamav_py.systemd_dropin as sd  # noqa: E402


def _fake_systemctl(monkeypatch, returncode=0, stdout="", stderr="", missing=False):
    calls = []

    def fake(*args, **kw):
        calls.append(args)
        if missing:
            return None
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(sd, "_systemctl_user", fake)
    return calls


@pytest.mark.parametrize("stdout, atteso", [
    ("enabled\n", True), ("enabled-runtime\n", True),
    ("disabled\n", False), ("masked\n", False), ("static\n", False),
    ("not-found\n", None), ("", None),
])
def test_timer_enabled(monkeypatch, stdout, atteso):
    calls = _fake_systemctl(monkeypatch, stdout=stdout)
    assert sd.timer_enabled() is atteso
    assert calls == [("is-enabled", "klamav-scan.timer")]


def test_timer_enabled_senza_systemctl(monkeypatch):
    _fake_systemctl(monkeypatch, missing=True)
    assert sd.timer_enabled() is None


@pytest.mark.parametrize("func, argv", [
    (sd.daemon_reload, ("daemon-reload",)),
    (sd.disable_timer, ("disable", "--now", "klamav-scan.timer")),
])
def test_comandi_systemctl(monkeypatch, func, argv):
    calls = _fake_systemctl(monkeypatch)
    assert func() is None and calls == [argv]
    _fake_systemctl(monkeypatch, returncode=1, stderr="Failed to connect to bus\n")
    assert func() == "Failed to connect to bus"
    _fake_systemctl(monkeypatch, missing=True)
    assert func() == "systemctl non disponibile"


def test_systemctl_non_solleva_mai(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("systemctl")
    monkeypatch.setattr(subprocess, "run", boom)
    assert sd._systemctl_user("daemon-reload") is None



# -- endpoint di clamd ---------------------------------------------------

DEFAULT_Q = HOME / DEFAULT_QUARANTINE_SUBDIR

GOLDEN_TCP = f"""{HEADER}
# Scritto e rimosso dalle Impostazioni di klamav-py-gui; vedi klamav-py-gui(1).

[Service]
# Azzeramento obbligatorio: su Type=oneshot le ExecStart si sommano.
ExecStart=
ExecStart=/usr/bin/klamav-py --tcp "10.0.0.5:3311" scan %h --quarantine "{DEFAULT_Q}" --quiet
# clamd via TCP: rete consentita solo per questo (vedi klamav-py(1), SICUREZZA).
PrivateNetwork=no
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
"""


def test_golden_tcp_con_quarantena_predefinita():
    # Basta l'endpoint a richiedere il drop-in, anche con la quarantena
    # predefinita, che compare allora con il suo percorso esplicito.
    assert render_dropin(DEFAULT_Q, home=HOME, endpoint=ClamdEndpoint.tcp("10.0.0.5", 3311)) == GOLDEN_TCP


@pytest.mark.parametrize("endpoint", [
    None,
    ClamdEndpoint(),
    ClamdEndpoint.unix(DEFAULT_SOCKET),
    ClamdEndpoint(transport="unix", tcp_host="10.0.0.5", tcp_port=4000),  # host salvato ma non in uso
])
def test_nessun_dropin_con_tutto_predefinito(endpoint):
    assert render_dropin(DEFAULT_Q, home=HOME, endpoint=endpoint) is None
    assert render_dropin(None, home=HOME, endpoint=endpoint) is None


def test_socket_non_predefinito_senza_rete():
    text = render_dropin(None, home=HOME, endpoint=ClamdEndpoint.unix("/run/clamd.scan/clamd.sock"))
    assert '--socket "/run/clamd.scan/clamd.sock" scan' in text
    assert "PrivateNetwork" not in text and "RestrictAddressFamilies" not in text


def test_rete_solo_con_tcp():
    unix = render_dropin(Path("/srv/q"), home=HOME, endpoint=ClamdEndpoint())
    tcp = render_dropin(Path("/srv/q"), home=HOME, endpoint=ClamdEndpoint.tcp("::1"))
    assert "PrivateNetwork" not in unix
    assert _values(tcp, "PrivateNetwork") == ["no"]
    # Il filtro resta: esteso, non tolto.
    assert _values(tcp, "RestrictAddressFamilies") == ["AF_UNIX AF_INET AF_INET6"]


@pytest.mark.parametrize("endpoint", [
    ClamdEndpoint.tcp("localhost"),
    ClamdEndpoint.tcp("10.0.0.5", 3311),
    ClamdEndpoint.tcp("::1", 3311),
    ClamdEndpoint.tcp("fe80::1%eth0"),
    ClamdEndpoint.unix("/run/clamd.scan/clamd.sock"),
    ClamdEndpoint.unix("/srv/Il Mio clamd/100% $HOME.sock"),
])
@pytest.mark.parametrize("q", [DEFAULT_Q, Path("/srv/quarantena")])
def test_argv_endpoint_accettato_dal_parser_della_cli(endpoint, q):
    text = render_dropin(q, home=HOME, endpoint=endpoint)
    argv = split_exec(_values(text, "ExecStart")[1], home=str(HOME))
    args = build_parser().parse_args(argv[1:])
    assert args.endpoint == endpoint
    assert args.command == "scan" and args.path == HOME and args.quarantine == q


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze non disponibile")
def test_systemd_analyze_verify_tcp(tmp_path):
    true = shutil.which("true")
    units = tmp_path / "units"
    dropin_dir = units / "klamav-scan.service.d"
    dropin_dir.mkdir(parents=True)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    (units / "klamav-scan.service").write_text(UNIT.read_text().replace(EXEC_BINARY, true))
    (units / "klamav-scan-notify.service").write_text(f"[Service]\nType=oneshot\nExecStart={true}\n")
    text = render_dropin(Path("/srv/q"), home=HOME, endpoint=ClamdEndpoint.tcp("::1", 3311))
    (dropin_dir / "50-klamav-py.conf").write_text(text.replace(EXEC_BINARY, true))
    env = {**os.environ, "SYSTEMD_UNIT_PATH": f"{units}:", "XDG_RUNTIME_DIR": str(runtime)}
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", "--man=no", str(units / "klamav-scan.service")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    output = proc.stdout + proc.stderr
    if "Failed to initialize manager" in output:
        pytest.skip(output.strip())
    assert proc.returncode == 0, output
    assert "50-klamav-py.conf" not in output, output



# -- drop-in di altri ----------------------------------------------------

# Il caso reale che ha motivato il rilevamento: un override.conf creato con
# "systemctl --user edit" per aggiungere delle esclusioni.
OVERRIDE_REALE = """[Service]
ExecStart=
ExecStart=/usr/bin/klamav-py scan %h --exclude %h/.docker/diun/data --quiet
UMask=0077

# Rete: solo socket Unix con percorso (clamd)
PrivateNetwork=true
RestrictAddressFamilies=AF_UNIX
"""


@pytest.fixture
def dirs(tmp_path):
    user = tmp_path / "user" / "klamav-scan.service.d"
    etc = tmp_path / "etc" / "klamav-scan.service.d"
    user.mkdir(parents=True)
    etc.mkdir(parents=True)
    return SimpleNamespace(user=user, etc=etc, ours=user / "50-klamav-py.conf")


def _write_ours(dirs, endpoint=None):
    sync_dropin(render_dropin(Path("/srv/q"), home=HOME, endpoint=endpoint), dirs.ours)


def _paths(dirs):
    return sd._scan_dropin_dirs((dirs.user.parent, dirs.etc.parent))


def test_override_reale_dopo_il_nostro(dirs):
    _write_ours(dirs)
    (dirs.user / "override.conf").write_text(OVERRIDE_REALE)
    found = foreign_overrides(tcp=False, ours=dirs.ours, paths=_paths(dirs))
    assert len(found) == 1 and found[0].keys == ("ExecStart",) and found[0].after
    assert "override.conf" in found[0].describe()
    assert "non la cartella di quarantena e la connessione" in found[0].describe()


def test_override_reale_con_tcp_segnala_anche_la_rete(dirs):
    _write_ours(dirs, ClamdEndpoint.tcp("127.0.0.1"))
    (dirs.user / "override.conf").write_text(OVERRIDE_REALE)
    (found,) = foreign_overrides(tcp=True, ours=dirs.ours, paths=_paths(dirs))
    assert found.keys == ("ExecStart", "PrivateNetwork")
    assert "via TCP" in found.describe()


def test_dropin_precedente_viene_sostituito_dal_nostro(dirs):
    _write_ours(dirs)
    (dirs.user / "10-mio.conf").write_text("[Service]\nExecStart=\nExecStart=/bin/true\n")
    (found,) = foreign_overrides(tcp=False, ours=dirs.ours, paths=_paths(dirs))
    assert not found.after and "personalizzazioni" in found.describe()


def test_senza_il_nostro_anche_un_dropin_precedente_vince(dirs):
    # Impostazioni predefinite: nessun nostro drop-in, quindi l'ExecStart
    # altrui è quello in uso, qualunque sia il nome del file.
    (dirs.user / "10-mio.conf").write_text("[Service]\nExecStart=\nExecStart=/bin/true\n")
    (found,) = foreign_overrides(tcp=False, ours=dirs.ours, paths=_paths(dirs))
    assert "usa il comando di quel file" in found.describe()


def test_privatenetwork_prima_del_nostro_non_conta(dirs):
    _write_ours(dirs, ClamdEndpoint.tcp("127.0.0.1"))
    (dirs.user / "10-rete.conf").write_text("[Service]\nPrivateNetwork=true\n")
    assert foreign_overrides(tcp=True, ours=dirs.ours, paths=_paths(dirs)) == []


@pytest.mark.parametrize("contenuto", [
    "[Service]\nNice=5\nIOSchedulingClass=idle\n",
    "[Unit]\nExecStart=/bin/true\n",  # sezione sbagliata
    "[Service]\n# ExecStart=/bin/true\n; ExecStart=/bin/true\n",  # commenti
    "[Service]\nPrivateNetwork=true\n",  # senza TCP non interessa
])
def test_nessun_conflitto(dirs, contenuto):
    _write_ours(dirs)
    (dirs.user / "override.conf").write_text(contenuto)
    assert foreign_overrides(tcp=False, ours=dirs.ours, paths=_paths(dirs)) == []


def test_il_nostro_non_e_mai_un_conflitto(dirs):
    _write_ours(dirs, ClamdEndpoint.tcp("::1"))
    assert foreign_overrides(tcp=True, ours=dirs.ours, paths=_paths(dirs)) == []


def test_directory_utente_prevale_a_parita_di_nome(dirs):
    (dirs.etc / "override.conf").write_text("[Service]\nExecStart=/bin/false\n")
    (dirs.user / "override.conf").write_text("[Service]\nNice=5\n")
    (dirs.etc / "90-altro.conf").write_text("[Service]\nExecStart=/bin/false\n")
    paths = _paths(dirs)
    assert [p.name for p in paths] == ["90-altro.conf", "override.conf"]
    assert paths[1].parent == dirs.user  # quello di /etc è mascherato
    (found,) = foreign_overrides(tcp=False, ours=dirs.ours, paths=paths)
    assert found.path.name == "90-altro.conf"


def test_dropin_paths_da_systemctl(monkeypatch):
    calls = _fake_systemctl(
        monkeypatch,
        stdout="/etc/systemd/user/klamav-scan.service.d/10-a.conf "
        "/home/u/.config/systemd/user/klamav-scan.service.d/override.conf\n",
    )
    assert sd.dropin_paths() == [
        Path("/etc/systemd/user/klamav-scan.service.d/10-a.conf"),
        Path("/home/u/.config/systemd/user/klamav-scan.service.d/override.conf"),
    ]
    assert calls == [("show", "-p", "DropInPaths", "--value", "klamav-scan.service")]


@pytest.mark.parametrize("kw", [dict(missing=True), dict(returncode=1)])
def test_dropin_paths_senza_manager(monkeypatch, kw):
    _fake_systemctl(monkeypatch, **kw)
    assert sd.dropin_paths() is None
