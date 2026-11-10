#!/bin/bash
# Note della release GitHub: la voce di CHANGELOG.md della versione indicata,
# senza l'intestazione «## X — data». È la descrizione prevista dalla
# procedura di rilascio (docs/RILASCIO.md).
# Errore se la voce manca o è vuota: una release senza note vuol dire che il
# commit di rilascio non ha aggiornato il CHANGELOG.
# Errore anche se una sezione non ha un titolo inglese (Security, Fixed,
# Added, Changed, Removed, Release): le note della release sono in inglese
# dalla 0.1.21, e un titolo italiano vuol dire una voce scritta in italiano.
#
# Uso: tools/release-notes.sh 0.1.14 > note.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:?uso: tools/release-notes.sh <versione>}"

notes="$(awk -v ver="$VERSION" '
    /^## / { if (inside) exit; inside = ($2 == ver); next }
    inside && /^---$/ { exit }
    inside { print }
' "$ROOT/CHANGELOG.md")"

if [ -z "$(printf '%s' "$notes" | tr -d '[:space:]')" ]; then
    echo "nessuna voce «## $VERSION» in CHANGELOG.md" >&2
    exit 1
fi
bad="$(printf '%s\n' "$notes" | grep -E '^### ' \
    | grep -vxE '### (Security|Fixed|Added|Changed|Removed|Release)' || true)"
if [ -n "$bad" ]; then
    echo "voce «## $VERSION» di CHANGELOG.md con sezioni non in inglese:" >&2
    printf '%s\n' "$bad" | sed 's/^/  /' >&2
    echo "titoli ammessi: Security, Fixed, Added, Changed, Removed, Release" >&2
    exit 1
fi
# Righe vuote in testa e in coda tolte.
printf '%s\n' "$notes" | sed -e '/./,$!d' | tac | sed -e '/./,$!d' | tac
