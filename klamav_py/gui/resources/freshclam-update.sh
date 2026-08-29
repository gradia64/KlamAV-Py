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
# qui dentro.
#
# 1. systemctl stop: ferma il demone di sistema (nome varia per
#    distribuzione: clamav-freshclam su Debian/Ubuntu, freshclam su
#    Arch/Fedora). 2>/dev/null silenzia l'errore se il servizio ha un
#    nome diverso o non esiste su questo sistema.
# 2. freshclam --stdout: l'aggiornamento vero e proprio.
# 3. res=$?: salva il codice di uscita di freshclam PRIMA di eseguire
#    altro (systemctl start altrimenti lo sovrascriverebbe).
# 4. systemctl start: riavvia il demone di sistema.
# 5. exit $res: il codice di uscita restituito alla GUI è quello di
#    freshclam, non quello dell'ultimo systemctl.

systemctl stop clamav-freshclam 2>/dev/null
systemctl stop freshclam 2>/dev/null
freshclam --stdout
res=$?
systemctl start clamav-freshclam 2>/dev/null
systemctl start freshclam 2>/dev/null
exit $res
