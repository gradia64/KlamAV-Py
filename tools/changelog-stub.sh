#!/usr/bin/env bash
# Genera la bozza di una nuova voce di CHANGELOG.md a partire dalla voce
# in cima a debian/changelog (quella appena scritta da dch) e la
# inserisce nel file, subito dopo il separatore "---".
#
# Non è una conversione automatica: produce una bozza da ACCORCIARE a
# mano. debian/changelog è verboso per natura, CHANGELOG.md non deve
# esserlo. Le voci vengono messe tutte sotto "### Corretto": vanno
# ridistribuite fra Sicurezza / Corretto / Aggiunto / Modificato.
#
# Uso, dalla radice del repo, dopo dch e prima del commit di rilascio:
#   tools/changelog-stub.sh
#   $EDITOR CHANGELOG.md
#   python3 -m pytest tests/ -k changelog

set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO"

CHANGELOG_MD=CHANGELOG.md
CHANGELOG_DEB=debian/changelog

for f in "$CHANGELOG_MD" "$CHANGELOG_DEB"; do
    [ -f "$f" ] || { echo "ERRORE: $f non trovato (eseguire dalla radice del repo)" >&2; exit 1; }
done

# Versione completa (con revisione Debian) e data della voce in cima
VERSIONE=$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "$CHANGELOG_DEB")
[ -n "$VERSIONE" ] || { echo "ERRORE: prima riga di $CHANGELOG_DEB non riconosciuta" >&2; exit 1; }

# La riga di firma "-- Nome <email>  Data" chiude la voce; ne estraggo la data
DATA=$(awk '/^ -- /{sub(/^ -- [^>]*>[[:space:]]*/,""); print; exit}' "$CHANGELOG_DEB")
DATA_ISO=$(date -d "$DATA" +%Y-%m-%d 2>/dev/null || date +%Y-%m-%d)

# La revisione Debian (-1, -2) non ha senso fuori da Debian: la tengo solo
# se diversa da -1, perché in quel caso segnala una release solo-packaging.
UPSTREAM=${VERSIONE%%-*}
REVISIONE=${VERSIONE#*-}
if [ "$REVISIONE" = "1" ] || [ "$REVISIONE" = "$VERSIONE" ]; then
    TITOLO="$UPSTREAM"
else
    TITOLO="$VERSIONE"
fi

if grep -q "^## $TITOLO " "$CHANGELOG_MD"; then
    echo "La versione $TITOLO è già presente in $CHANGELOG_MD: niente da fare."
    exit 0
fi

# Corpo della voce: righe fra l'intestazione e la firma. dch usa "  * " per
# una voce nuova e "    " per le continuazioni: le ricompatto su una riga
# sola e le converto in bullet Markdown.
CORPO=$(awk '
    NR == 1 { next }
    /^ -- / { exit }
    /^[[:space:]]*$/ { next }
    /^  \* / { if (buf != "") print buf; sub(/^  \* /, "- "); buf = $0; next }
    { sub(/^[[:space:]]+/, " "); buf = buf $0 }
    END { if (buf != "") print buf }
' "$CHANGELOG_DEB")

# A capo a 76 colonne, con le continuazioni rientrate di due spazi: le
# voci di dch sono lunghe e, lasciate su una riga sola, farebbero
# fallire il test che intercetta il testo incollato e riflusso.
CORPO=$(printf '%s\n' "$CORPO" | python3 -c '
import sys, textwrap
for riga in sys.stdin.read().splitlines():
    if not riga.strip():
        continue
    print(textwrap.fill(riga, width=76, subsequent_indent="  "))
')

STUB="## $TITOLO — $DATA_ISO

### Corretto
$CORPO
"

# Inserimento dopo il separatore "---" che chiude l'intestazione del file
TMP=$(mktemp)
awk -v stub="$STUB" '
    !fatto && /^---$/ { print; print ""; print stub; fatto = 1; next }
    { print }
    END { if (!fatto) exit 1 }
' "$CHANGELOG_MD" > "$TMP" || {
    rm -f "$TMP"
    echo "ERRORE: separatore '---' non trovato in $CHANGELOG_MD" >&2
    exit 1
}
mv "$TMP" "$CHANGELOG_MD"

echo "Bozza per $TITOLO inserita in $CHANGELOG_MD."
echo "Ora accorciala e ridistribuisci le voci fra Sicurezza / Corretto / Aggiunto / Modificato."
