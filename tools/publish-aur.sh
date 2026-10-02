#!/bin/bash
# Pubblica PKGBUILD, klamav-py.install e .SRCINFO (da build/aur/, vedi
# tools/build-arch.sh) nel repository AUR del pacchetto «klamav-py».
#
# Chiave SSH dell'account AUR:
#   AUR_SSH_PRIVATE_KEY   chiave privata (secret della CI), scritta in un file
#                         temporaneo 0600 e cancellata alla fine
#   altrimenti            la configurazione SSH dell'utente (uso locale)
#
# La chiave host di aur.archlinux.org è fissata qui sotto: niente fiducia
# al primo contatto. Impronta verificata il 2026-10-02 contro quella
# pubblicata su https://aur.archlinux.org (ED25519
# SHA256:RFzBCUItH9LZS0cKB5UE6ceAYhBD5C8GeOBip8Z11+4).
#
# Uso: tools/publish-aur.sh [cartella]   (default build/aur)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
AUR="${1:-build/aur}"
AUR_REMOTE="${AUR_REMOTE:-ssh://aur@aur.archlinux.org/klamav-py.git}"   # override solo per i test
AUR_HOSTKEY="aur.archlinux.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEuBKrPzbawxA/k2g6NcyV5jmqwJ2s+zpgZGZ7tpLIcN"
FILES=(PKGBUILD klamav-py.install .SRCINFO)

for f in "${FILES[@]}"; do
    [ -f "$AUR/$f" ] || { echo "manca $AUR/$f (tools/build-arch.sh)" >&2; exit 1; }
done
VERSION="$(sed -n 's/^pkgver=//p' "$AUR/PKGBUILD")"
# .SRCINFO deve descrivere questo PKGBUILD: AUR mostra quello, non il PKGBUILD.
grep -qx "	pkgver = $VERSION" "$AUR/.SRCINFO" \
    || { echo ".SRCINFO non allineato al PKGBUILD ($VERSION)" >&2; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
printf '%s\n' "$AUR_HOSTKEY" > "$TMP/known_hosts"
SSH_CMD="ssh -o UserKnownHostsFile=$TMP/known_hosts -o StrictHostKeyChecking=yes"
if [ -n "${AUR_SSH_PRIVATE_KEY:-}" ]; then
    (umask 077 && printf '%s\n' "$AUR_SSH_PRIVATE_KEY" > "$TMP/id_aur")
    SSH_CMD="$SSH_CMD -i $TMP/id_aur -o IdentitiesOnly=yes"
fi
export GIT_SSH_COMMAND="$SSH_CMD"

git clone --quiet "$AUR_REMOTE" "$TMP/repo"
for f in "${FILES[@]}"; do
    cp "$AUR/$f" "$TMP/repo/"
done
cd "$TMP/repo"
git add "${FILES[@]}"
if git diff --cached --quiet; then
    echo "AUR già aggiornato a klamav-py $VERSION: niente da pubblicare"
    exit 0
fi
git -c user.name="${AUR_GIT_NAME:-gradia}" -c user.email="${AUR_GIT_EMAIL:-gradia@disroot.org}" \
    commit --quiet -m "klamav-py $VERSION"
git push --quiet origin HEAD:master
echo "pubblicato su AUR: klamav-py $VERSION"
