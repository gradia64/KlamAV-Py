#!/usr/bin/env bash
# Test locale del pacchetto Arch senza una macchina Arch: build makepkg
# + installazione + smoke test dentro un container archlinux:latest.
# È lo stesso flusso con cui il PKGBUILD è stato validato.
#
# Uso (dalla radice del repo o da qualunque directory):
#   arch/test-local.sh                     build + install + smoke test CLI
#                                          dall'ALBERO DI LAVORO
#   arch/test-local.sh --tag               build dal TAG GitHub, PKGBUILD
#                                          non modificato: è il percorso
#                                          reale di un utente AUR
#   KLAMAV_TEST_GUI=1 arch/test-local.sh   installa anche pyside6 e verifica
#                                          import + avvio GUI (offscreen)
#   arch/test-local.sh --shell             apre una shell nel container
#                                          dopo i test (per poking manuale)
#   arch/test-local.sh --tarball           prepara /tmp/klamav-py-aur-test
#                                          (tarball + PKGBUILD patchato +
#                                          .install) da copiare su una vera
#                                          macchina Arch/VM e lì: makepkg -si
#
# Perché due percorsi diversi, e perché servono entrambi:
#
#   - dall'albero di lavoro (default, --shell, --tarball): fotografa lo
#     stato corrente, comprese le modifiche non ancora committate. È ciò
#     che serve mentre si sviluppa. Il PKGBUILD viene però PATCHATO al
#     volo (source locale e sha256sums=SKIP), quindi questo percorso non
#     verifica né l'URL del tag né il checksum.
#
#   - dal tag (--tag): usa il PKGBUILD esattamente com'è, quindi scarica
#     davvero da GitHub e verifica il sha256. È l'unico modo di
#     accorgersi che il tag non contiene un file che package() installa,
#     che l'URL ha il prefisso "v" sbagliato, o che sha256sums è rimasto
#     indietro rispetto al tag. Da usare prima di pubblicare su AUR.

set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

MODE="${1:-}"

if [ "$MODE" != "--tarball" ]; then
    command -v docker >/dev/null || { echo "ERRORE: docker non trovato (serve per il test senza macchina Arch)" >&2; exit 1; }
fi

VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' klamav_py/__init__.py)
PKGVER=$(sed -n 's/^pkgver=//p' arch/PKGBUILD)
if [ "$VER" != "$PKGVER" ]; then
    if [ "$MODE" = "--tag" ]; then
        echo "ERRORE: __init__.py=$VER ma PKGBUILD pkgver=$PKGVER." >&2
        echo "In modalità --tag si costruisce dal tag v$PKGVER: allineare prima le versioni." >&2
        exit 1
    fi
    echo "ATTENZIONE: __init__.py=$VER ma PKGBUILD pkgver=$PKGVER: il test usa $VER" >&2
fi

if [ "$MODE" = "--tag" ]; then
    # Percorso REALE di un utente AUR: makepkg scarica il tarball dal tag
    # GitHub e ne verifica il sha256, con il PKGBUILD NON modificato.
    # L'output di makepkg non è silenziato di proposito: il download e la
    # verifica del checksum sono esattamente ciò che si vuole vedere.
    echo "==> Build dal tag GitHub v$PKGVER (percorso reale AUR), PKGBUILD non modificato"
    docker run --rm \
        -v "$REPO/arch/PKGBUILD":/src/PKGBUILD:ro \
        -v "$REPO/arch/klamav-py.install":/src/klamav-py.install:ro \
        archlinux:latest bash -c '
set -e
echo "==> Installo build toolchain"
pacman -Sy --noconfirm python-build python-installer python-wheel \
    python-setuptools fakeroot debugedit >/dev/null 2>&1

mkdir /work && cd /work
cp /src/PKGBUILD /src/klamav-py.install .
useradd -m builder && chown -R builder /work

echo "==> makepkg: scarica dal tag e verifica il checksum"
su builder -c "makepkg -f"

echo "==> pacman -U (installazione)"
pacman -U --noconfirm klamav-py-*.pkg.tar.zst >/dev/null

echo "==> Smoke test CLI"
klamav-py --version

echo "==> Unit systemd utente installate"
ls /usr/lib/systemd/user/ | grep klamav

echo "==> Icona e desktop entry"
ls -l /usr/share/icons/hicolor/scalable/apps/klamav-py.svg
grep ^Icon= /usr/share/applications/klamav-py.desktop

echo
echo "=== BUILD DAL TAG RIUSCITA: il pacchetto AUR è pubblicabile ==="
'
    exit 0
fi

TARBALL=/tmp/KlamAV-Py-$VER.tar.gz
echo "==> Creo il tarball dell'albero di lavoro: $TARBALL"
tar --exclude=venv --exclude=.git --exclude=build --exclude=.pybuild \
    --exclude='debian/klamav-py' --exclude='debian/.debhelper' \
    --exclude='klamav_py.egg-info' --exclude=arch --exclude='*.tar.gz' \
    -czf "$TARBALL" --transform "s,^\.,KlamAV-Py-$VER," .

# Il PKGBUILD va patchato in due punti, non uno solo: sostituire soltanto
# source lascerebbe il sha256sums del tarball di GitHub, che non può
# corrispondere a un tarball generato ora dall'albero di lavoro. SKIP qui
# è legittimo: il file è stato creato in questo stesso script.
patch_pkgbuild() {
    sed -e "s|^source=(.*|source=($1)|" \
        -e "s|^sha256sums=(.*|sha256sums=('SKIP')|" \
        arch/PKGBUILD
}

if [ "$MODE" = "--tarball" ]; then
    # Directory già pronta da copiare sulla macchina Arch: PKGBUILD con
    # source relativa (makepkg risolve i nomi semplici rispetto alla
    # directory del PKGBUILD), tarball e file .install insieme.
    OUTDIR=/tmp/klamav-py-aur-test
    rm -rf "$OUTDIR" && mkdir -p "$OUTDIR"
    cp "$TARBALL" "$OUTDIR/"
    patch_pkgbuild "\"$(basename "$TARBALL")\"" > "$OUTDIR/PKGBUILD"
    cp arch/klamav-py.install "$OUTDIR/"
    echo
    echo "Directory pronta: $OUTDIR"
    ls -la "$OUTDIR"
    echo
    echo "Copiala sulla macchina Arch (scp/USB), poi lì, da utente normale:"
    echo "  cd klamav-py-aur-test && makepkg -si"
    exit 0
fi

# Variante del PKGBUILD con source locale al tarball appena creato
PKGBUILD_TEST=/tmp/PKGBUILD-test
patch_pkgbuild "file:///src/KlamAV-Py-$VER.tar.gz" > "$PKGBUILD_TEST"

GUI=${KLAMAV_TEST_GUI:-0}
SHELL_MODE=0
[ "$MODE" = "--shell" ] && SHELL_MODE=1
DOCKER_FLAGS=()
[ "$SHELL_MODE" = 1 ] && DOCKER_FLAGS+=(-ti)

echo "==> Avvio il container Arch (la prima run scarica l'immagine, ~170MB)"
docker run --rm "${DOCKER_FLAGS[@]}" \
    -e KLAMAV_TEST_GUI="$GUI" -e KLAMAV_TEST_SHELL="$SHELL_MODE" \
    -v "$TARBALL":/src/KlamAV-Py-$VER.tar.gz:ro \
    -v "$PKGBUILD_TEST":/src/PKGBUILD:ro \
    -v "$REPO/arch/klamav-py.install":/src/klamav-py.install:ro \
    archlinux:latest bash -c '
set -e
echo "==> Installo build toolchain"
pacman -Sy --noconfirm python-build python-installer python-wheel \
    python-setuptools fakeroot debugedit >/dev/null 2>&1

mkdir /work && cd /work
cp /src/PKGBUILD /src/klamav-py.install .
useradd -m builder && chown -R builder /work

echo "==> makepkg (utente non-root, come su AUR)"
su builder -c "makepkg -f" >/dev/null

echo "==> pacman -U (installazione)"
pacman -U --noconfirm klamav-py-*.pkg.tar.zst >/dev/null

echo "==> Smoke test CLI"
klamav-py --version

echo "==> Unit systemd utente installate"
ls /usr/lib/systemd/user/ | grep klamav

if [ "$KLAMAV_TEST_GUI" = "1" ]; then
    echo "==> Installo pyside6 (download pesante, qualche minuto)"
    pacman -S --noconfirm pyside6 >/dev/null 2>&1
    python -c "from klamav_py.gui import app, main_window, scan_worker, update_worker, ping_worker; print(\"import moduli GUI: ok\")"
    set +e
    QT_QPA_PLATFORM=offscreen timeout 10 klamav-py-gui >/dev/null 2>&1
    rc=$?
    set -e
    if [ "$rc" -eq 124 ]; then
        echo "avvio GUI offscreen: ok (resta attiva finché il timeout la interrompe)"
    else
        echo "avvio GUI offscreen: USCITA IMMEDIATA rc=$rc (verificare)"
        exit 1
    fi
else
    echo "==> GUI non testata (usa KLAMAV_TEST_GUI=1 per testarla)"
fi

echo
echo "=== TEST COMPLETATI: pacchetto valido ==="

if [ "$KLAMAV_TEST_SHELL" = "1" ]; then
    echo "=== Shell nel container (il pacchetto è installato in /usr) ==="
    exec bash
fi
'
