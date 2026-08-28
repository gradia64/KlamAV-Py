Changelog

Formato libero (progetto singolo autore). Le voci di rilascio sono inordine anti-cronologico; le analisi di audit complete sono conservate infondo. Per i metadati del pacchetto Debian vedi debian/changelog.
0.1.3 (in lavorazione)

Emerso da test reali su home da 330k+ file (28-29/08) e seconda tornata di audit.

    Pausa/Ripresa nella pagina Scansione: granularità di file intero,stop funziona durante la pausa, sessione clamd ricreata proattivamentedopo pause > 25s (IdleTimeout). UI guidata dai segnali del worker.
    Guard "una scansione alla volta" manuale↔programmata (laprogrammata salta con voce in cronologia); Real-Time esente per design.
    Esclusione della directory di quarantena dall'attraversamento(pruning, non letta) + rifiuto strutturale di ri-quarantenare file giàin quarantena. Emerso dal campo: i 5 EICAR in quarantena venivanori-rilevati a ogni scansione home-wide.
    Pre-check dimensionale (TOO_LARGE): file > StreamMaxLength nonvengono più inviati a clamd; classificazione di fallback degli erroridi stream su file sopra soglia. Diagnosi di campo: i ~200 "Pipeinterrotta" corrispondevano ai 136 file >25M della home.
    Sessione IDSESSION marcata "dead" quando clamd chiude laconnessione: ricreazione prima del file successivo (elimina le"vittime collaterali" a cascata). reset_session() espostosull'istanza; finally sul generatore per l'abbandono (Interrompi).
    fcntl.flock sulle sequenze read-modify-write di index.json:GUI/CLI/worker concorrenti non si sovrascrivono più le entry.
    Log persistente delle scansioni programmate (~/.local/share/ klamav-py/logs/, rotazione 10) + progresso live in paginaPianificazione e tooltip tray. Prima: dettaglio irrecuperabile.
    Cronologia: campo too_large (default 0 per voci vecchie) ecolonna "Non verificati"; tooltip con il percorso del log.
    Real-Time: file troppo grandi etichettati "Non verificato", nonpiù "Analizzato (Sicuro)".
    CLI: --exclude ripetibile, --max-stream-size, pathcanonicalizzati con resolve() per un matching affidabile delleesclusioni; esclusione automatica della dir di --quarantine.
    Traversata da rglob a os.walk con pruning e memoria costante.
    Aggiunti 17 nuovi test (45 totali): pausa/ripresa/stop durante lapausa con client finto deterministico, pruning delle esclusioni(inclusi symlink a directory), pre-check e fallback TOO_LARGE,serializzazione flock dell'indice, rifiuto di ri-quarantenare.

0.1.2 — 5 problemi corretti (audit)

Un'analisi indipendente del codice ha individuato altri 5 problemi primaancora di arrivare a Flathub, tutti corretti in questa versione:

    Permessi dei file in quarantena. shutil.move() da solo nonneutralizza nulla: un file infetto 0755 arrivava in quarantenaancora eseguibile. Corretto in quarantine.py: la mode originaleviene salvata prima dello spostamento e ripristinata al restore(altrimenti un file ripristinato restava bloccato ai permessi diquarantena per sempre), il file in quarantena è ora 0400(read-only, non eseguibile), il nome su disco non è più prevedibile(timestamp+UUID invece del nome file originale), un restore su unpercorso nel frattempo rioccupato viene rifiutato esplicitamenteinvece di sovrascrivere silenziosamente, e index.json vienescritto in modo atomico (file temporaneo + os.replace()) per nonrischiare un indice corrotto in caso di crash a metà scrittura.5 nuovi test coprono tutti questi casi, incluso uno di compatibilitàcon index.json scritti dalle versioni precedenti (senza il nuovocampo original_mode).
    DynamicUser + scansione di /home. Passato a unit systemdutente (la motivazione completa è nel README, sezione "Perché unitutente e non di sistema").
    StreamMaxLength di clamd. I file oltre il limite (tipicamente25MB, clamd.conf) venivano contati come errori generici. Ora hannouno status dedicato (TOO_LARGE) sia nel client (clamd_client.py)sia nella CLI (riga di riepilogo separata: "N file oltreStreamMaxLength, non verificati") sia nella GUI (icona e coloreneutri, non rossi come un errore vero). Nota correlata dionestà: anche i file entro il limite di stream restano soggetti aMaxScanSize/MaxFileSize/MaxRecursion di clamd (defaultapprossimativi 100M/25M/16): un archivio corposo può risultare "OK"con una scansione parziale — è un limite di clamd stesso, nonqualcosa che klamav-py possa rilevare o aggirare.
    Limiti inotify / QFileSystemWatcher. Due problemi distinti:addPath()/addPaths() possono fallire in silenzio sefs.inotify.max_user_watches/max_user_instances è esaurito(verifica con cat /proc/sys/fs/inotify/max_user_watches), e se unacartella monitorata viene eliminata e ricreata (es. pulizia cache diun browser) il watch sottostante muore e non torna da solo. Correttocon: (1) controllo del valore di ritorno di addPaths(), con lostato mostrato in Impostazioni ("Real-Time attivo su N/M cartelle",con l'elenco delle cartelle non coperte se qualcuna fallisce); (2)una riconciliazione periodica ogni 60 secondi che confronta lecartelle effettivamente osservate con quelle configurate eri-aggiunge quelle mancanti. Il monitoraggio resta comunque nonricorsivo (limite già noto e documentato): i file creati insottocartelle di una cartella monitorata non vengono visti.
    pkexec sh -c in update_worker.py. Non era (e non è)iniettabile: la stringa passata è statica, senza interpolazione diinput esterno. Aggiunto però un commento esplicito e ben visibilesopra la stringa di comando, a scoraggiare una futura modifica chela renda interpolata per errore — è l'unico punto del progetto incui la sicurezza dipende dalla disciplina di chi tocca quel codicein futuro, non da un vincolo strutturale del linguaggio.

Verificati con test automatici mirati dove possibile (nuove famiglie ditest: permessi e atomicità della quarantena, classificazioneTOO_LARGE, riconciliazione dei watch Real-Time) e, per lariconciliazione Real-Time, con un test funzionale diretto sulla logicadi osservazione/fallimento — non sono però riuscito a riprodurre inmodo affidabile in questo ambiente di sviluppo lo scenario esatto"cartella eliminata e ricreata" che dovrebbe far morire un watchinotify (il watch è sopravvissuto al ciclo nel mio test, probabilmenteper timing troppo rapido o per una specificità di questo ambienteoffscreen): il meccanismo di rilevamento e segnalazione di unfallimento di addPaths() è invece verificato con certezza.
0.1.1 — Bug report pre-release, 4 problemi risolti

Il bug report è emerso prima del rilascio 0.1.0; i fix sono statirilasciati nella 0.1.1 (vedi debian/changelog).

Un test reale su Debian Sid / KDE Plasma 6 (scansione della Home,oltre 334.000 file, EICAR via scansione manuale e Real-Time) haprodotto un bug report con 4 problemi, tutti corretti:

    Nessuna notifica/report di fine scansione. La scansione manualeora produce, al termine: un riepilogo esplicito con percorso, durata,file scansionati, infetti, errori ed esito, mostrato in un dialogose la finestra è visibile in quel momento (se l'app è minimizzata intray non viene forzata in primo piano: basta la notifica tray, giàpresente ma ora sempre inviata — prima un controllo isVisible()sull'icona tray poteva sopprimerla). La durata viene misurata datime.monotonic() all'avvio della scansione.
    Sezione Quarantena non aggiornata in tempo reale. Mettere unfile in quarantena — manualmente dalla pagina Scansione, oautomaticamente da una scansione pianificata/Real-Time — oraaggiorna subito la pagina Quarantena, senza dover riavviare l'app.ScanWorker ha un nuovo segnale quarantined, emesso subito dopoogni spostamento riuscito in quarantena e collegato, in tutti e trei punti che creano un ScanWorker (scansione manuale, pianificata,Real-Time), a un refresh della pagina Quarantena.
    Impossibile copiare il log della scansione. Nuovo pulsante"Copia log" nella pagina Scansione, accanto a "Metti in quarantena iselezionati": copia negli appunti di sistema tutte le righe dellalista risultati (infetti ed errori).
    Sfarfallio/ridimensionamento della finestra durante la scansione.Causa reale, confermata col codice sorgente: ScanWorker emettevaun segnale Qt per ogni singolo file scansionato (anche i puliti),aggiornando due QLabel a testo dinamico ad ogni file. Su unascansione di 334.000+ file questo significa altrettantiricalcoli di layout in rapida sequenza — è la causa dello sfarfallio,non un problema di sizePolicy/minimumSize come inizialmenteipotizzabile. Corretto rallentando lato worker la frequenza diaggiornamento a un tick ogni 150ms (il conteggio interno restapreciso al 100%, cambia solo quanto spesso viene mostrato) etroncando con elisione (… nel mezzo) il testo "Scansione in corso:⟨percorso⟩" a una larghezza massima fissa, così un percorso moltolungo non fa più crescere il sizeHint della label ad ogni update.I risultati "puliti" non producono più nemmeno un segnale (primane veniva emesso uno per ognuno, usato solo per il conteggio):ora result_ready viene emesso solo per infetti/errori.

Verificati con test funzionali mirati (istanziando le pagine inmodalità offscreen e simulando segnali/azioni), non solo a occhio: ilrefresh incrociato Scansione→Quarantena, l'elisione del testo distato, il contenuto copiato negli appunti e la formattazione delladurata sono tutti confermati con asserzioni automatiche. Non è statopossibile riprodurre un carico realistico di 334.000 file in questoambiente di sviluppo (nessun clamd reale disponibile): il fix allosfarfallio è stato validato leggendo la causa nel codice (frequenza diemissione dei segnali) piuttosto che riproducendo il sintomo a pienascala.
0.1.0

Rilascio iniziale: CLI + GUI, scansione via protocollo nativo di clamd(INSTREAM/IDSESSION), quarantena con indice JSON condiviso, Real-Time,pianificazione, integrazione Dolphin, pacchettizzazione .deb.
Audit 1 — Problemi noti (prima tornata, anteriore alla 0.1.1)

Snapshot storico conservato com'era: alcune affermazioni in esso (es."la GUI non è ancora coperta da test automatici") sono state superatedalle versioni successive — vedi 0.1.1.

Punti emersi rileggendo l'intero codice dopo le ultime modifiche.

Corretti:

    Avvio non più bloccante. Il ping a clamd all'apertura dellafinestra ora gira in un QThread dedicato (gui/ping_worker.py,PingWorker) invece che in modo sincrono nel thread della UI: lafinestra appare subito, l'eventuale avviso "clamd non raggiungibile"arriva in modo asincrono quando il ping risponde (o va in timeout).

    Quoting nel file .desktop di Dolphin. Rimosso il wrappersh -c '... "%f"': ora Exec= invoca direttamente l'interpretePython con %f come argomento, senza passare da una shellintermedia. Elimina il rischio che un nome file con ", ` o$ rompa il quoting — esattamente il tipo di problema che ilprogetto dichiara di aver eliminato rispetto a klamav 0.22.

    Gestione errori in _manage_autostart(). Ora ritorna(successo, messaggio_errore) invece di sollevare un'eccezione nongestita: se la scrittura in ~/.config/autostart/ fallisce (es. perpermessi), SettingsPage mostra un avviso invece di un traceback.

    Import inutilizzato in gui/scan_worker.py (ScanResult)rimosso; verificato con pyflakes che non ce ne siano altri.

    Sovrapposizione dei widget in Impostazioni al ridimensionamentodella finestra. SettingsPage chiedeva una dimensione minimarigida di 593×781px (parecchi widget a dimensione fissa: caselle ditesto e pulsanti alti 36px, la lista cartelle monitorate alta120px, testi lunghi nelle checkbox). Qt normalmente impedisce discendere sotto questo minimo, ma non tutti i window manager lorispettano rigidamente durante un ridimensionamento interattivo: selo ignorano, la finestra può finire più piccola di quanto i widgeta dimensione fissa richiedano. Il contenuto della pagina ora vivedentro un QScrollArea (minimo sceso a 68×68px, verificato con unconfronto screenshot prima/dopo): se lo spazio non basta piùcompare una scrollbar, non una sovrapposizione. Le due descrizionipiù lunghe (Real-Time, Dolphin) hanno anche setWordWrap(True) perridurre la larghezza minima naturale richiesta.

    Le altre pagine (ScanPage 382×415, QuarantinePage 397×218,SchedulerPage 358×338, ecc.) hanno minimi più contenuti e non sonostate toccate in questo giro perché non segnalate — usano lo stessopattern di widget a dimensione fissa, quindi in teoria sonoesposte allo stesso rischio su un window manager che ignora ivincoli di Qt, solo con soglie meno facili da raggiungere. Se infuturo si presenta lo stesso sintomo altrove, la correzione èidentica (avvolgere il contenuto in un QScrollArea).

    Test automatici aggiunti in tests/ (pytest) per la parte dilogica pura, senza dipendenze da clamd o da Qt: parsing dellerisposte del protocollo clamd (test_clamd_client.py), gestionedella quarantena su filesystem temporaneo (test_quarantine.py) eparsing/raggruppamento errori della CLI (test_cli.py). Si lancianodentro il venv (vedi il README per il setup):

    cd klamav-pypython3 -m venv venv        # se non l'hai già creatosource venv/bin/activatepip install -r requirements-dev.txtpython3 -m pytest tests/

  Su distribuzioni con Python "externally managed" (Debian/Ubuntu
  recenti, PEP 668), pip install -r requirements-dev.txt fuori
  da un venv fallisce con externally-managed-environment: è il
  comportamento atteso del sistema, non un problema di questo
  progetto — usa il venv come sopra, non --break-system-packages.

  La GUI (widget PySide6, worker QThread) non è ancora coperta da
  test automatici: richiederebbe pytest-qt o un mocking più
  strutturato di ClamdClient, valutabile come prossimo passo.

Non un problema — comportamento confermato intenzionale: la
quarantena sempre automatica nella sezione Real-Time (la motivazione
è nel README, nelle scelte di design).

Nessun problema di sicurezza rilevato in clamd_client.py, cli.py o
update_worker.py: quest'ultimo passa a pkexec sh -c una stringa di
comando statica, senza interpolazione di input proveniente dall'utente
o dal filesystem, quindi non è iniettabile.
