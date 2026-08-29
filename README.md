KlamAV-Py

CILicense

Frontend minimale per ClamAV via clamd — CLI e GUI per Linux.

Riscrittura minimale, in Python, dell'idea alla base di KlamAV 0.22(frontend a ClamAV), senza le parti diventate tecnologia morta(Dazuko, DCOP, Qt3) e senza i problemi di sicurezza dell'originale(shell injection via KShellProcess): nessun eseguibile esterno vieneinvocato per la scansione, i path non transitano mai da una shell.
Caratteristiche

    Scansione via protocollo nativo di clamd (INSTREAM/IDSESSION susocket Unix), CLI e GUI.
    CLI senza dipendenze esterne (solo libreria standard), GUI in PySide6.
    Quarantena con permessi neutralizzati (0400), nome su disco nonprevedibile, ripristino dei permessi originali, indice condiviso eprotetto da lock tra CLI/GUI/worker.
    Scansioni pianificate: timer interno alla GUI e/o unit systemd utente(via pacchetto .deb), con log persistente dei risultati.
    Monitoraggio Real-Time delle cartelle configurate (QFileSystemWatcher,con riconciliazione periodica dei watch persi).
    Pausa/Ripresa delle scansioni, guard "una scansione alla volta".
    Pre-check dimensionale: i file oltre StreamMaxLength sono segnalati"non verificati" senza sprecare la sessione clamd.
    Integrazione menu contestuale Dolphin, autostart, system tray,single-instance con IPC.

Requisiti

    clamav-daemon installato e attivo (clamd), non i soli binariclamscan/freshclam: questo progetto parla col demone via socket,non invoca eseguibili esterni per la scansione.
    Python 3.10+ come baseline dichiarata. Il minimo tecnico reale è 3.9(Path.is_relative_to); il walrus operator usato in clamd_client.pyesiste dal 3.8. Testato localmente su 3.14 (Debian Sid) e in CI su3.10/3.11/3.12.
    CLI: nessuna dipendenza esterna a runtime — gira anche con il Pythondi sistema, senza venv.
    GUI: PySide6, va installato in un venv dedicato (vedi sotto), nonnel Python di sistema.
    GUI, solo per l'aggiornamento del database virus dal pulsante"Aggiorna Database": freshclam nel PATH e pkexec (PolicyKit)disponibili, dato che l'operazione richiede privilegi di root.
    GUI, solo per l'integrazione col menu contestuale di Dolphin:un ambiente KDE Plasma con kbuildsycoca5/kbuildsycoca6.
    Sviluppo/test: pytest (vedi requirements-dev.txt), non richiestoa runtime.

Installazione
Da release precompilata (consigliata per l'uso quotidiano)

Scarica il pacchetto .deb dalla paginaReleases e installa:

sudo dpkg -i klamav-py_*.debsudo apt-get install -f    # sistema le dipendenze mancanti, se servono

Installa klamav-py (CLI) e klamav-py-gui in /usr/bin/, abilita
automaticamente il timer systemd utente per la scansione programmata e
registra l'applicazione nei menu. Dettagli e avvertenze nella sezione
"Pacchettizzazione .deb" più sotto.
Da sorgenti (sviluppo o build del pacchetto)

Build del .deb:
bash
 
  
 
 
sudo apt install devscripts debhelper dh-python pybuild-plugin-pyproject \
    python3-all python3-setuptools
cd klamav-py
dpkg-buildpackage -us -uc -b
sudo dpkg -i ../klamav-py_<versione>_all.deb
 
 

Ambiente di sviluppo (GUI da checkout):
bash
 
  
 
 
cd klamav-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
 
 

Con il venv attivo (o chiamando direttamente venv/bin/python):
bash
 
  
 
 
python3 -m klamav_py.gui.app
 
 

La CLI invece non ha bisogno del venv e gira con il Python di sistema.

Nota per lo sviluppo: se nel venv esiste anche una copia installata
del pacchetto (pip install . non-editable), l'import di klamav_py
può risolvere lì invece che nella checkout a seconda della directory
corrente. Per evitare la trappola usa pip install -e .; e su una
macchina con checkout e .deb installati insieme, verifica sempre quale
istanza GUI è effettivamente viva (ps aux | grep klamav): la
single-instance fa sì che il secondo lancio venga assorbito dalla prima
istanza esistente, qualunque essa sia.
Uso rapido
bash
 
  
 
 
# CLI: verifica che clamd risponda (funziona anche senza venv)
python3 -m klamav_py.cli ping

# CLI: scansione con quarantena automatica
python3 -m klamav_py.cli scan /home/utente/Scaricati \
    --quarantine ~/.local/share/klamav-py/quarantine

# CLI: scansione della home con esclusioni e log errori su file
python3 -m klamav_py.cli scan ~ \
    --exclude ~/.local/share/klamav-py \
    --exclude ~/.cache \
    --log-errors /tmp/klamav-errors.log

# GUI (richiede il venv attivo o l'interprete del venv, vedi sopra)
python3 -m klamav_py.gui.app
# oppure, con socket/quarantena non standard:
python3 -m klamav_py.gui.app --socket /run/clamav/clamd.ctl \
    --quarantine-dir ~/.local/share/klamav-py/quarantine
# oppure per avviare direttamente la scansione di un percorso (usato
# anche dall'integrazione Dolphin, vedi sotto):
python3 -m klamav_py.gui.app --scan-target /percorso/da/scansionare
 
 

La directory passata a --quarantine è sempre esclusa
automaticamente dall'attraversamento, sia in CLI che in GUI: i file
già gestiti non devono essere ri-rilevati (e ri-quarantenati) a ogni
scansione che copre la loro posizione.
CLI: opzioni di scansione

     --quarantine DIR — sposta i file infetti in DIR (creata con
    permessi 0700).
     --exclude DIR — directory da escludere dall'attraversamento
    ricorsivo (ripetibile). Le directory escluse non vengono nemmeno
    lette (pruning): utile per cache, Trash, mount di rete sincronizzati.
     --max-stream-size BYTES — soglia del pre-check dimensionale
    (default 25MB, allineato a StreamMaxLength di clamd.conf;
    0 disattiva il pre-check).
     --log-errors FILE — dettaglio di ogni errore (path e motivo) su file.
     --no-persistent / --session-batch-size N — controllo della
    sessione persistente (vedi sezione dedicata).

Codici di uscita: 0 = pulito, 1 = infezioni trovate, 2 = errore
di esecuzione (clamd irraggiungibile, path inesistente) — utile per
OnFailure= in systemd o per script di monitoraggio.
Sessione persistente, pre-check dimensionale, ricostruzione

Di default scan_stream riusa una singola connessione (sessione
IDSESSION) per più file di fila invece di aprirne una nuova per
ognuno — su alberi con decine di migliaia di file evita l'overhead di
connect ripetuto. La sessione viene comunque richiusa e riaperta ogni
500 file (--session-batch-size) per limitare l'impatto di limiti non
documentati su sessioni molto lunghe, e ricreata da zero ogni volta che
qualcosa va storto: un file problematico non compromette il resto della
scansione. Quando clamd chiude la connessione (tipico caso: rifiuto per
StreamMaxLength), la sessione è marcata come morta e ricreata
prima del file successivo, così i file seguenti non muoiono a
cascata su una connessione già chiusa.

Pre-check dimensionale: i file oltre la soglia (default 25MB, come
StreamMaxLength) non vengono inviati affatto a clamd — escono subito
con status TOO_LARGE ("non verificato"), distinto dagli errori veri
sia in CLI che in GUI. Senza pre-check, clamd rifiuta lo stream a metà
invio chiudendo la connessione: l'errore che l'utente vedrebbe sarebbe
una "pipe interrotta" su un file che in realtà sta bene (vedi
"Diagnosi degli errori comuni"). Il pre-check ha una race nota e
accettata — un file che cresce oltre soglia tra il controllo e l'invio
viene comunque gestito dal fallback che classifica gli errori di stream
su file sopra soglia come TOO_LARGE.

Se sospetti che un problema sia legato alla sessione persistente,
--no-persistent torna al comportamento "una connessione per file"
per isolare il caso.

A fine scansione la CLI stampa un riepilogo degli errori raggruppati
per tipo (es. "2 Permission denied", "1 Broken pipe"), invece di dover
fare grep/sort/uniq -c manuali.
GUI: panoramica dell'interfaccia

Applicazione PySide6 a finestra unica con barra laterale e stile
ispirato a KDE Plasma (usa sempre i colori della palette di sistema,
mai colori hardcoded, tranne per gli stati semantici come "infetto").
Sette sezioni:

     

    Scansione — scansione manuale su un percorso a scelta, in un
    QThread separato (ScanWorker): la finestra resta reattiva anche
    su directory grandi, e la barra di stato mostra il file in
    elaborazione in tempo reale (con elisione del testo per non far
    crescere il layout su percorsi lunghi). I contatori distinguono
    scansionati / infetti / errori / non verificati (troppo grandi),
    e solo infetti/errori/non-verificati producono righe in lista.

    Sospendi / Riprendi. La pausa ha granularità di file intero: il
    file in streaming viene completato, poi il worker si ferma (il
    pulsante segue i segnali del worker, non il click, quindi lo stato
    mostrato è sempre quello effettivo). "Interrompi" resta attivo anche
    in pausa. Dopo una pausa più lunga di ~25s (vicino all'IdleTimeout
    di clamd, 30s di default) la sessione viene ricreata proattivamente
    alla ripresa, per non produrre un errore finto sul primo file.
    Con la finestra minimizzata in tray, il tooltip dell'icona mostra i
    contatori della scansione in corso e lo stato di pausa.

    Quarantena manuale di default. La casella "Metti in quarantena
    automaticamente i file infetti" è disattivata di default: i file
    segnalati restano dove sono e li sposti tu con "Metti in quarantena
    i selezionati" — comodo per valutare un falso positivo prima di
    spostare qualcosa. "Copia log" copia negli appunti tutte le righe
    della lista (infetti, errori, non verificati).
     

    Cronologia — registro persistente
    (~/.local/share/klamav-py/history.json, ultime 1000 voci) con
    data/ora, tipo, percorso, scansionati, infetti, errori e non
    verificati. Le scansioni programmate indicizzano anche il log
    dettagliato su disco (tooltip sulla riga): il dettaglio
    infetti/errori di una scansione background, che non passa da nessuna
    lista UI, resta così sempre ispezionabile.
     

    Quarantena — legge lo stesso indice JSON usato dalla CLI
    (index.json nella directory di quarantena): i due strumenti sono
    intercambiabili sugli stessi dati. Ripristino nella posizione
    originale (con ripristino dei permessi originali, rifiuto esplicito
    se il percorso è stato nel frattempo rioccupato) o eliminazione
    definitiva.
     

    Aggiornamenti — scarica le definizioni virus lanciando
    freshclam --stdout tramite pkexec (autenticazione PolicyKit),
    fermando e riavviando automaticamente il demone clamav-freshclam/
    freshclam di sistema per evitare conflitti di lock sul file di
    log. Output mostrato in tempo reale. Di default parte automaticamente
    1.5s dopo l'avvio dell'app (disattivabile in Impostazioni).
     

    Real-Time — log delle scansioni automatiche sulle cartelle
    monitorate (configurabili in Impostazioni). Usa QFileSystemWatcher:
    creazione/modifica di un file → scansione dopo un debounce di 3s (per
    non scansionare file ancora in scrittura), e i file infetti vengono
    sempre messi in quarantena automaticamente. I file troppo grandi
    sono etichettati "Non verificato", non "Sicuro" (vedi scelte di
    design).
     

    Pianificazione — scansione ricorrente (ogni N ore o giorni) su
    una cartella a scelta, tramite QTimer interno: richiede l'app in
    esecuzione (anche minimizzata in tray). Durante l'esecuzione la
    pagina mostra lo stato e i contatori live; il tooltip della tray
    riflette l'avanzamento; a fine scansione il log dettagliato viene
    scritto in ~/.local/share/klamav-py/logs/scheduled-<timestamp>.log
    (rotazione: ultime 10 esecuzioni) e referenziato in Cronologia. Non
    è un timer di sistema come quello del pacchetto .deb, che funziona
    anche a GUI chiusa (vedi "Scansioni pianificate: i tre meccanismi").
     

    Impostazioni — socket di clamd, directory di quarantena,
    cartelle monitorate per il Real-Time (con indicatore "Real-Time
    attivo su N/M cartelle"), autostart al login, avvio minimizzato in
    tray, aggiornamento DB all'avvio, integrazione Dolphin.

La versione del programma è visibile nel titolo della finestra, nel
tooltip di riposo della system tray e nel pannello Impostazioni.

Tutte le impostazioni sono salvate con QSettings (organizzazione
"KlamAV-Py", applicazione "KlamAV-Py" — su Linux tipicamente
~/.config/KlamAV-Py/KlamAV-Py.conf). Le impostazioni di versioni
precedenti alla 0.1.3-2 (file ~/.config/KlamAV/KlamAV.conf) vengono
migrate automaticamente al primo avvio; il vecchio file resta su disco.
Scelte di design intenzionali

Quarantena manuale in Scansione, automatica in Real-Time. Non è
un'incoerenza: le cartelle monitorate in Real-Time sono tipicamente
destinazioni di file appena scaricati/ricevuti da fuori (es.
~/Scaricati) — il rischio di falso positivo su un file nuovo e mai
toccato è basso, quindi ha senso agire subito. La pagina Scansione può
essere puntata su cartelle con dati importanti, dove un falso positivo
va verificato prima di spostare qualcosa.

Guard "una scansione alla volta" tra manuale e programmata. Una
scansione manuale non parte se una programmata è in corso (e viceversa
la programmata salta, con voce esplicita in Cronologia e notifica, se
una manuale è attiva). Motivi: contesa su clamd (due traversal
home-wide in parallelo raddoppiano I/O e memoria del demone) e doppio
carico sul filesystem. Il Real-Time è deliberatamente esente dal
guard: è la protezione primaria, agisce su file singoli (secondi), e
clamd gestisce connessioni concorrenti per design — sospenderlo mentre
gira una manuale sarebbe peggio. Una scansione in pausa resta "in
corso" ai fini del guard: blocca la programmata finché non riprende o
viene interrotta.

Esclusione della directory di quarantena. I file in quarantena sono
i dati gestiti dall'applicazione: vengono esclusi dall'attraversamento
(pruning, non vengono nemmeno letti) e, come difesa in profondità,
quarantine_file() rifiuta esplicitamente di ri-quarantenare un file
che si trova già dentro la directory di quarantena. Senza questi guard,
una scansione home-wide rileverebbe i vecchi EICAR in quarantena a ogni
ciclo, gonfiando per sempre infetti ed errori con "fantasmi" già
gestiti.
System tray e single-instance

L'app resta attiva nella system tray anche chiudendo la finestra (la
X la nasconde, non la termina: si esce dal menu della tray o da
"Esci"). È pensata per restare in background per pianificazione e
Real-Time.

È inoltre single-instance: se lanci la GUI mentre un'altra istanza è
già attiva, la seconda invia il proprio --scan-target (se presente)
alla prima via QLocalServer/QLocalSocket e termina subito, invece
di aprire una seconda finestra. È il meccanismo che rende sensata
l'integrazione Dolphin: cliccando "Scansiona con KlamAV-Py" su più
file/cartelle, tutte le richieste finiscono nella stessa finestra già
aperta.
Integrazione Dolphin

Dal pannello Impostazioni si può installare la voce "Scansiona con
KlamAV-Py" nel menu contestuale di Dolphin, scrivendo un file
.desktop in ~/.local/share/kservices5/ServiceMenus/ e
~/.local/share/kio/servicemenus/ e rigenerando la cache dei servizi
KDE (kbuildsycoca5/6). L'Exec= invoca direttamente il comando
senza shell intermedia: un nome file con ", ` o $ non può
rompere il quoting.
Autostart

Se abilitato, scrive un file .desktop in ~/.config/autostart/ che
lancia la GUI con l'interprete Python correntemente in uso; se
disabilitato, rimuove il file.
Scansioni pianificate: i tre meccanismi

    Timer della GUI (pagina Pianificazione): gira solo con l'app
    attiva (anche in tray), ma offre progresso live, log persistente e
    integrazione completa con la UI.
    Unit systemd utente (installata dal .deb): parte al login di
    ciascun utente senza configurazione, funziona a GUI chiusa. Per far
    girare il timer anche a utente scollegato:
    loginctl enable-linger $USER. Usa lo stesso percorso di quarantena
    di default della GUI (~/.local/share/klamav-py/quarantine).
    Unit systemd di sistema (systemd/ nel repo, solo per
    installazioni manuali multi-utente/server): da configurare ed
    abilitare a mano; la ExecStart assume un venv sotto
    /opt/klamav-py/venv, ma la CLI gira anche senza venv. Scenario
    distinto dalla unit utente del .deb, non la stessa unit installata
    in due modi.

Diagnosi degli errori comuni

Su una scansione home-wide di una macchina di sviluppo reale (330k+
file) gli errori residui sono fisiologici e riconoscibili:

     "Pipe interrotta" su file grandi — clamd ha rifiutato lo stream
    per StreamMaxLength (25MB di default in clamd.conf) chiudendo la
    connessione a metà invio. Con il pre-check dimensionale attivo è
    quasi scomparso: i file oltre soglia escono come "non verificati"
    senza toccare la sessione. Per alzare il limite:
    StreamMaxLength 100M in clamd.conf e riavvio di clamd — con il
    tradeoff che un limite più alto rende più facile intasare clamd con
    stream enormi.
    Nota di onestà: anche i file entro il limite di stream restano
    soggetti a MaxScanSize/MaxFileSize/MaxRecursion di clamd
    (default approssimativi 100M/25M/16): un archivio corposo può
    risultare "OK" con scansione parziale — limite di clamd stesso, non
    rilevabile da questo progetto.
     Errno 13 "Permesso negato" — permessi reali del filesystem (es.
    file posseduti da container Docker dentro la home). Non è un
    problema del progetto.
     Errno 2 "File o directory non esistente" — file spariti durante
    la traversata (Trash svuotato, sync cloud che desincronizza, browser
    che riscrive la cache). Rumore atteso.
     "timed out" — tipicamente file grandi letti via mount di rete
    (Nextcloud, NFS): latenza, non malfunzionamento.
     Errori a raffica di un solo tipo (migliaia) — quasi certamente
    clamd morto o riavviato a metà scansione:
    journalctl -u clamav-daemon.service nell'intervallo della
    scansione, e dmesg | grep -i oom per il caso OOM. Il riepilogo
    per tipo della CLI (o il log persistente della programmata) rende
    questo caso immediatamente distinguibile dal rumore fisiologico.

Fix di sicurezza nella 0.1.4

Questa versione risolve una serie di problemi di sicurezza emersi da un
audit del codice. Il modello di minaccia di riferimento è un sistema
multi-utente e, per la quarantena, il file infetto stesso trattato come
contenuto potenzialmente ostile (non un aggressore esterno).

     TOCTOU in quarantena e ripristino. quarantine_file() apre il file
    con O_NOFOLLOW e verifica l'identità dell'inode (dev+ino) dopo lo
    spostamento, invece di fidarsi di un controllo is_file() separato
    dall'operazione di move: un file infetto potrebbe tentare di evadere
    l'isolamento sostituendosi con un symlink nella finestra tra il
    controllo e lo spostamento. I symlink (e i file speciali come le
    FIFO) sono ora rifiutati esplicitamente. restore() reclama la
    destinazione in modo atomico con O_CREAT|O_EXCL invece di un
    exists() seguito da move(), eliminando la finestra in cui un altro
    processo poteva creare il file di destinazione nel mezzo.
     Socket IPC single-instance ristretto e validato. Il QLocalServer
    usa UserAccessOption: senza, su Linux i permessi del socket
    dipendono dallo umask del processo e potrebbero risultare
    accessibili ad altri utenti del sistema. Il percorso ricevuto dal
    socket è ora limitato in dimensione (evita payload enormi pensati
    come DoS) e decodificato in modo robusto (un payload UTF-8
    malformato viene ignorato, non fa propagare eccezioni).
     Aggiornamento database senza stringa di shell costruita a runtime.
    L'operazione via pkexec non passa più una stringa di comandi
    costruita in Python a sh -c: esegue uno script fisso spedito col
    pacchetto (klamav_py/gui/resources/freshclam-update.sh). Elimina
    alla radice la possibilità che un futuro parametro reso configurabile
    finisca interpolato in una riga di shell eseguita come root.
     Rigenerazione cache servizi KDE senza shell. os.system() è stato
    sostituito da subprocess.run() con argv esplicito (nessuna shell).
     Permessi dei file .desktop dell'integrazione Dolphin corretti da
    0755 a 0644, in linea con lo standard freedesktop.org (sono file di
    configurazione, non eseguibili).

I fix sono coperti da 9 nuovi test automatici (rifiuto di symlink e FIFO
in quarantena, verifica del reclamo atomico della destinazione nel
ripristino, validazione del payload IPC).

Nota sulla 0.1.4-2 (correttezza, non sicurezza)

La radice di ogni scansione viene ora risolta (Path.resolve()) all'inizio
di scan_stream, in un unico punto comune a tutti i modi di avviare una
scansione (CLI, GUI manuale, integrazione Dolphin/IPC, e in prospettiva
Real-Time e programmata). Non è un fix di sicurezza: nel caso dell'IPC il
socket è già ristretto allo stesso utente, quindi non esiste un confine di
privilegio da proteggere e un symlink non dà accesso a nulla che l'utente
non possa già leggere. È un fix di correttezza: se il percorso da
scansionare è un symlink-directory, os.walk() lo segue comunque quando è
il punto di partenza (followlinks=False blocca solo i symlink interni
all'albero, non la radice), e senza risolverlo il referto di scansione
mostrerebbe i risultati sotto il percorso del symlink invece che sotto
quello reale effettivamente letto. Per un antivirus conta il contenuto
reale controllato, non l'alias da cui ci si è arrivati.

La risoluzione avviene una sola volta sulla radice, non per ogni file:
_iter_files (l'attraversamento vero e proprio) resta invariato, così sia
lo streaming a memoria costante sia il pruning delle directory escluse
(quarantena inclusa) continuano a funzionare esattamente come prima. I
symlink interni all'albero continuano a non essere seguiti. Il fix è
coerente con scan_file() (CONTSCAN), che già risolveva il proprio path.

Cosa manca rispetto a KlamAV originale (di proposito)

     Nessun on-access scanning kernel-level. Il monitoraggio Real-Time
    è basato su QFileSystemWatcher (modifiche a directory osservate
    esplicitamente, non ricorsivo, con qualche secondo di latenza) —
    non è un sostituto di un vero on-access scanner. Per quello ClamAV
    fornisce già clamonacc (fanotify-based): non ha senso
    reimplementare un equivalente di Dazuko. Se serve on-access reale,
    si configura via clamd.conf/clamav-clamonacc.service.
     Nessuna integrazione mail client-side. Se serve scansione della
    posta, oggi ha più senso lato server (milter) che lato client
    KMail/Evolution come faceva klammail.

Sviluppo e test
bash
 
  
 
 
cd klamav-py
python3 -m venv venv        # se non l'hai già creato
source venv/bin/activate
pip install -r requirements-dev.txt
python3 -m pytest tests/
 
 

I test coprono la logica pura senza clamd reale (parsing del protocollo
clamd, gestione quarantena su filesystem temporaneo, CLI) e la GUI con
istanziazione offscreen delle pagine e simulazione di segnali/azioni
(ripresa Scansione↔Quarantena, elisione stato, appunti, pausa/ripresa
con client finto iniettato via client_factory).

Su distribuzioni con Python "externally managed" (Debian/Ubuntu
recenti, PEP 668), pip install -r requirements-dev.txt fuori da un
venv fallisce con externally-managed-environment: comportamento
atteso del sistema, usa il venv.
Licenza

KlamAV-Py è rilasciato sotto licenza GPL-3.0-or-later (vedi il file
LICENSE). Codice scritto da zero: non contiene codice proveniente da
KlamAV 0.22, di cui è solo un erede spirituale dell'idea.
Pacchettizzazione .deb

Lo scheletro Debian è in debian/, testato con build reale
(dpkg-buildpackage) e installazione/rimozione complete
(dpkg -i / apt-get remove --purge).

Verifica prima di installare: lo scheletro è stato validato in un
ambiente Ubuntu 24.04, dove python3-pyside6.qtcore e affini non
esistono nei repository (Ubuntu pacchettizza solo PySide2). Su Debian
dovrebbero esistere, ma prima di installare il .deb controlla con
apt-cache search pyside6 che i pacchetti elencati in
debian/control (python3-pyside6.qtcore, .qtgui, .qtwidgets,
.qtnetwork) esistano nella tua versione, e correggi i nomi se
differiscono.
Cosa fa lo scheletro Debian

     

    Un unico pacchetto binario klamav-py con CLI e GUI (niente
    split, finché il progetto è giovane).
     

    pyproject.toml definisce due entry point in /usr/bin/:
    klamav-py (CLI) e klamav-py-gui (GUI). La GUI li usa per
    auto-rilevare il comando da scrivere nei .desktop di autostart e
    Dolphin (_gui_relaunch_command()), invece di assumere una checkout
    locale con venv.

    Il campo license usa deliberatamente il vecchio formato tabella
    ({text = "..."}) invece della stringa SPDX: setuptools recenti
    deprecano il primo con un warning, ma setuptools vecchi (verificato:
    68.1.2, Ubuntu 24.04) rifiutano il secondo con errore fatale.
    Meglio un warning cosmetico ovunque che un build rotto su alcuni
    sistemi.
     

    Unit systemd a livello UTENTE (systemd --user), non di sistema
    (perché, vedi sotto). I file si chiamano
    debian/klamav-py.klamav-scan.user.service/.timer: il segmento
    .user. nel nome è obbligatorio perché dh_installsystemduser
    li riconosca — scoperto solo testando empiricamente un build reale.
    Vanno a finire in /usr/lib/systemd/user/.
     

    postinst/postrm usano deb-systemd-helper --user: il symlink di
    abilitazione va in /etc/systemd/user/timers.target.wants/ (vale per
    tutti gli utenti) e il timer parte al prossimo login di ciascuno,
    senza comandi manuali.
     

    Una unit utente non può ordinarsi in modo affidabile rispetto a
    una unit di sistema come clamav-daemon.service (manager systemd
    separati, niente dipendenze cross-manager): il .service non ha
    After=/Requires= su clamd. In pratica non è un problema: clamd
    parte ben prima del login utente.

Perché unit utente e non di sistema

La primissima versione usava una unit di sistema con DynamicUser=yes.
Sembrava pulita (niente useradd manuale), ma non regge a uso reale:

     Le home nascono 0700/0750: un utente dinamico non legge nulla, e
    la scansione di /home produceva solo Permission denied a raffica.
     La quarantena finiva in /var/lib/klamav-py, di proprietà
    dell'utente dinamico: la GUI non poteva né vederla né gestirla.

L'unit utente gira come l'utente, legge la home senza problemi e usa la
stessa quarantena di default della GUI: i percorsi coincidono.

Upgrade da versioni precedenti (che avevano la unit di sistema):
la rimozione del file della vecchia unit non ferma un'istanza già
attiva — il symlink in /etc/systemd/system/timers.target.wants/ non
è tracciato da dpkg. Per questo postinst include una migrazione
dedicata che ferma e disabilita esplicitamente la vecchia unit di
sistema prima di abilitare la nuova utente (verificato leggendo il
postinst del .deb compilato).
Da rivedere prima di una release stabile

     Versione hardcoded sia in pyproject.toml che in
    debian/changelog: da tenere sincronizzata manualmente ad ogni
    release finché non si automatizza.
     Nessun test automatico verifica l'installazione del .deb di per sé
    (verifica manuale): se il progetto cresce, vale un job CI con
    sbuild/pbuilder che lo ricostruisce da zero.

Estensioni naturali

     Circuit breaker sui risultati: se clamd muore a metà scansione,
    ogni file rimanente produce un errore "impossibile aprire sessione"
    (il meccanismo dietro un episodio osservato di ~10.200 errori). Un
    contatore di errori consecutivi oltre soglia interromperebbe la
    scansione con un fallimento rapido e leggibile invece del flood.
     Esclusioni configurabili da UI (oggi solo via --exclude CLI):
    cache, Trash, mount di rete — ridurrebbe ulteriormente il rumore e
    accelererebbe le scansioni home-wide.
     Differenziare il nome del QLocalServer per sys.prefix: su una
    macchina di sviluppo con checkout+venv e .deb installati insieme,
    installato e checkout non si contenderebbero la stessa istanza
    single-instance.
     Notifiche desktop native (notify-send o D-Bus diretto) oltre a
    QSystemTrayIcon.showMessage, per desktop con supporto tray
    irregolare.
     Esportazione dell'indice quarantena in un formato leggibile da un
    frontend web/TUI (es. per controllare la quarantena dal NAS).
     Configurabile da UI se il Real-Time debba mettere in quarantena
    automaticamente o solo segnalare.
