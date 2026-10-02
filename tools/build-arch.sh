#!/bin/bash
# Pacchetto Arch dal PKGBUILD del repository, esattamente com'è: sorgente
# git dal tag firmato su GitHub, firma verificata da makepkg contro
# validpgpkeys. È il percorso di un utente AUR (come «arch/test-local.sh
# --tag»), quindi il tag v$pkgver deve essere già pubblicato.
#
# Da eseguire come root in un container archlinux:base-devel (CI o locale):
#   docker run --rm -v "$PWD":/src -w /src archlinux:base-devel tools/build-arch.sh
#
# - installa le dipendenze lette dal PKGBUILD (makepkg non può usare sudo);
# - costruisce come utente non privilegiato, con la sola chiave pubblica
#   di rilascio nel portachiavi (arch/klamav-py-release-key.asc: makepkg
#   accetta comunque solo l'impronta di validpgpkeys);
# - namcap: gli errori bloccano, gli avvisi no;
# - rigenera .SRCINFO (al posto di tools/srcinfo.sh, che serve docker);
# - installa il pacchetto e prova la CLI.
#
# Risultato in build/aur/: PKGBUILD, klamav-py.install, .SRCINFO (i file da
# pubblicare su AUR) e il pacchetto .pkg.tar.zst.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="build/aur"

if [ "$(id -u)" -ne 0 ]; then
    echo "da eseguire come root in un container Arch (vedi l'intestazione)" >&2
    exit 1
fi

# shellcheck disable=SC1091,SC2154
DEPS="$(bash -c 'source arch/PKGBUILD; echo "${depends[@]} ${makedepends[@]}"')"
# shellcheck disable=SC2086
pacman -Syu --noconfirm --needed namcap $DEPS >/dev/null

id builder >/dev/null 2>&1 || useradd -m builder
WORK="$(mktemp -d)"
cp arch/PKGBUILD arch/klamav-py.install "$WORK/"
cp arch/klamav-py-release-key.asc "$WORK/release-key.asc"
chown -R builder: "$WORK"

su builder -s /bin/bash -c "
    set -euo pipefail
    cd '$WORK'
    gpg --batch --quiet --import release-key.asc
    makepkg --printsrcinfo > .SRCINFO
    makepkg -f --noconfirm
"

PKGFILE="$(ls "$WORK"/klamav-py-*-any.pkg.tar.zst)"
NAMCAP="$(namcap "$WORK/PKGBUILD" "$PKGFILE" || true)"
[ -n "$NAMCAP" ] && printf '%s\n' "$NAMCAP"
if printf '%s\n' "$NAMCAP" | grep -q ' E: '; then
    echo "namcap ha trovato errori" >&2
    exit 1
fi

mkdir -p "$OUT"
cp "$WORK/PKGBUILD" "$WORK/klamav-py.install" "$WORK/.SRCINFO" "$PKGFILE" "$OUT/"

echo '--- installazione di prova'
pacman -U --noconfirm "$PKGFILE" >/dev/null
version="$(sed -n 's/^pkgver=//p' arch/PKGBUILD)"
test "$(klamav-py --version)" = "klamav-py $version"
test -f /usr/lib/systemd/user/klamav-scan.service
echo "pacchetto Arch OK: $OUT/$(basename "$PKGFILE")"
