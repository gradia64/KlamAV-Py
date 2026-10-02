#!/bin/bash
# Note della release GitHub: la voce di CHANGELOG.md della versione indicata
# (Modificato, Corretto, Aggiunto), senza l'intestazione «## X — data».
# È la descrizione prevista dalla procedura di rilascio (CONTESTO, sezione
# 4, «Rilascio»). Errore se la voce manca o è vuota: una release senza note
# vuol dire che il commit di rilascio non ha aggiornato il CHANGELOG.
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
# Righe vuote in testa e in coda tolte.
printf '%s\n' "$notes" | sed -e '/./,$!d' | tac | sed -e '/./,$!d' | tac
