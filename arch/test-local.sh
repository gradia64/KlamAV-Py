#!/usr/bin/env bash
# Test locale del pacchetto Arch senza una macchina Arch: build makepkg
# + installazione + smoke test dentro un container archlinux:latest.
# È lo stesso flusso con cui il PKGBUILD è stato validato.
#
# Uso (dalla radice del repo o da qualunque directory):
#   arch/test-local.sh                     build + install + smoke test CLI
#                                          dall'ALBERO DI LAVORO
#   arch/test-local.sh --tag               build dal TAG FIRMATO su GitHub,
#                                          PKGBUILD non modificato: è il
#                                          percorso reale di un utente AUR,
#                                          verifica della firma compresa
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
#     volo (source al tarball locale), quindi questo percorso non
#     verifica né il tag né la sua firma.
#
#   - dal tag (--tag): usa il PKGBUILD esattamente com'è, quindi clona
#     davvero da GitHub e verifica la firma del tag contro validpgpkeys.
#     È l'unico modo di accorgersi che il tag non contiene un file che
#     package() installa, che non è stato pubblicato, che non è firmato o
#     che è firmato con la chiave sbagliata. Da usare prima di pubblicare
#     su AUR.
#
# La chiave pubblica per la verifica viene da arch/klamav-py-release-key.asc,
# non dal keyserver: il test non dipende dalla rete per la chiave. Il file
# non è un punto di fiducia: makepkg accetta la firma solo se la chiave ha
# l'impronta scritta in validpgpkeys, qualunque cosa contenga il file.

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
    # Percorso REALE di un utente AUR: makepkg clona il tag da GitHub e ne
    # verifica la firma, con il PKGBUILD NON modificato.
    # L'output di makepkg non è silenziato di proposito: il clone e la
    # verifica della firma sono esattamente ciò che si vuole vedere.
    KEYFILE="$REPO/arch/klamav-py-release-key.asc"
    FPR=$(sed -n "s/^validpgpkeys=('\([0-9A-F]\{40\}\)').*/\1/p" arch/PKGBUILD)
    [ -n "$FPR" ] || { echo "ERRORE: validpgpkeys non trovato nel PKGBUILD" >&2; exit 1; }
    [ -f "$KEYFILE" ] || { echo "ERRORE: manca $KEYFILE (gpg --armor --export $FPR)" >&2; exit 1; }

    echo "==> Build dal tag firmato v$PKGVER (percorso reale AUR), PKGBUILD non modificato"
    docker run --rm \
        -e KLAMAV_FPR="$FPR" \
        -v "$REPO/arch/PKGBUILD":/src/PKGBUILD:ro \
        -v "$REPO/arch/klamav-py.install":/src/klamav-py.install:ro \
        -v "$KEYFILE":/src/release-key.asc:ro \
        archlinux:latest bash -c '
set -e
echo "==> Installo build toolchain"
pacman -Sy --noconfirm git python-build python-installer python-wheel \
    python-setuptools fakeroot debugedit >/dev/null 2>&1

mkdir /work && cd /work
cp /src/PKGBUILD /src/klamav-py.install .
useradd -m builder && chown -R builder /work

echo "==> Importo la chiave di rilascio nel portachiavi di builder"
su builder -c "gpg --batch --quiet --import /src/release-key.asc"
# Errore leggibile subito, invece del "chiave pubblica sconosciuta" di
# makepkg: il file deve contenere la chiave con l impronta di validpgpkeys.
su builder -c "gpg --batch --list-keys $KLAMAV_FPR" >/dev/null 2>&1 || {
    echo "ERRORE: arch/klamav-py-release-key.asc non contiene la chiave $KLAMAV_FPR" >&2
    exit 1
}

echo "==> makepkg: clona il tag e verifica la firma"
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

echo "==> Man page nel pacchetto (compresse da makepkg)"
# Non si controlla /usr/share/man: l immagine archlinux ha NoExtract su
# usr/share/man/* in pacman.conf, quindi pacman -U non le estrae. Si
# controlla il contenuto del pacchetto, cioè ciò che riceve un utente.
n=$(pacman -Qlp klamav-py-*.pkg.tar.zst | grep -cE "usr/share/man/(it/)?man1/klamav-py(-gui)?[.]1[.]gz$")
echo "$n man page nel pacchetto"
[ "$n" -eq 4 ] || { echo "ERRORE: attese 4 man page (2 pagine x 2 lingue)" >&2; exit 1; }

echo
echo "=== BUILD DAL TAG RIUSCITA: il pacchetto AUR è pubblicabile ==="
'
    exit 0
fi

TARBALL=/tmp/klamav-py-$VER.tar.gz
echo "==> Creo il tarball dell'albero di lavoro: $TARBALL"
tar --exclude=venv --exclude=.git --exclude=build --exclude=.pybuild \
    --exclude='debian/klamav-py' --exclude='debian/.debhelper' \
    --exclude='klamav_py.egg-info' --exclude=arch --exclude='*.tar.gz' \
    -czf "$TARBALL" --transform "s,^\.,klamav-py," .

# Il PKGBUILD viene patchato solo in source: la sorgente git firmata
# diventa il tarball dell'albero di lavoro. La directory radice del
# tarball è "klamav-py" perché build() e package() fanno cd in
# "$srcdir/$pkgname", dove il PKGBUILD reale mette il clone git.
# sha256sums resta SKIP (è già così nel PKGBUILD), e validpgpkeys non ha
# effetto: un tarball locale non ha firma da verificare.
patch_pkgbuild() {
    sed -e "s|^source=(.*|source=($1)|" arch/PKGBUILD
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
patch_pkgbuild "file:///src/klamav-py-$VER.tar.gz" > "$PKGBUILD_TEST"

GUI=${KLAMAV_TEST_GUI:-0}
SHELL_MODE=0
[ "$MODE" = "--shell" ] && SHELL_MODE=1
DOCKER_FLAGS=()
[ "$SHELL_MODE" = 1 ] && DOCKER_FLAGS+=(-ti)

echo "==> Avvio il container Arch (la prima run scarica l'immagine, ~170MB)"
docker run --rm "${DOCKER_FLAGS[@]}" \
    -e KLAMAV_TEST_GUI="$GUI" -e KLAMAV_TEST_SHELL="$SHELL_MODE" \
    -v "$TARBALL":/src/klamav-py-$VER.tar.gz:ro \
    -v "$PKGBUILD_TEST":/src/PKGBUILD:ro \
    -v "$REPO/arch/klamav-py.install":/src/klamav-py.install:ro \
    archlinux:latest bash -c '
set -e
echo "==> Installo build toolchain"
# git non serve al tarball locale, ma è in makedepends e makepkg senza
# -s rifiuta di partire se una dipendenza di build manca.
pacman -Sy --noconfirm git python-build python-installer python-wheel \
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

echo "==> Man page nel pacchetto (compresse da makepkg)"
# Non si controlla /usr/share/man: l immagine archlinux ha NoExtract su
# usr/share/man/* in pacman.conf, quindi pacman -U non le estrae. Si
# controlla il contenuto del pacchetto, cioè ciò che riceve un utente.
n=$(pacman -Qlp klamav-py-*.pkg.tar.zst | grep -cE "usr/share/man/(it/)?man1/klamav-py(-gui)?[.]1[.]gz$")
echo "$n man page nel pacchetto"
[ "$n" -eq 4 ] || { echo "ERRORE: attese 4 man page (2 pagine x 2 lingue)" >&2; exit 1; }

if [ "$KLAMAV_TEST_GUI" = "1" ]; then
    echo "==> Installo pyside6 (download pesante, qualche minuto)"
    pacman -S --noconfirm pyside6 >/dev/null 2>&1
    python -c "from klamav_py.gui import app, main_window, scan_worker, freshclam_restart_worker, db_info_worker, ping_worker; print(\"import moduli GUI: ok\")"
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
