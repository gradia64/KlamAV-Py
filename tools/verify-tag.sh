#!/bin/bash
# Verifica che un tag di rilascio sia annotato e firmato con la chiave di
# rilascio del repository (arch/klamav-py-release-key.asc), primaria o una
# sua sottochiave di firma.
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

if [ "$(git cat-file -t "refs/tags/$TAG" 2>/dev/null)" != "tag" ]; then
    echo "$TAG non è un tag annotato (git tag -s): niente da verificare" >&2
    exit 1
fi

GNUPGHOME="$(mktemp -d)"
export GNUPGHOME
trap 'gpgconf --kill gpg-agent >/dev/null 2>&1 || true; rm -rf "$GNUPGHOME"' EXIT
chmod 700 "$GNUPGHOME"
gpg --batch --quiet --import "$PUBKEY"
PRIMARY="$(gpg --batch --with-colons --show-keys "$PUBKEY" | awk -F: '$1 == "fpr" { print $10; exit }')"

# VALIDSIG <impronta della chiave che ha firmato> ... <impronta della primaria>:
# l'ultimo campo è la primaria anche per una firma fatta con una sottochiave.
status="$(git verify-tag --raw "$TAG" 2>&1 || true)"
if ! printf '%s\n' "$status" | grep -qE "^\[GNUPG:\] VALIDSIG .* $PRIMARY\$"; then
    echo "il tag $TAG non è firmato con la chiave di rilascio $PRIMARY" >&2
    printf '%s\n' "$status" | grep -E '^\[GNUPG:\] (ERRSIG|BADSIG|NO_PUBKEY|VALIDSIG|EXPKEYSIG|REVKEYSIG)' >&2 || true
    exit 1
fi
echo "$TAG: firma valida della chiave di rilascio $PRIMARY"
