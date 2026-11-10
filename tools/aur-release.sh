#!/bin/bash
# Pubblica su AUR il PKGBUILD di una release, dalla macchina del maintainer.
# Stesso schema di OllaDesk, Plico e Clonaut.
#
# La chiave SSH dell'account AUR non sta nella CI: vale per tutti i pacchetti
# dell'account, e chi ne ottenesse i secret potrebbe pubblicare un PKGBUILD
# qualsiasi. La CI costruisce e prova il pacchetto dal tag (job «arch» di
# release.yml, tools/build-arch.sh); il push lo fa il maintainer con questo
# script, dopo che la release è uscita:
#
# 1. verifica il tag con tools/verify-tag.sh (firmato dalla sottochiave dei
#    tag);
# 2. scarica l'artefatto «aur» della run riuscita di release.yml per quel
#    tag, costruita dallo stesso commit;
# 3. controlla che PKGBUILD e klamav-py.install dell'artefatto siano
#    identici a quelli dell'albero del tag (git archive): si pubblica solo
#    ciò che è nel tag firmato e che la CI ha costruito e provato, e la CI
#    non può cambiare ciò che si pubblica;
# 4. controlla che .SRCINFO (generato da makepkg nella CI) descriva quel
#    PKGBUILD: versione, sorgente, somme e chiave;
# 5. pubblica con tools/publish-aur.sh e la chiave SSH dell'utente
#    (~/.ssh/config, Host aur.archlinux.org).
#
# Uso: tools/aur-release.sh vX.Y.Z
# Per i test: KLAMAV_AUR_ARTIFACT=<cartella> salta gh; AUR_REMOTE e le
# variabili di tools/verify-tag.sh come lì.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TAG="${1:?uso: tools/aur-release.sh vX.Y.Z}"
VERSION="${TAG#v}"
[[ $TAG =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "tag non valido: $TAG" >&2; exit 1; }
FILES=(PKGBUILD klamav-py.install)

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# 1. il tag è del maintainer
tools/verify-tag.sh "$TAG"
tag_sha="$(git rev-parse "refs/tags/$TAG^{commit}")"

# 2. l'artefatto della run che ha costruito il pacchetto da quel commit
if [ -n "${KLAMAV_AUR_ARTIFACT:-}" ]; then
    ART="$KLAMAV_AUR_ARTIFACT"
else
    run="$(gh run list --workflow release.yml --branch "$TAG" --event push \
            --status success --json databaseId,headSha \
            --jq "map(select(.headSha == \"$tag_sha\"))[0].databaseId")"
    if [ -z "$run" ] || [ "$run" = "null" ]; then
        echo "nessuna run riuscita di release.yml per $TAG ($tag_sha): la release è uscita?" >&2
        exit 1
    fi
    gh run download "$run" --name aur --dir "$TMP/artifact"
    ART="$TMP/artifact"
fi
for f in "${FILES[@]}" .SRCINFO; do
    [ -f "$ART/$f" ] || { echo "manca $f nell'artefatto della CI ($ART)" >&2; exit 1; }
done

# 3. gli stessi file dell'albero del tag
mkdir "$TMP/src" "$TMP/aur"
git archive "$tag_sha" arch | tar -x -C "$TMP/src"
for f in "${FILES[@]}"; do
    if ! cmp -s "$TMP/src/arch/$f" "$ART/$f"; then
        echo "$f della CI è diverso da quello del tag $TAG:" >&2
        diff -u "$TMP/src/arch/$f" "$ART/$f" >&2 || true
        exit 1
    fi
    cp "$TMP/src/arch/$f" "$TMP/aur/"
done
pkgbuild="$TMP/aur/PKGBUILD"
[ "$(sed -n 's/^pkgver=//p' "$pkgbuild")" = "$VERSION" ] \
    || { echo "il PKGBUILD del tag non dichiara pkgver=$VERSION" >&2; exit 1; }

# 4. .SRCINFO coerente con il PKGBUILD (è quello che AUR mostra e indicizza)
srcinfo="$ART/.SRCINFO"
primary="$(sed -n "s/^validpgpkeys=('\([0-9A-F]*\)').*/\1/p" "$pkgbuild")"
url="$(sed -n 's/^url="\(.*\)"$/\1/p' "$pkgbuild")"
expect=(
    "pkgbase = klamav-py"
    "	pkgver = $VERSION"
    "	source = klamav-py::git+$url.git#tag=v$VERSION?signed"
    "	validpgpkeys = $primary"
    "	sha256sums = SKIP"
)
for line in "${expect[@]}"; do
    grep -qxF "$line" "$srcinfo" || { echo ".SRCINFO della CI senza «${line#	}»" >&2; exit 1; }
done
if [ "$(grep -c '^	source = ' "$srcinfo")" -ne 1 ] \
        || [ "$(grep -c '^	sha256sums = ' "$srcinfo")" -ne 1 ] \
        || [ "$(grep -c '^	validpgpkeys = ' "$srcinfo")" -ne 1 ]; then
    echo ".SRCINFO della CI con sorgenti, somme o chiavi in più del PKGBUILD" >&2
    exit 1
fi
cp "$srcinfo" "$TMP/aur/.SRCINFO"

# 5. pubblicazione con la chiave SSH dell'utente
tools/publish-aur.sh "$TMP/aur"
