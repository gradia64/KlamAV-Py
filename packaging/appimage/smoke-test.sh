#!/bin/bash
# Prova di un'AppImage già costruita, senza FUSE e senza rete.
#
#   1. controlli con il Python dell'AppImage (smoke_check.py): marcatore,
#      file .desktop sul file .AppImage, niente timer di sistema, icona, SSL
#   2. la CLI («cli» e un collegamento chiamato klamav-py): --version, ping e
#      scansione contro un finto clamd (tests/fake_clamd.py) su socket Unix,
#      con un file pulito e uno «infetto» (uscita 1)
#   3. avvio della GUI (offscreen e, con xvfb-run, su X11), che deve essere
#      ancora viva dopo qualche secondo
#
# HOME, configurazione e runtime dir sono sempre cartelle temporanee:
# niente impostazioni, quarantena o single-instance di chi lancia la prova
# (con una KlamAV-Py già aperta la GUI le passerebbe gli argomenti e
# uscirebbe subito).
#
# Uso: packaging/appimage/smoke-test.sh dist/KlamAV-Py-X.Y.Z-x86_64.AppImage
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APPIMAGE_FILE="$(readlink -f "${1:?uso: smoke-test.sh <file .AppImage>}")"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/klamav_py/__init__.py")"
TMP="$(mktemp -d)"
# runtime dir corta, sotto /tmp: il socket del single-instance ci deve
# stare nei 108 byte di sun_path, che un TMPDIR lungo supererebbe
XDG_RUNTIME_DIR="$(mktemp -d /tmp/klamav-run.XXXXXX)"
FAKE_PID=""
cleanup() {
    [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null || true
    rm -rf "$TMP" "$XDG_RUNTIME_DIR"
}
trap cleanup EXIT
export QT_QPA_PLATFORM=offscreen
export HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/config" XDG_RUNTIME_DIR
mkdir -p "$HOME" "$XDG_CONFIG_HOME"
chmod 700 "$XDG_RUNTIME_DIR"
# extract-and-run: niente FUSE; la copia estratta in una cartella nostra
export APPIMAGE_EXTRACT_AND_RUN=1 TMPDIR="$TMP/run"
mkdir -p "$TMPDIR"

cd "$TMP"
"$APPIMAGE_FILE" --appimage-extract >/dev/null
APPDIR_X="$TMP/squashfs-root"
PY="$APPDIR_X/usr/python/bin/python3"

echo "== controlli nell'AppImage"
APPIMAGE="$APPIMAGE_FILE" APPDIR="$APPDIR_X" "$PY" -E -s "$ROOT/packaging/appimage/smoke_check.py"

echo "== CLI"
out="$("$APPIMAGE_FILE" cli --version)"
[ "$out" = "klamav-py $VERSION" ] || { echo "versione inattesa: $out" >&2; exit 1; }
ln -s "$APPIMAGE_FILE" "$TMP/klamav-py"
out="$("$TMP/klamav-py" --version)"
[ "$out" = "klamav-py $VERSION" ] || { echo "collegamento klamav-py: $out" >&2; exit 1; }

SOCK="$TMP/clamd.sock"
python3 -I - "$ROOT/tests/fake_clamd.py" "$SOCK" <<'PY' &
import importlib.util, socket, sys, time
spec = importlib.util.spec_from_file_location("fake_clamd", sys.argv[1])
fake = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake)
fake.FakeClamd(socket.AF_UNIX, sys.argv[2])
time.sleep(600)
PY
FAKE_PID=$!
for _ in $(seq 50); do [ -S "$SOCK" ] && break; sleep 0.1; done
[ -S "$SOCK" ] || { echo "il finto clamd non è partito" >&2; exit 1; }

"$APPIMAGE_FILE" cli --socket "$SOCK" ping
mkdir -p "$TMP/scan"
echo pulito > "$TMP/scan/pulito.txt"
python3 -I -c 'import sys; open(sys.argv[1], "wb").write(b"KLAMAV-PY-FINTO-VIRUS-DI-PROVA")' \
    "$TMP/scan/infetto.txt"
set +e
"$APPIMAGE_FILE" cli --socket "$SOCK" scan "$TMP/scan" > "$TMP/scan.log" 2>&1
rc=$?
set -e
if [ "$rc" -ne 1 ] || ! grep -q "infetto.txt" "$TMP/scan.log"; then
    cat "$TMP/scan.log"
    echo "scansione con la CLI dell'AppImage: uscita $rc invece di 1" >&2
    exit 1
fi
echo "scansione OK (1 rilevamento)"

# avvia l'AppImage per 10 secondi: deve essere ancora aperta e senza traceback
run_app() {   # run_app <descrizione> [prefisso del comando…]
    local what="$1" rc
    shift
    echo "== avvio della GUI ($what)"
    set +e
    "$@" timeout 10 "$APPIMAGE_FILE" > "$TMP/app.log" 2>&1
    rc=$?
    set -e
    if [ "$rc" -ne 124 ]; then
        cat "$TMP/app.log"
        echo "la GUI è uscita da sola (codice $rc) invece di restare aperta" >&2
        exit 1
    fi
    # anche il single-instance: dall'AppImage deve funzionare (QtNetwork)
    if grep -Eq 'Traceback|QThread: Destroyed|Segmentation|qt\.qpa\.plugin|istanza singola non attivo' \
            "$TMP/app.log"; then
        cat "$TMP/app.log"
        echo "errori nell'output della GUI" >&2
        exit 1
    fi
    echo "avvio OK"
}

run_app offscreen

# con un server X virtuale si carica davvero il plugin X11 di Qt, con le
# librerie xcb-util incluse nell'AppImage: offscreen non le tocca
if command -v xvfb-run >/dev/null; then
    # dopo il timeout il runtime esce prima che Python abbia finito di
    # chiudere: X resta acceso ancora un po' (vedi OllaDesk)
    QT_QPA_PLATFORM=xcb run_app "X11, xvfb" \
        xvfb-run -a sh -c '"$@"; rc=$?; sleep 3; exit $rc' sh
else
    echo "== X11 saltato: xvfb-run non installato (pacchetto xvfb)"
fi
