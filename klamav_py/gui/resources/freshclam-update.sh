#!/bin/sh
# ============================================================
# ATTENZIONE — LEGGERE PRIMA DI MODIFICARE QUESTO FILE
# ============================================================
# Questo script viene eseguito con `pkexec sh <questo-file>` (root, via
# Polkit). Va tenuto SEMPRE testo statico, spedito com'è col pacchetto:
# non deve MAI leggere argomenti, variabili d'ambiente o file di
# configurazione dell'utente per decidere cosa eseguire. La versione
# precedente costruiva questa stessa sequenza come stringa Python passata
# a `sh -c`: funzionalmente identico, ma un domani un parametro reso
# dinamico in quella stringa (nome del servizio, opzioni di freshclam...)
# sarebbe stata shell injection diretta con privilegi di root. Come file
# fisso installato dal pacchetto, non c'è alcuna stringa costruita a
# runtime da interpolare: se serve rendere qualcosa configurabile, va
# passato a freshclam come argomento separato di argv, non concatenato
# qui dentro. L'unica variabile qui sotto ($to_restart) contiene solo
# nomi di servizio scritti letteralmente in questo file.
#
# Sequenza:
# 1. Si annotano i servizi del demone di sistema ATTIVI in questo momento
#    (il nome varia per distribuzione: clamav-freshclam su Debian/Ubuntu,
#    freshclam su Arch/Fedora). Alla fine si riavviano solo quelli: un
#    demone fermato di proposito dall'utente resta fermo.
# 2. systemctl stop: ferma il demone (tiene il lock sul log/database).
# 3. freshclam --stdout: l'aggiornamento vero e proprio.
# 4. Il riavvio avviene nel trap su EXIT, non in fondo allo script, così
#    scatta in OGNI caso di uscita: fine normale, errore di freshclam,
#    SIGTERM/SIGHUP/SIGINT (es. logout della sessione). Unica eccezione
#    possibile: SIGKILL, che per definizione non è intercettabile.
# 5. SIGPIPE ignorato: lo stdout di questo script è una pipe letta dalla
#    GUI. Se la GUI termina prima di noi, la prima scrittura successiva
#    ucciderebbe lo script con SIGPIPE PRIMA del riavvio del demone,
#    lasciando il servizio di sistema fermo. Con SIGPIPE ignorato le
#    scritture falliscono (EPIPE) senza uccidere nessuno; l'impostazione
#    è ereditata da freshclam, e anche se freshclam dovesse comunque
#    fermarsi, il trap riavvia il demone, che aggiorna da sé.
# 6. Il codice di uscita restituito alla GUI è quello di freshclam, non
#    quello dei systemctl eseguiti nel trap.

trap '' PIPE

to_restart=""
for svc in clamav-freshclam freshclam; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        to_restart="$to_restart $svc"
    fi
done

# shellcheck disable=SC2329  # invocata dal trap su EXIT
restore_daemon() {
    for svc in $to_restart; do
        systemctl start "$svc" 2>/dev/null
    done
}

# EXIT per l'uscita normale; i segnali vengono convertiti in exit (con il
# codice convenzionale 128+N) perché in sh POSIX il trap su EXIT non
# scatta se lo script viene ucciso da un segnale non gestito.
trap restore_daemon EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

for svc in $to_restart; do
    systemctl stop "$svc" 2>/dev/null
done

freshclam --stdout
exit $?
