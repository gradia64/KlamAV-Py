"""
Script del workflow di rilascio (tools/, .github/workflows/release.yml).

Il workflow si fida di questi script per tre decisioni che non si possono
sbagliare: pubblicare solo da un tag firmato dalla sottochiave dei tag del
maintainer (non da una qualunque della chiave di rilascio: la sottochiave
della CI non deve poter firmare un tag valido), firmare gli allegati solo
con la sottochiave della CI, e non pubblicare su AUR un .SRCINFO che non
descrive il PKGBUILD. Ruoli in tools/release-keys.sh.
Qui si provano con chiavi e repository temporanei, mai con quelli reali:
GNUPGHOME e HOME puntano sempre in tmp_path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from klamav_py import __version__

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"

pytestmark = pytest.mark.skipif(
    not (shutil.which("gpg") and shutil.which("git") and shutil.which("bash")),
    reason="servono gpg, git e bash",
)

PASSPHRASE = "segreta di prova"


def _env(tmp_path: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GPG_", "GNUPG", "GIT_"))}
    env.update(HOME=str(tmp_path), LC_ALL="C", **extra)
    return env


def _run(cmd, tmp_path, cwd=None, check=True, **extra):
    proc = subprocess.run(cmd, cwd=cwd, env=_env(tmp_path, **extra),
                          capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise AssertionError(f"{cmd} -> {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
    return proc


class Keyring:
    """Portachiavi temporaneo con una chiave «di rilascio» come quella vera
    (primaria solo certificazione, una sottochiave per i tag e una per la
    CI) e una chiave estranea."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.home = tmp_path / "gnupg"
        self.home.mkdir(mode=0o700)
        self.release = self._new_key("Rilascio di prova <rilascio@example.org>", subkey=True)
        self.gpg("--quick-add-key", self.release, "ed25519", "sign", "1y")
        self.tag_subkey, self.ci_subkey = self._fprs(self.release)[1:3]
        self.other = self._new_key("Estranea <estranea@example.org>", subkey=False)
        self.pubkey = tmp_path / "release-key.asc"
        self.pubkey.write_text(self.gpg("--armor", "--export", self.release).stdout)

    def gpg(self, *args, check=True):
        return _run(["gpg", "--homedir", str(self.home), "--batch", "--pinentry-mode", "loopback",
                     "--passphrase", PASSPHRASE, *args], self.tmp, check=check)

    def _new_key(self, uid, subkey):
        self.gpg("--quick-gen-key", uid, "ed25519", "cert" if subkey else "sign,cert", "1y")
        fpr = self._fprs(uid)[0]
        if subkey:
            self.gpg("--quick-add-key", fpr, "ed25519", "sign", "1y")
        return fpr

    def _fprs(self, uid):
        out = self.gpg("--with-colons", "--list-keys", uid).stdout
        return [l.split(":")[9] for l in out.splitlines() if l.startswith("fpr:")]

    def subkey_secret(self, sub) -> str:
        """Come nel secret della CI: solo quella sottochiave."""
        return self.gpg("--armor", "--export-secret-subkeys", f"{sub}!").stdout

    def roles(self) -> dict:
        """Ruoli della chiave di prova per tools/release-keys.sh."""
        return {"KLAMAV_RELEASE_PRIMARY": self.release,
                "KLAMAV_TAG_SIGNING_SUBKEYS": self.tag_subkey,
                "KLAMAV_CI_SIGNING_SUBKEY": self.ci_subkey}

    def close(self):
        subprocess.run(["gpgconf", "--homedir", str(self.home), "--kill", "gpg-agent"],
                       env=_env(self.tmp), capture_output=True)


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    # Una generazione per modulo: qualche secondo per chiave.
    k = Keyring(tmp_path_factory.mktemp("chiavi"))
    yield k
    k.close()


def _git(repo: Path) -> list:
    """git sul repository di prova, senza la configurazione dell'utente
    (firma automatica di commit e tag)."""
    return ["git", "-C", str(repo), "-c", "user.name=prova", "-c", "user.email=prova@example.org",
            "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


def _project(tmp_path: Path, keys: Keyring) -> Path:
    """Un repository minimo con gli script veri e la chiave di prova."""
    repo = tmp_path / "progetto"
    (repo / "tools").mkdir(parents=True)
    (repo / "arch").mkdir()
    for name in ("verify-tag.sh", "sign-release.sh", "publish-aur.sh", "release-keys.sh"):
        shutil.copy(TOOLS / name, repo / "tools" / name)
    shutil.copy(keys.pubkey, repo / "arch" / "klamav-py-release-key.asc")
    (repo / "README").write_text("x")
    _run(["git", "init", "-q", str(repo)], tmp_path)
    _run([*_git(repo), "add", "."], tmp_path)
    _run([*_git(repo), "commit", "-q", "-m", "inizio"], tmp_path)
    return repo


def _tag(repo, tmp_path, keys, name, *, key=None, annotated=True):
    if not annotated:
        args = [name]
    elif key is None:
        args = ["-a", "-m", name, name]
    else:
        args = ["-s", "-u", key, "-m", name, name]
    _run([*_git(repo), "-c", f"gpg.program={_gpg_wrapper(tmp_path, keys)}", "tag", *args], tmp_path)


def _gpg_wrapper(tmp_path, keys) -> Path:
    wrapper = tmp_path / "gpg-prova"
    wrapper.write_text(
        f'#!/bin/sh\nexec gpg --homedir "{keys.home}" --pinentry-mode loopback '
        f'--passphrase "{PASSPHRASE}" "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


# -- tools/verify-tag.sh -------------------------------------------------------

def test_tag_firmato_dalla_sottochiave_dei_tag_accettato(tmp_path, keys):
    repo = _project(tmp_path, keys)
    _tag(repo, tmp_path, keys, "v1.0.0", key=f"{keys.tag_subkey}!")
    proc = _run(["tools/verify-tag.sh", "v1.0.0"], tmp_path, cwd=repo, **keys.roles())
    assert f"firmato dal maintainer (sottochiave {keys.tag_subkey}" in proc.stdout


def test_tag_firmato_dalla_sottochiave_della_ci_rifiutato(tmp_path, keys):
    # La firma è valida e la primaria è quella di rilascio: è proprio il caso
    # di una CI compromessa che firma un tag. Non deve passare.
    repo = _project(tmp_path, keys)
    _tag(repo, tmp_path, keys, "v1.0.0", key=f"{keys.ci_subkey}!")
    proc = _run(["tools/verify-tag.sh", "v1.0.0"], tmp_path, cwd=repo, check=False, **keys.roles())
    assert proc.returncode != 0
    assert f"firmato da {keys.ci_subkey}, che non è una sottochiave dei tag" in proc.stderr


@pytest.mark.parametrize("caso", ["altra chiave", "annotato senza firma", "leggero"])
def test_tag_non_valido_rifiutato(tmp_path, keys, caso):
    repo = _project(tmp_path, keys)
    if caso == "altra chiave":
        _tag(repo, tmp_path, keys, "v1.0.0", key=keys.other)
    else:
        _tag(repo, tmp_path, keys, "v1.0.0", annotated=(caso != "leggero"))
    proc = _run(["tools/verify-tag.sh", "v1.0.0"], tmp_path, cwd=repo, check=False, **keys.roles())
    assert proc.returncode != 0
    assert "firmato dal maintainer" not in proc.stdout


# -- tools/sign-release.sh -----------------------------------------------------

def _dist(tmp_path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "klamav-py_1.0.0_all.deb").write_bytes(b"deb")
    (dist / "klamav-py-1.0.0-1-any.pkg.tar.zst").write_bytes(b"arch")
    return dist


def test_firma_con_la_sottochiave_della_ci(tmp_path, keys):
    repo = _project(tmp_path, keys)
    dist = _dist(tmp_path)
    _run(["tools/sign-release.sh", str(dist)], tmp_path, cwd=repo, **keys.roles(),
         GPG_PRIVATE_KEY=keys.subkey_secret(keys.ci_subkey), GPG_PASSPHRASE=PASSPHRASE)
    names = sorted(p.name for p in dist.iterdir())
    assert names == sorted([
        "klamav-py_1.0.0_all.deb", "klamav-py_1.0.0_all.deb.sig",
        "klamav-py-1.0.0-1-any.pkg.tar.zst", "klamav-py-1.0.0-1-any.pkg.tar.zst.sig",
        "SHA256SUMS", "SHA256SUMS.sig",
    ])
    sums = (dist / "SHA256SUMS").read_text()
    assert "klamav-py_1.0.0_all.deb" in sums and "pkg.tar.zst" in sums
    # Firmato proprio dalla sottochiave della CI, non da un'altra.
    status = keys.gpg("--status-fd", "1", "--verify", str(dist / "SHA256SUMS.sig"),
                      str(dist / "SHA256SUMS")).stdout
    assert f"VALIDSIG {keys.ci_subkey} " in status


@pytest.mark.parametrize("chiave", ["sottochiave dei tag", "chiave estranea"])
def test_firma_con_un_altra_chiave_rifiutata(tmp_path, keys, chiave):
    # Con la sola sottochiave dei tag nel secret (l'errore di configurazione
    # della prima versione) gli allegati non si firmano: i ruoli restano
    # separati anche se qualcuno sbaglia secret.
    repo = _project(tmp_path, keys)
    dist = _dist(tmp_path)
    if chiave == "sottochiave dei tag":
        secret = keys.subkey_secret(keys.tag_subkey)
    else:
        secret = keys.gpg("--armor", "--export-secret-keys", keys.other).stdout
    proc = _run(["tools/sign-release.sh", str(dist)], tmp_path, cwd=repo, check=False,
                **keys.roles(), GPG_PRIVATE_KEY=secret, GPG_PASSPHRASE=PASSPHRASE)
    assert proc.returncode != 0
    assert not list(dist.glob("*.sig"))


# -- tools/publish-aur.sh ------------------------------------------------------

def _aur_files(tmp_path, srcinfo_version="1.0.0") -> Path:
    aur = tmp_path / "aur"
    aur.mkdir()
    (aur / "PKGBUILD").write_text("pkgname=klamav-py\npkgver=1.0.0\npkgrel=1\n")
    (aur / "klamav-py.install").write_text("post_install() { :; }\n")
    (aur / ".SRCINFO").write_text(f"pkgbase = klamav-py\n\tpkgver = {srcinfo_version}\n")
    return aur


def test_pubblicazione_aur_idempotente(tmp_path, keys):
    repo = _project(tmp_path, keys)
    remote = tmp_path / "aur.git"
    _run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], tmp_path)
    aur = _aur_files(tmp_path)
    first = _run(["tools/publish-aur.sh", str(aur)], tmp_path, cwd=repo, AUR_REMOTE=str(remote))
    assert "pubblicato su AUR: klamav-py 1.0.0" in first.stdout
    files = _run(["git", "-C", str(remote), "ls-tree", "--name-only", "master"], tmp_path).stdout
    assert files.split() == [".SRCINFO", "PKGBUILD", "klamav-py.install"]
    again = _run(["tools/publish-aur.sh", str(aur)], tmp_path, cwd=repo, AUR_REMOTE=str(remote))
    assert "già aggiornato" in again.stdout


def test_srcinfo_non_allineato_non_pubblicato(tmp_path, keys):
    repo = _project(tmp_path, keys)
    aur = _aur_files(tmp_path, srcinfo_version="0.9.0")
    proc = _run(["tools/publish-aur.sh", str(aur)], tmp_path, cwd=repo, check=False,
                AUR_REMOTE=str(tmp_path / "non-esiste.git"))
    assert proc.returncode != 0 and ".SRCINFO non allineato" in proc.stderr


# -- versione e note: sul repository vero ----------------------------------------

def test_versione_allineata_al_tag(tmp_path):
    ok = _run([str(TOOLS / "check-version.sh"), f"v{__version__}"], tmp_path, cwd=ROOT)
    assert f"versione {__version__} allineata" in ok.stdout
    bad = _run([str(TOOLS / "check-version.sh"), "v99.0.0"], tmp_path, cwd=ROOT, check=False)
    assert bad.returncode != 0 and "klamav_py/__init__.py dice" in bad.stderr


def test_note_della_release_dal_changelog(tmp_path):
    notes = _run([str(TOOLS / "release-notes.sh"), __version__], tmp_path).stdout
    assert notes.startswith("### ") and not notes.startswith("## ")
    assert "---" not in notes.splitlines()
    # Solo la voce richiesta: nessuna intestazione di un'altra versione.
    assert not re.search(r"^## ", notes, re.M)
    missing = _run([str(TOOLS / "release-notes.sh"), "99.0.0"], tmp_path, check=False)
    assert missing.returncode != 0


# -- il workflow usa questi script e non firma i tag ---------------------------

def _jobs(text: str) -> dict:
    """Testo di ogni job di release.yml, per nome (indentazione a 2 spazi)."""
    body = text[text.index("\njobs:\n"):]
    parts = re.split(r"\n  ([\w-]+):\n", body)
    return dict(zip(parts[1::2], parts[2::2]))


_BLOCK_SCALAR = re.compile(r"""(?:^|[:\-\s])[|>][0-9+-]*\s*(?:#.*)?$""")


def _strip_comment(line: str) -> str:
    """La riga senza il commento YAML: «#» a inizio riga, o preceduto da
    uno spazio fuori da apici e da ${{ … }}. Dentro una stringa
    («echo "nota #1 ${{ secrets.X }}"») non è un commento."""
    quote = None
    expr = 0
    i = 0
    while i < len(line):
        c = line[i]
        if expr:
            if line.startswith("}}", i):
                expr -= 1
                i += 2
                continue
        elif quote == "'":
            if c == "'":
                quote = None
        elif quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quote = None
        elif c == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i].rstrip()
        elif c in "'\"" and (i == 0 or line[i - 1] in " \t:-[{,"):
            quote = c
        if line.startswith("${{", i):
            expr += 1
            i += 3
            continue
        i += 1
    return line


def _without_comments(text: str) -> str:
    """Il testo di un workflow senza i commenti YAML. Le righe di un blocco
    letterale («run: |») restano intere: lì «#» è testo per la shell, e
    GitHub sostituisce comunque le espressioni ${{ … }} che contiene."""
    out = []
    block = None  # indentazione della chiave che ha aperto il blocco
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip())
        if block is not None:
            if not line.strip() or indent > block:
                out.append(line)
                continue
            block = None
        if line.lstrip().startswith("#"):
            continue
        stripped = _strip_comment(line)
        if _BLOCK_SCALAR.search(stripped):
            block = indent
        out.append(stripped)
    return "\n".join(out)


_SECRETS = re.compile(r"\bsecrets\b")


def secret_readers(workflows: dict[str, str]) -> list[str]:
    """«file:job» di ogni lettura dei secret fuori dal job publish di
    release.yml nell'environment release: un altro job, un altro workflow,
    o il livello del workflow (env: globale, valido per tutti i job).
    Compresi `secrets: inherit` e `toJSON(secrets)`."""
    found = []
    for name, text in sorted(workflows.items()):
        text = "\n" + _without_comments(text)
        if "\njobs:\n" not in text:
            if _SECRETS.search(text):
                found.append(f"{name}:<workflow>")
            continue
        if _SECRETS.search(text[:text.index("\njobs:\n")]):
            found.append(f"{name}:<workflow>")
        for job, body in _jobs(text).items():
            if not _SECRETS.search(body):
                continue
            if name == "release.yml" and job == "publish" and \
                    re.search(r"^    environment: release$", body, re.M):
                continue
            found.append(f"{name}:{job}")
    return found


_PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")


def unpinned_actions(workflows: dict[str, str]) -> list[str]:
    """«file: action» di ogni action esterna non fissata per SHA, in tutti
    i workflow (0.1.16: prima solo release.yml). Unica eccezione le action
    e i workflow locali (./)."""
    found = []
    for name, text in sorted(workflows.items()):
        for ref in re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", _without_comments(text), re.M):
            ref = ref.strip("'\"")
            if not ref.startswith("./") and not _PINNED.fullmatch(ref):
                found.append(f"{name}: {ref}")
    return found


# Nessun job firma tag o usa la chiave primaria (solo gli allegati); nessun
# workflow imposta gli override KLAMAV_* di tools/release-keys.sh, che
# farebbero accettare a verify-tag.sh un tag firmato da un'altra
# sottochiave, quella della CI compresa.
_FORBIDDEN = ("git tag", "export-secret-keys", "KLAMAV_")


def forbidden_mentions(workflows: dict[str, str]) -> list[str]:
    """«file: testo» di ogni uso vietato, commenti esclusi."""
    return [f"{name}: {word}" for name, text in sorted(workflows.items())
            for word in _FORBIDDEN if word in _without_comments(text)]


def _workflows() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8")
            for p in sorted((ROOT / ".github/workflows").glob("*.y*ml"))}


def test_workflow_verifica_il_tag_e_isola_i_secret():
    workflows = _workflows()
    text = workflows["release.yml"]
    jobs = _jobs(text)
    assert "tools/verify-tag.sh" in jobs["verify"] and "tools/check-version.sh" in jobs["verify"]
    assert forbidden_mentions(workflows) == []
    # I secret solo nel job publish, nell'environment protetto, in tutti i
    # workflow (0.1.15: prima solo i job di release.yml).
    assert "secrets." in jobs["publish"]
    assert secret_readers(workflows) == []
    # Ogni action esterna è fissata per SHA, in ogni workflow: tests.yml
    # gira anche dentro release.yml e decide i test da cui dipende il
    # rilascio.
    assert unpinned_actions(workflows) == []


@pytest.mark.parametrize("workflows", [
    # Un altro job di release.yml.
    {"release.yml": "jobs:\n  build:\n    steps:\n      - run: echo ${{ secrets.GPG_PASSPHRASE }}\n"},
    # publish senza l'environment protetto.
    {"release.yml": "jobs:\n  publish:\n    steps:\n      - env:\n"
                    "          K: ${{ secrets.GPG_PRIVATE_KEY }}\n"},
    # Un altro workflow, anche con secrets: inherit verso uno riutilizzabile.
    {"tests.yml": "on: push\njobs:\n  test:\n    uses: ./x.yml\n    secrets: inherit\n"},
    # env: a livello di workflow, visibile a tutti i job.
    {"tests.yml": "on: push\nenv:\n  K: ${{ secrets.AUR_SSH_PRIVATE_KEY }}\njobs:\n"
                  "  test:\n    steps: []\n"},
    # « #» dentro una stringa non apre un commento (0.1.16: prima sì).
    {"tests.yml": 'on: push\njobs:\n  test:\n    steps:\n'
                  '      - run: echo "nota #1 ${{ secrets.X }}"\n'},
    {"tests.yml": "on: push\njobs:\n  test:\n    steps:\n"
                  "      - run: echo 'nota #1 ${{ secrets.X }}'\n"},
    # In un blocco letterale « #» è testo per la shell, e GitHub sostituisce
    # comunque l'espressione.
    {"tests.yml": "on: push\njobs:\n  test:\n    steps:\n      - run: |\n"
                  "          echo ok  # ${{ secrets.X }}\n"
                  "          # ${{ secrets.Y }}\n"},
], ids=["altro-job", "senza-environment", "altro-workflow", "env-globale",
        "stringa-doppia", "stringa-singola", "blocco-letterale"])
def test_controllo_dei_secret_vede_le_letture_fuori_da_publish(workflows):
    assert secret_readers(workflows), workflows


def test_controllo_dei_secret_accetta_publish_e_commenti():
    workflows = {
        "release.yml": "# i secret GPG_PRIVATE_KEY e secrets.X nei commenti non contano\n"
                       "jobs:\n  verify:\n    steps: []\n  publish:\n"
                       "    environment: release\n    steps:\n      - env:\n"
                       "          K: ${{ secrets.GPG_PRIVATE_KEY }}  # firma\n",
        # Un commento vero dopo una direttiva non viene letto da GitHub.
        "tests.yml": "on: push\njobs:\n  test:\n    steps:\n"
                     "      - run: echo ok  # ${{ secrets.X }}\n"
                     '      - run: "echo #1"  # ${{ secrets.Y }}\n',
    }
    assert secret_readers(workflows) == []


def test_action_per_tag_mobile_rifiutata_in_ogni_workflow():
    workflows = {
        "release.yml": "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@"
                       + "a" * 40 + "  # v7.0.1\n",
        "altro.yml": "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v5\n"
                     "      - uses: 'actions/setup-python@v6'\n"
                     "      - name: x\n        uses: owner/repo/sub@main\n",
    }
    assert unpinned_actions(workflows) == [
        "altro.yml: actions/checkout@v5", "altro.yml: actions/setup-python@v6",
        "altro.yml: owner/repo/sub@main"]


def test_action_locali_e_commenti_non_contano():
    workflows = {"tests.yml": "jobs:\n  a:\n    uses: ./.github/workflows/x.yml\n"
                              "  b:\n    steps:\n      # - uses: actions/checkout@v5\n"
                              "      - uses: actions/checkout@" + "b" * 40 + "  # v5\n"}
    assert unpinned_actions(workflows) == []


def test_usi_vietati_visti_fuori_dai_commenti():
    assert forbidden_mentions({"release.yml": "jobs:\n  a:\n    steps:\n"
                                              "      - run: git tag -s v1\n"}) == [
        "release.yml: git tag"]
    assert forbidden_mentions({"tests.yml": "env:\n  KLAMAV_CI_SIGNING_SUBKEY: X\n"}) == [
        "tests.yml: KLAMAV_"]
    assert forbidden_mentions({"tests.yml": "jobs:\n  a:\n    steps:\n      - run: |\n"
                                            "          # git tag -s v1\n"}) == [
        "tests.yml: git tag"]


def test_usi_vietati_nei_commenti_ammessi():
    workflows = {"release.yml": "# il tag lo firma il maintainer con git tag -s,\n"
                                "# gli override KLAMAV_* restano ai test\n"
                                "jobs:\n  a:\n    steps:\n"
                                "      - run: echo ok  # niente git tag qui\n"}
    assert forbidden_mentions(workflows) == []


def test_ruoli_delle_chiavi_coerenti_con_la_chiave_pubblica(tmp_path):
    # tools/release-keys.sh e arch/klamav-py-release-key.asc devono parlare
    # della stessa chiave: due sottochiavi di firma distinte, ognuna con il
    # suo ruolo, e la primaria che non firma.
    roles = dict(re.findall(r'^(\w+)="\$\{KLAMAV_\w+:-([0-9A-F ]+)\}"',
                            (TOOLS / "release-keys.sh").read_text(), re.M))
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700)
    out = _run(["gpg", "--homedir", str(gnupg), "--batch", "--with-colons", "--show-keys",
                str(ROOT / "arch/klamav-py-release-key.asc")], tmp_path).stdout
    rows = [l.split(":") for l in out.splitlines()]
    keys, kind = {}, None
    for r in rows:
        if r[0] in ("pub", "sub"):
            kind = (r[0], r[11])
        elif r[0] == "fpr" and kind:
            keys[r[9]] = kind
            kind = None
    primary = roles["RELEASE_PRIMARY"]
    tag_subkeys = roles["TAG_SIGNING_SUBKEYS"].split()
    ci = roles["CI_SIGNING_SUBKEY"]
    assert keys[primary][0] == "pub" and "s" not in keys[primary][1]
    for sub in (*tag_subkeys, ci):
        assert keys[sub] == ("sub", "s")
    assert ci not in tag_subkeys
    # L'impronta di validpgpkeys è la primaria.
    assert primary in (ROOT / "arch/PKGBUILD").read_text()
