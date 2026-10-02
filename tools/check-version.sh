#!/bin/bash
# Il tag di rilascio corrisponde alla versione in tutte le sue fonti:
# klamav_py/__init__.py, arch/PKGBUILD, debian/changelog (revisione Debian
# esclusa) e una voce «## <versione>» in CHANGELOG.md. tests/test_changelog.py
# verifica già l'allineamento fra le fonti; qui si aggiunge il tag, che i
# test non vedono. Un tag che non corrisponde produrrebbe un .deb e un
# pacchetto Arch con una versione diversa da quella annunciata.
#
# Uso: tools/check-version.sh v0.1.14
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
TAG="${1:?uso: tools/check-version.sh <tag>}"
want="${TAG#v}"

code="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' klamav_py/__init__.py)"
pkgbuild="$(sed -n 's/^pkgver=//p' arch/PKGBUILD)"
deb="$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' debian/changelog)"
deb="${deb%-*}"

status=0
for pair in "klamav_py/__init__.py:$code" "arch/PKGBUILD:$pkgbuild" "debian/changelog:$deb"; do
    if [ "${pair#*:}" != "$want" ]; then
        echo "tag $TAG ma ${pair%%:*} dice ${pair#*:}" >&2
        status=1
    fi
done
if ! grep -qE "^## $(printf '%s' "$want" | sed 's/\./\\./g')( |$)" CHANGELOG.md; then
    echo "CHANGELOG.md non ha la voce «## $want»" >&2
    status=1
fi
[ "$status" -eq 0 ] && echo "versione $want allineata al tag $TAG"
exit "$status"
