#!/bin/bash
# Verifica che un tag di rilascio sia annotato e firmato da una sottochiave
# di firma dei TAG (tools/release-keys.sh), cioè dal maintainer. Non basta
# una sottochiave qualunque della chiave di rilascio: la sottochiave della
# CI, che firma gli allegati, deve non poter firmare un tag che passi da qui.
#
# I tag li firma solo il maintainer (vedi docs/CONTESTO-PROGETTO.md,
# «Rilascio»): la CI non pubblica nulla per un tag non firmato, o firmato
# con un'altra chiave, perché makepkg lo rifiuterebbe comunque per tutti gli
# utenti AUR (validpgpkeys) e il .deb allegato non corrisponderebbe a un
# sorgente verificabile.
#
# La verifica usa un portachiavi temporaneo con la SOLA chiave pubblica del
# repository: una chiave presente nel portachiavi della macchina non conta.
#
# Uso: tools/verify-tag.sh v0.1.14
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TAG="${1:?uso: tools/verify-tag.sh <tag>}"
PUBKEY="${KLAMAV_RELEASE_KEY:-$ROOT/arch/klamav-py-release-key.asc}"
# shellcheck source=tools/release-keys.sh
. "$ROOT/tools/release-keys.sh"

if [ "$(git cat-file -t "refs/tags/$TAG" 2>/dev/null)" != "tag" ]; then
    echo "$TAG non è un tag annotato (git tag -s): niente da verificare" >&2
    exit 1
fi

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"' EXIT
chmod 700 "$GNUPGHOME"
gpg --batch --quiet --import "$PUBKEY"

# VALIDSIG <impronta di chi ha firmato> ... <impronta della primaria>: si
# controllano entrambe. Una firma revocata o scaduta non produce VALIDSIG.
status="$(git verify-tag --raw "$TAG" 2>&1 || true)"
signer="$(printf '%s\n' "$status" | awk -v p="$RELEASE_PRIMARY" '$2 == "VALIDSIG" && $NF == p { print $3 }')"
for allowed in $TAG_SIGNING_SUBKEYS; do
    if [ -n "$signer" ] && [ "$signer" = "$allowed" ]; then
        echo "$TAG: firmato dal maintainer (sottochiave $signer della chiave di rilascio $RELEASE_PRIMARY)"
        exit 0
    fi
done
if [ -n "$signer" ]; then
    echo "il tag $TAG è firmato da $signer, che non è una sottochiave dei tag ($TAG_SIGNING_SUBKEYS)" >&2
else
    echo "il tag $TAG non ha una firma valida della chiave di rilascio $RELEASE_PRIMARY" >&2
    printf '%s\n' "$status" | grep -E '^\[GNUPG:\] (ERRSIG|BADSIG|NO_PUBKEY|VALIDSIG|EXPKEYSIG|REVKEYSIG)' >&2 || true
fi
exit 1
