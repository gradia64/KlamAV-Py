# Changelog

Cronologia delle versioni per gli utenti, indipendente dalla
distribuzione. Il dettaglio esteso fino alla 0.1.3 e le tornate di audit
sono in `docs/CHANGELOG-archive.md`.

---
## 0.1.19 — 2026-11-05

### Aggiunto
- **La GUI avverte quando la scansione del timer è stata interrotta.** Con
  il PC spento a metà scansione la unit di notifica non dice nulla (un'uscita
  per segnale non è un errore da notificare) e `Persistent=true` non ripete
  l'attivazione, che risulta avvenuta: la home restava controllata in parte
  senza che nessuno lo sapesse. Ora la scansione del timer segna su disco il
  proprio inizio e la propria fine
  (`~/.local/share/klamav-py/timer-scan.json`): al primo avvio successivo la
  GUI trova l'inizio senza la fine, avvisa con una notifica della tray (una
  volta per interruzione) e lo dice nella pagina Pianificazione, con l'ora
  di avvio e il rimando alle pagine Segnalazioni e Quarantena per quanto
  trovato fino a lì. Le uscite con un codice non avvisano: quelle le notifica
  già la unit. Il marcatore riguarda solo il timer di sistema: un'interruzione
  di una scansione avviata dalla shell resta visibile nel terminale.
- **L'orario della scansione di sistema si sceglie dalla pagina
  Pianificazione.** Fino a ora la unit partiva ogni giorno a mezzanotte
  (`OnCalendar=daily`) e cambiarlo voleva dire scrivere a mano un drop-in di
  klamav-scan.timer. Ora il campo «Ogni giorno alle» della pagina Pianificazione
  scrive e rimuove quel drop-in al posto tuo (a mezzanotte non serve: è il
  valore della unit). Il ritardo casuale fino a 30 minuti della unit resta.
  Un drop-in scritto a mano che imposta `OnCalendar` d'ora in poi viene
  segnalato come avviso, perché l'orario scelto nella pagina potrebbe non
  avere effetto: chi aveva il drop-in manuale può toglierlo e salvare
  l'orario dalla GUI.
- Test: stato dell'ultima scansione del timer (scritture best-effort della
  CLI, lettura severa), `service_active` per distinguere una scansione in
  corso da una interrotta, drop-in dell'orario (golden, `systemd-analyze
  verify`, conflitti e override estranei), orario ed esclusioni scritti
  insieme senza cortocircuiti. Totale: 1124 test.

---
## 0.1.18 — 2026-10-29

### Modificato
- **Le segnalazioni della scansione programmata di sistema compaiono
  nella pagina Segnalazioni.** I file solo segnalati (firme euristiche,
  archivi di posta) trovati dal timer di sistema stavano solo nel
  journal: la notifica del mattino diceva «file infetti», ma per sapere
  quali bisognava cercarli con `journalctl`. Ora ogni scansione (timer,
  riga di comando, finestra) li registra in
  `~/.local/share/klamav-py/pending-reports.json` appena li trova, anche
  se poi viene interrotta, e la pagina Segnalazioni li mostra con le
  stesse azioni di prima, anche dopo un riavvio della finestra. Una voce
  esce quando la elimini o ne registri la presa visione (anche da riga di
  comando) o quando il file non c'è più. La notifica del timer rimanda
  alla pagina. Dopo l'aggiornamento chiudere KlamAV-Py con «Esci» e
  riaprirlo (una finestra rimasta aperta usa ancora la versione
  precedente); poi, dopo la prossima scansione del timer con una
  segnalazione, aprire la pagina Segnalazioni.

### Corretto
- **GUI: un possibile crash dopo un errore in un'operazione in
  background.** Quando il caricamento della quarantena falliva, o una
  scansione non partiva per una cartella non leggibile, il worker restava
  in memoria finché il garbage collector di Python non lo liberava, anche
  da un altro thread: per Qt è un errore che può far chiudere la GUI di
  colpo, in un momento qualunque. Ora quegli oggetti si liberano subito.
  Chi aggiorna non deve fare nulla.

### Aggiunto
- Test: dopo ogni test della GUI la suite controlla che nessun oggetto
  Qt resti tenuto in vita solo da un ciclo di riferimenti, la causa del
  segfault visto nella CI con Python 3.12 durante il rilascio della
  0.1.17; registro delle segnalazioni da valutare, scritto dalla CLI
  anche in una scansione interrotta e dal worker della GUI, e pagina
  Segnalazioni che lo legge, con presa visione, eliminazione e voci tolte
  quando il file non c'è più. La pagina man della CLI nomina anche
  `.<nome>.intent-pending`. Totale: 1082 test.

---
## 0.1.17 — 2026-10-22

### Modificato
- **Ripristino, eliminazione e «Metti in quarantena» non bloccano più la
  finestra.** Con la quarantena, o i file da spostare, su un disco lento
  o di rete, queste azioni bloccavano la finestra fino alla fine. Ora
  girano in background, una alla volta: durante l'operazione Ripristina
  ed Elimina sono disattivati e «Metti in quarantena» chiede di
  attendere. Conferme e messaggi sono quelli di prima.
- **Quarantena: le operazioni interrotte e riprese si vedono.** Quando
  una quarantena interrotta (per esempio da un crash) viene completata o
  annullata, o il suo intento non è valido e viene messo da parte, la
  pagina Quarantena lo dice, con il percorso originale e l'esito, e se la
  pagina non è visibile arriva una notifica della tray. La CLI scrive la
  stessa riga su stderr prima della scansione; il codice di uscita non
  cambia. Prima l'esito era registrato ma nessuno lo mostrava.

### Corretto
- **Quarantena: un'operazione non perde più il suo intento per un
  recupero concorrente.** Se un'altra istanza (GUI, CLI, timer) eseguiva
  il recupero delle operazioni interrotte nell'istante in cui una
  quarantena registrava il proprio intento, poteva scambiarlo per un
  residuo e cancellarlo: un'interruzione successiva lasciava il file in
  quarantena senza voce, non ripristinabile dalla GUI. Ora l'intento
  diventa visibile al recupero solo quando è completo. Chi aggiorna non
  deve fare nulla: un eventuale `.<nome>.intent-pending` rimasto nella
  cartella di quarantena da un crash viene tolto al primo avvio.
- **Quarantena: un file spostato resta ripristinabile anche se l'indice
  non si scrive.** Se dopo lo spostamento in quarantena la scrittura
  dell'indice falliva (disco pieno, errore di I/O), il file restava nella
  cartella di quarantena senza voce e la GUI non poteva ripristinarlo.
  Ora l'errore dice che il file è in quarantena e che la voce verrà
  completata, e la completa il prossimo recupero: all'avvio della CLI o
  della GUI, o premendo «Aggiorna» nella pagina Quarantena (che ora
  ripete il recupero, senza riavviare).
- **Una scansione non resta più «in corso» per sempre.** Se la
  scansione della GUI non poteva partire per la cartella di quarantena
  (per esempio un file al posto della cartella) o per il client di clamd,
  la pagina Scansione restava «in corso» fino al riavvio e la scansione
  programmata interna non lasciava traccia. Ora entrambe la registrano
  come non completata, con il motivo nello stato, nel referto e nella
  Cronologia, come la CLI («Quarantena non utilizzabile»).
- **Scansione manuale: i problemi segnalati contano per l'esito.** Un
  problema durante la scansione che non riguarda un file (per esempio il
  registro delle prese visione non valido, o una quarantena automatica
  fallita) compariva in lista come «ERRORE SISTEMA», ma l'esito restava
  «Completata senza problemi» con la spunta verde. Ora, come nella
  programmata interna, l'esito è «Completata con errori» con l'icona di
  avviso, e il referto dice quanti problemi ci sono stati.
- **Avviso del drop-in all'avvio: «rimosso» quando è stato rimosso.**
  Con le impostazioni tornate tutte predefinite il drop-in della
  scansione programmata di sistema viene tolto all'avvio, ma l'avviso
  diceva che era stato «aggiornato alla versione …». Ora dice che è stato
  rimosso.

### Aggiunto
- Test: gara deterministica fra un recupero concorrente e la
  registrazione dell'intento; indice, sola lettura o rimozione
  dell'originale che falliscono dopo lo spostamento, con il recupero che
  completa la voce; esiti del recupero con lo stesso testo nella pagina
  Quarantena e su stderr della CLI, e notifica della tray una volta;
  nessuna chiamata al filesystem dal thread della GUI durante ripristino,
  eliminazione e «Metti in quarantena», e chiusura che aspetta
  l'operazione in corso; scansione manuale e programmata interna non
  eseguite per la quarantena o il client; problemi della scansione
  manuale nell'esito; avviso del drop-in rimosso. Totale: 1057 test.

---
## 0.1.16 — 2026-10-15

### Modificato
- **GUI con pip su Python 3.10 o 3.11: PySide6 resta alla 6.11.x.** La
  0.1.15 escludeva solo PySide6 6.12.0, che su quelle versioni fa
  abortire la GUI alla prima scansione. Qt ha confermato
  ([PYSIDE-3474](https://qt-project.atlassian.net/browse/PYSIDE-3474)) che
  dalla 6.12 le wheel sono compilate apposta per Python 3.12, quindi
  varrebbe anche per le versioni successive: `requirements.txt` e l'extra
  `gui` ora chiedono PySide6 < 6.12 con Python < 3.12. Non riguarda i
  pacchetti Debian e Arch, né Python 3.12 e successivi. Chi usa un venv
  con Python 3.10 o 3.11: ripetere `pip install -r requirements.txt`, poi
  `python -c "import PySide6; print(PySide6.__version__)"` deve dire 6.11
  o precedente.

### Corretto
- **Impostazioni non valide all'avvio: l'avviso dice la causa giusta.**
  Con un valore salvato non valido (per esempio una porta TCP non numerica
  in un file di impostazioni modificato a mano) la pagina Pianificazione
  diceva che il drop-in del timer era «scritto da una versione precedente»
  e non si poteva aggiornare, anche se era già aggiornato o non esisteva.
  Ora l'avviso nomina l'impostazione e rimanda alle Impostazioni, e il
  drop-in non viene toccato. Per controllare: nessuna azione richiesta;
  correggere il valore nelle Impostazioni e salvare fa sparire l'avviso.
- **Pagina Quarantena: pulsanti attivi solo quando servono.** Dopo un
  errore nel preparare la quarantena (per esempio una cartella che è un
  file) Ripristina ed Elimina restavano attivi senza fare nulla, e «Metti
  in quarantena» nella pagina Scansione rispondeva «riprova tra poco»
  anche se il caricamento non sarebbe ripartito da solo. Ora i due
  pulsanti sono attivi solo a quarantena caricata, e la pagina Scansione
  dice il motivo e di premere «Aggiorna» nella pagina Quarantena (durante
  il caricamento: di attendere).
- **Errori senza infetti: niente più spunta verde.** Con errori ma senza
  infetti né guasti di I/O, le notifiche della scansione manuale e della
  programmata interna mostravano la spunta verde, e il referto l'icona
  informativa, mentre l'esito diceva «Completata con errori». Ora ogni
  esito diverso da «Completata senza problemi» ha l'icona di avviso (le
  infezioni quella del virus), anche quando la programmata interna segnala
  problemi come il registro delle prese visione.
- Il README descriveva ancora una «unit systemd di sistema» nel
  repository, tolta nella 0.1.9: ora i meccanismi di pianificazione sono
  due (timer della GUI e unit utente), con il motivo per cui una unit di
  sistema non è più distribuita. Tolto anche `systemd/klamav-scan.timer`,
  rimasto dalla vecchia unit: nessun pacchetto lo installava, quindi chi
  aggiorna non deve fare nulla.

### Aggiunto
- Test e CI: le action di tutti i workflow sono fissate per SHA (prima
  solo quelle del workflow di rilascio), e il controllo dei secret non si
  lascia più ingannare da un « #» dentro una stringa; stato della pagina
  Quarantena e guardie con un caricamento in corso; drop-in rimosso
  all'avvio quando le impostazioni tornano predefinite; infetti e guasti
  di I/O insieme; stessa icona per lo stesso esito in notifiche e referto;
  nessun riferimento alla vecchia unit di sistema. I test che fanno
  abortire apposta un processo non lasciano più core dump né notifiche di
  crash nel desktop. Totale: 1023 test.

---
## 0.1.15 — 2026-10-08

### Modificato
- **Il timer di sistema riceve le opzioni nuove senza salvare nulla.** Il
  drop-in con cui quarantena, connessione e cartelle escluse della GUI
  arrivano al timer (`~/.config/systemd/user/klamav-scan.service.d/50-klamav-py.conf`)
  restava alla versione che lo aveva scritto, finché non si salvavano
  Impostazioni o Pianificazione: dalla 0.1.14 senza `--log-errors`. Ora la
  GUI, all'avvio, riscrive un drop-in suo rimasto a una versione precedente
  e lo dice nella pagina Pianificazione. Un file con quel nome scritto a
  mano (senza la prima riga `# Generato da KlamAV-Py`) non viene toccato.
  Dopo l'aggiornamento: chiudere KlamAV-Py con «Esci» dal menu della tray e
  riaprirlo, perché una finestra già aperta continua a eseguire la versione
  precedente. Per controllare: `systemctl --user cat klamav-scan.service`
  mostra `--log-errors` nell'ultima riga `ExecStart=`. Senza drop-in (mai
  salvato nulla di diverso dai predefiniti) la riga è quella della unit
  installata, che ha già l'opzione.
- **I guasti di I/O si vedono anche nella GUI.** Un errore di lettura che
  non è un permesso negato (un disco che degrada, un mount NFS che non
  risponde) già contava fra gli errori, ma nella GUI non si distingueva da
  un file illeggibile per permessi. Ora lo stato e il referto della
  scansione manuale, la notifica e il log della pianificazione interna
  dicono, con la stessa frase dell'ultima riga del riepilogo della CLI, che
  parte dell'albero non è stata controllata; la Cronologia ha la colonna
  «Guasti di I/O». Le voci già salvate valgono zero guasti. Nella CLI la
  frase dice ora «elencati fra gli errori» invece di «elencati sopra fra
  gli errori».

### Corretto
- **Il motivo di una scansione non completata resta in Cronologia.** La
  voce diceva solo «Manuale (non completata)» o «Programmata (non
  completata)»: il motivo (cartella inesistente o non leggibile, clamd
  irraggiungibile) stava nello stato e nel referto, che spariscono alla
  chiusura. Ora è nella colonna «Motivo» della Cronologia, anche per una
  programmata rinviata. Le voci già salvate restano leggibili, senza
  motivo.
- **La finestra non si blocca più sulla quarantena.** All'avvio e al cambio
  di cartella nelle Impostazioni la GUI preparava la quarantena e
  recuperava le operazioni interrotte nel thread della finestra, e la
  pagina Quarantena rileggeva l'indice allo stesso modo: con la quarantena
  su un mount di rete lento o su un disco che degrada, la finestra restava
  bloccata. Ora lo fa in background, e la pagina Quarantena mostra il
  caricamento e poi l'elenco. Un errore nel preparare la cartella, che
  prima impediva l'avvio della GUI, compare nella pagina Quarantena. Il
  recupero è lo stesso di prima.
- Il pacchetto Arch non dipendeva da `hicolor-icon-theme`, pur
  installando l'icona in quel tema (errore di namcap).
- **GUI installata con pip su Python 3.10 o 3.11: esclusa PySide6
  6.12.0.** Quella versione toglie un riferimento a `True` a ogni segnale,
  e con Python precedente alla 3.12 la GUI abortisce dopo poche centinaia
  di segnali, cioè alla prima scansione (`Fatal Python error:
  bool_dealloc`). `requirements.txt` e l'extra `gui` la escludono per
  quelle versioni di Python. Non riguarda i pacchetti Debian e Arch, né
  Python 3.12 e successivi. Chi usa un venv: ripetere `pip install -r
  requirements.txt`, poi `python -c "import PySide6;
  print(PySide6.__version__)"` non deve dire 6.12.0.

### Aggiunto
- Rilascio automatico al push di un tag firmato: il workflow verifica che
  il tag sia firmato dal maintainer, ripete i test, costruisce il `.deb`
  su Debian sid e il pacchetto Arch dal tag firmato, firma tutti gli
  allegati (`.sig` e `SHA256SUMS`), crea la release GitHub con le note di
  questo file e aggiorna il pacchetto su AUR. Gli allegati sono firmati
  con una sottochiave dedicata alla CI, separata da quella che firma i
  tag: per verificarli serve la chiave pubblica aggiornata, da reimportare
  con i comandi del README («Verifica dei rilasci») anche se l'avete già.
  Per controllare: `gpg --list-keys --with-subkey-fingerprints
  EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9` elenca due sottochiavi `[S]`,
  fra cui `F9F4283586602F8DD724C467088861B04D8D328D`; con la chiave
  vecchia ce n'è una sola, e `gpg --verify` sugli allegati segnala la
  chiave pubblica mancante. Il pacchetto AUR e `git tag -v` non richiedono
  l'aggiornamento: i tag sono firmati come prima.
- Test: rigenerazione del drop-in all'avvio e nessuna chiamata a
  `systemctl --user` dal thread della GUI, guasti di I/O e motivo nella
  GUI e in Cronologia (anche con voci della 0.1.14), recupero della
  quarantena fuori dal thread della GUI con il filesystem strumentato,
  guardia sulla HOME dei test estesa a ogni lancio della CLI, secret dei
  workflow solo nel job di pubblicazione, refcount di `True` dopo
  l'emissione di segnali (PySide6 6.12.0). Totale: 986 test.

---
## 0.1.14 — 2026-10-02

### Modificato
- **Guasti di I/O: la scansione non vale più come pulita.** Un errore di
  lettura che non è un permesso negato né un file sparito (un disco che
  degrada, un mount NFS che non risponde più), su un file o su una
  sottocartella, faceva uscire la CLI con 0: sotto il timer di sistema,
  nessuna notifica. Ora, senza infezioni, l'uscita è 2 e la notifica
  scatta; con infezioni resta 1, e l'ultima riga del riepilogo segnala
  comunque il guasto, e la notifica invita a controllarlo. Nella 0.1.13 un
  guasto su una sottocartella finiva inoltre fra le «cartelle non
  leggibili», con il suggerimento di escluderla: ora è una riga «ERRORE»
  con la cartella e il motivo. Fra le cartelle non leggibili restano solo
  i permessi negati, che come i file spariti non cambiano il codice di
  uscita. Un errore di lettura a metà file non viene più riportato come
  «sessione clamd interrotta».
- La scansione programmata di sistema scrive errori e cartelle non
  leggibili dell'ultima esecuzione anche in
  `~/.local/state/log/klamav-py/scan-errors.log`, oltre che nel journal.
  Chi ha salvato quarantena, connessione o cartelle escluse dalla GUI con
  una versione precedente ottiene l'opzione quando la GUI riscrive il suo
  drop-in: con la 0.1.14 a un salvataggio delle Impostazioni o della
  Pianificazione fatto dopo aver chiuso («Esci» dalla tray) e riaperto la
  GUI, perché una finestra aperta prima dell'aggiornamento esegue ancora
  la versione precedente; dalla 0.1.15 all'avvio, senza salvare (vedi la
  0.1.15).
- Un percorso da scansionare non valido ha un messaggio per ciascun caso:
  inesistente, collegamento simbolico rotto (con la destinazione), né
  directory né file regolare (`klamav-py scan /dev/null`, una FIFO), non
  leggibile. Prima `/dev/null` diceva «non è leggibile: Not a directory»
  e un collegamento rotto «percorso inesistente». Il codice di uscita
  resta 2. La riga «CARTELLA NON LEGGIBILE» ha lo stesso formato nella
  CLI e nella GUI.
- La conferma della presa visione (CLI e pagina Segnalazioni) dice il suo
  ambito: vale per il contenuto e la firma, in qualunque percorso, quindi
  una copia identica altrove, con la stessa firma, risulta già valutata.
  La pagina Segnalazioni dice anche di mostrare le segnalazioni della
  sessione corrente, e dove trovare quelle della scansione programmata di
  sistema.

### Corretto
- **Presa visione rifiutata sotto una cartella `--report-only`.** Un file
  sotto una cartella passata con `--report-only` era solo segnalato dalla
  scansione, ma `--acknowledge` lo rifiutava come «rilevamento da
  quarantena», perché ignorava quell'opzione (e `--quarantine-all`). Ora
  la regola è la stessa; senza le stesse opzioni il rifiuto spiega cosa
  passare.
- **Prese visione sparite senza spiegazione.** Con un registro delle
  prese visione danneggiato, `--acknowledge`, `--unacknowledge` e la
  pagina Segnalazioni lo mettevano da parte senza dire nulla:
  `--acknowledge` rispondeva «Presa visione registrata» e le vecchie
  segnalazioni tornavano nuove. Ora compare un avviso, su stderr o nella
  pagina, con il file `acknowledged.json.corrupt-*` in cui le prese
  visione precedenti restano, non cancellate. Il codice di uscita non
  cambia.
- **Scansione manuale non eseguita presentata come riuscita.** Una
  scansione dalla finestra che non partiva (cartella non leggibile o non
  valida, cartella esclusa che la contiene, clamd irraggiungibile)
  mostrava la riga d'errore, ma lo stato, il referto e la voce in
  Cronologia dicevano «Completata senza problemi». Ora dicono «Scansione
  non completata» con il motivo, e la Cronologia registra «Manuale (non
  completata)».
- Dalla finestra, un percorso da scansionare non valido ha lo stesso
  messaggio della CLI per ogni caso (un collegamento rotto non è più
  «non esiste»), e in una selezione multipla da Dolphin una destinazione
  sparita o non valida ferma la scansione con il suo motivo, invece di
  essere tolta in silenzio dall'elenco.
- Real-Time: un file sparito fra la modifica e la scansione risultava
  «Analizzato», con una voce in Cronologia per una scansione di zero
  file. Ora la riga dice «Non analizzato» con il motivo, senza voce in
  Cronologia.
- La scansione programmata interna non riportava nel suo log i problemi
  che la scansione manuale mostra in lista, come un registro delle prese
  visione danneggiato. Ora finiscono nel log e la notifica finale ne
  indica il numero.

### Aggiunto
- `--report-only` e `--quarantine-all` si possono indicare anche prima del
  comando, fra le opzioni globali, e valgono per `--acknowledge`: la
  presa visione va chiesta con le stesse opzioni della scansione. Con
  `scan` funzionano in entrambe le posizioni.
- Test su guasti di I/O di file e sottocartelle (con un clamd finto, in
  sessione e senza), radice non valida, registro
  delle prese visione messo da parte, esiti delle scansioni della GUI,
  policy di `--acknowledge`, ambito della presa visione; le man page
  devono descrivere ogni opzione in entrambe le lingue, e i sottoprocessi
  dei test usano una HOME temporanea; radice e Real-Time nella GUI, voci
  delle man page senza testo. Totale: 942 test.

---
## 0.1.13 — 2026-09-30

### Modificato
- Le cartelle che non si possono leggere durante una scansione hanno un
  contatore a parte, «cartelle non leggibili», nella finestra, nella
  notifica della scansione programmata, in Cronologia e nel riepilogo
  della CLI, con il percorso nella lista o nel log. Non contano fra gli
  errori: una cartella creata con sudo o il bind mount di un container
  darebbero sempre gli stessi errori e nasconderebbero quelli nuovi. Se
  sono attese, basta escluderle. Il codice di uscita della CLI non cambia.
- README e PKGBUILD: comando per importare la chiave di rilascio con il
  keyserver indicato esplicitamente (keys.openpgp.org o
  keyserver.ubuntu.com) oppure dal file `arch/klamav-py-release-key.asc`,
  con la verifica dell'impronta. `gpg --recv-keys` senza `--keyserver`
  usa il keyserver configurato, che può non avere la chiave e far
  fallire `makepkg`.

### Corretto
- **Scansione pulita di una cartella che non si può leggere.** Una
  cartella da scansionare di un altro utente, o senza permesso di
  lettura, terminava come «completata, 0 file, 0 errori». Ora la
  scansione non parte e lo segnala (la CLI esce con 2); la scansione
  programmata interna non conta come eseguita e viene ritentata. Prima
  anche le sottocartelle illeggibili erano saltate in silenzio.
- Un file di recupero della quarantena (`.<nome>.intent`) danneggiato o
  modificato a mano poteva impedire l'avvio della finestra, far uscire la
  CLI con il codice delle infezioni, rendere illeggibile l'indice della
  quarantena o far cancellare un file fuori dalla quarantena. Ora viene
  validato come l'indice e, se non è valido, messo da parte
  (`.<nome>.intent.corrupt-*`) senza toccare nulla.
- Pianificazione: spuntare «Attiva» mentre il salvataggio verificava le
  cartelle attivava la pianificazione interna senza verificarne la
  cartella e senza chiedere del timer di sistema. Ora si salvano i valori
  presenti al clic su Salva.
- Una scansione programmata interna che non riesce a partire veniva
  ritentata ogni minuto creando ogni volta un nuovo log: in dieci minuti
  sparivano i log delle ultime scansioni vere, e le loro voci in
  Cronologia puntavano a file inesistenti. Ora solo il primo tentativo
  scrive un log.
- Il salvataggio delle Impostazioni, contando i file della quarantena che
  si stava lasciando, completava o annullava come effetto collaterale le
  quarantene interrotte in quella cartella. Ora la legge soltanto.

### Aggiunto
- **Presa visione delle segnalazioni non spostate.** Le firme euristiche e
  i file negli archivi di posta sono solo segnalati, e la stessa
  segnalazione tornava a ogni scansione: con il timer di sistema, la
  stessa notifica ogni notte (il caso d'origine: due email di phishing
  rimaste nel cestino di KMail). Dopo aver verificato il file si può
  registrarne la presa visione, con `klamav-py --acknowledge PERCORSO` o
  dalla nuova pagina Segnalazioni: le scansioni successive lo riportano
  come «già valutato» e non lo contano fra gli infetti, finché il
  contenuto non cambia. Una segnalazione nuova notifica come prima, e una
  presa visione non nasconde mai un file che andrebbe in quarantena.
  `--list-acknowledged` e `--unacknowledge` elencano e revocano; le voci
  non più ritrovate da 90 giorni si tolgono da sole.
- Pagina **Segnalazioni** nella finestra: i file solo segnalati dalle
  scansioni della sessione, con presa visione ed eliminazione (solo se il
  file è ancora quello rilevato), e l'elenco delle prese visione.
- Test su cartelle non leggibili, intento della quarantena non valido,
  valori della Pianificazione al clic, log dei tentativi e prese visione
  (registro, CLI, pagina). Totale: 851 test.

---
## 0.1.12 — 2026-09-28

### Modificato
- **Controlli più severi al salvataggio.** Le Impostazioni rifiutano una
  cartella di quarantena che contiene la cartella della scansione
  programmata interna, e la Pianificazione rifiuta una cartella da
  scansionare che sta dentro la quarantena: in entrambi i casi la
  scansione programmata non avrebbe controllato nulla. Con la
  pianificazione interna attiva, la cartella da scansionare deve essere
  un percorso assoluto di una cartella esistente: un file o un percorso
  relativo venivano salvati, e la scansione poi non partiva o non
  escludeva nulla.
- Una scansione manuale della cartella di quarantena ora dà un errore
  invece di terminare con zero file.

### Corretto
- **Scansioni programmate che risultavano pulite senza aver controllato
  nulla.** Una quarantena che contiene la cartella della scansione
  programmata interna la escludeva per intero; lo stesso succedeva con
  un'esclusione diventata non valida dopo il salvataggio (per esempio un
  link simbolico ripuntato su una cartella che contiene quella da
  scansionare). Ora la scansione ricontrolla quarantena ed esclusioni
  prima di partire e, se il caso si presenta, non parte e lo segnala.
- La scansione programmata interna con clamd irraggiungibile risultava
  "completata: 0 infetti". Ora una scansione non completata lo dice nella
  notifica e in Cronologia ("Programmata (non completata)"), non conta
  come eseguita e viene ritentata, con una sola notifica e una sola voce
  in Cronologia finché il problema non si risolve. Vale anche per una
  cartella da scansionare che non esiste più, che prima produceva solo
  una notifica.
- Una quarantena interrotta (crash, spegnimento) fra lo spostamento del
  file e l'aggiornamento dell'indice lasciava il file in quarantena senza
  voce, non ripristinabile dalla finestra; con la cartella di quarantena
  su un altro disco l'originale infetto poteva restare nella sua cartella
  sotto un nome nascosto. Ora l'operazione viene completata o annullata
  al successivo avvio di KlamAV-Py o della CLI.
- **Blocchi dell'interfaccia.** Non bloccano più la finestra:
  - il controllo delle cartelle escluse e della cartella da scansionare
    nella pagina Pianificazione, mentre si scrive il percorso o si salva,
    quando una cartella è su un mount di rete irraggiungibile. Durante il
    controllo il pulsante di salvataggio resta disattivato;
  - la stessa verifica all'avvio di ogni scansione programmata;
  - le chiamate a `systemctl --user` (stato e disattivazione del timer di
    sistema, ricarica delle unit dopo il salvataggio di Impostazioni e
    Pianificazione): con il gestore utente di systemd lento o bloccato la
    finestra poteva restare ferma fino a 10 secondi.
- Con clamd via TCP su localhost, l'aggiornamento del database era
  disabilitato quando freshclam usa una `DatabaseDirectory` diversa da
  `/var/lib/clamav`, con un messaggio che parlava di un container. Ora si
  usa la cartella indicata in `freshclam.conf`.
- Il controllo aggiornamenti ignorava i suffissi di pre-release: con
  installata una versione candidata (per esempio 0.1.13-rc1) la versione
  finale non veniva segnalata.

### Aggiunto
- Test su quarantena interrotta, validazioni e chiamate a systemctl fuori
  dal thread dell'interfaccia, confronto delle versioni. Il test di
  regressione del crash dei QThread ora riproduce la gara in modo
  deterministico e non viene più saltato. Totale: 763 test.

## 0.1.11 — 2026-09-27

### Modificato
- **`klamav-py scan --exclude` rifiuta due casi che prima passavano in
  silenzio, con uscita 2.** Un'esclusione uguale al percorso da scansionare
  o che lo contiene: la scansione terminava con 0 senza aver controllato
  nulla. Un'esclusione che punta a un file: veniva ignorata e il file era
  scansionato comunque. Uno script che usa `--exclude` in questi modi va
  corretto. Una cartella che non esiste ancora, o fuori dal percorso da
  scansionare, produce solo un avviso.
- Pianificazione con il timer di sistema attivo: il dialogo ha tre esiti.
  "No" mantiene il timer e salva, invece di annullare tutto; per annullare
  c'è "Annulla". Le cartelle escluse si salvano in entrambi i casi.

### Aggiunto
- Cartelle escluse dalle scansioni programmate, dalla pagina
  Pianificazione. Una lista sola per la pianificazione interna e per il
  timer di sistema, controllata contro entrambe le cartelle da
  scansionare; ogni voce mostra il proprio esito (icona e tooltip). Si
  aggiungono con il selettore o scrivendo il percorso, anche di cartelle
  nascoste o non ancora esistenti. Rende superfluo un `override.conf` con
  `--exclude`: se ne esiste uno che ridefinisce `ExecStart`, il salvataggio
  avvisa che sostituisce queste impostazioni.
- Test su esclusioni, drop-in e aggiornamento con clamd via TCP. Totale:
  695 test.

### Corretto
- Con clamd via TCP l'aggiornamento del database riavviava il freshclam
  locale anche quando clamd usa un altro database, e riportava "nessun
  aggiornamento". Ora è disabilitato, con una spiegazione, per un host
  remoto, e per localhost quando clamd non usa il database di
  `/var/lib/clamav` (per esempio in un container). L'aggiornamento
  all'avvio non chiede più una password inutile in questi casi.
- La scansione programmata interna usava la cartella da scansionare così
  com'era salvata: con un link simbolico nel percorso la quarantena non
  veniva esclusa dalla traversata, e i suoi file erano riletti e inviati a
  clamd a ogni scansione (i risultati erano poi scartati). Ora il percorso
  è risolto, come nella CLI.

Chi usava un `override.conf` per le esclusioni del timer: dopo aver
inserito le stesse cartelle nella Pianificazione e salvato, il file si può
eliminare (`systemctl --user daemon-reload` subito dopo).

## 0.1.10 — 2026-09-26

### Sicurezza
- Tag dei rilasci firmati con una chiave GPG dedicata al progetto (impronta
  nel README). Il pacchetto AUR clona il tag e ne verifica la firma
  (`?signed`, `validpgpkeys`) invece di scaricare l'archivio non firmato.
- La quarantena non può più stare in una directory volatile. Con
  `PrivateTmp` la unit la creava nella sua `/var/tmp` privata, che systemd
  elimina a fine scansione: file infetti e indice persi senza avvisi.
  Rifiutate `/tmp`, `/var/tmp`, `/run`, `/dev` e i percorsi nascosti dalla
  sandbox; avviso per tmpfs e ramfs montati altrove.
- clamd via TCP apre la rete della unit solo per chi lo usa: il drop-in
  sovrascrive `PrivateNetwork` ed estende `RestrictAddressFamilies`, senza
  togliere il filtro. La unit spedita resta senza rete.

### Corretto
- clamd irraggiungibile (spento, socket senza permessi, host sbagliato)
  produceva un errore per file e uscita 0: la scansione programmata non
  notificava nulla. Ora esce con 2, anche se clamd sparisce a scansione
  iniziata; un riavvio breve di clamd viene tollerato.
- Errori di connessione e di creazione della quarantena uscivano come
  traceback con codice 1, cioè "infezioni trovate".
- `klamav-py scan` con una quarantena che conteneva il percorso da
  scansionare usciva con 0 senza aver controllato alcun file.
- Dopo un cambio di cartella di quarantena, scansione manuale e pagina
  Quarantena restavano sulla vecchia fino al riavvio.
- Un socket di clamd non predefinito era ignorato dalla scansione
  programmata di sistema.

### Aggiunto
- Connessione a clamd via TCP: `--tcp HOST[:PORTA]` in CLI e GUI, scelta
  nelle Impostazioni con avviso sul traffico in chiaro (clamd non supporta
  TLS). Nessun ripiego automatico fra socket e TCP.
- Cartella di quarantena e connessione delle Impostazioni valgono anche per
  `klamav-scan.timer`, tramite un drop-in utente rimosso al ritorno ai
  valori predefiniti. Altri drop-in che lo rendono inefficace (per esempio
  un `override.conf`) vengono segnalati.
- Pianificazione interna e timer di sistema resi alternativi: attivando la
  prima viene proposto di disattivare il secondo.
- Pagine man `klamav-py(1)` e `klamav-py-gui(1)`, in italiano e inglese.
- Test su TCP, quarantena del timer, drop-in in conflitto e man page.
  Totale: 582 test.
  
### Modificato
- Rimossa la scansione per percorso (CONTSCAN): solo INSTREAM, che funziona
  uguale via socket e via TCP e non richiede che clamd legga i file.
- Validazione della cartella di quarantena condivisa fra GUI e CLI; nella
  CLI un percorso relativo è risolto sulla directory corrente.

Aggiornamento da AUR: la prima installazione richiede la chiave di rilascio
(`gpg --recv-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9`); paru e yay
propongono di importarla da soli.

## 0.1.9 — 2026-09-24

### Sicurezza
- Aggiornamento firme delegato a `clamav-freshclam.service`: pkexec esegue solo
  `systemctl restart` con argv fisso e binari root-owned. Rimosso
  `freshclam-update.sh`: freshclam non gira più come root.
- Quarantena: directory, indice e lock creati con le primitive private
  (il lock era 0664 con l'umask 0002 di Debian).
- Unit utente della scansione con sandbox (PrivateNetwork, seccomp,
  ProtectSystem=strict). Rimossa la vecchia unit di sistema inutilizzata.

### Corretto
- Scansione programmata della GUI: non partiva mai se la GUI restava aperta
  meno dell'intervallo e slittava dopo ogni sospensione. Ora la scadenza usa
  l'orologio reale, con recupero delle esecuzioni mancate.
- Quarantena: un salvataggio concorrente non cancella più la versione appena
  salvata; file su altri filesystem gestiti per copia; un indice corrotto
  viene messo da parte invece di bloccare la pagina.
- Real-Time: notifica e log dicevano "messo in quarantena" anche quando non
  lo era; i file con errore risultavano "Analizzato".
- Scansione notturna: riepilogo visibile nel journal della unit (stdout non
  bufferizzato) e niente più page cache piena (fadvise).

### Aggiunto
- Versione e data del database firme visibili; aggiornamento all'avvio solo
  con firme più vecchie di 36 ore.
- Controllo di clamd ogni minuto: avviso persistente, Real-Time sospeso e
  ripreso.
- Firme euristiche e archivi di posta solo segnalati (CLI: `--report-only`,
  `--quarantine-all`).
- Selezione multipla da Dolphin (`%F`): una sola scansione.
- Notifica desktop se la scansione programmata trova infezioni.

### Modificato
- Tetti sulla coda del Real-Time, sulla lista dei risultati e sul log della
  scansione programmata.
- CI su Python 3.10–3.14.

Aggiornamento: reinstallare l'integrazione Dolphin da Impostazioni.

## 0.1.8 — 2026-09-22

### Sicurezza
- Cronologia delle scansioni e log delle scansioni programmate erano
  creati con i permessi di default dello umask (directory 0755, file
  0644): su un sistema con home leggibile da altri, un altro utente
  locale poteva leggere percorsi dei file scansionati, nomi delle firme
  e file infetti. Ora la directory dei dati è 0700 e i file 0600, anche
  per i file creati dalle versioni precedenti.
- Il log di `--log-errors` della CLI era aperto senza protezioni. In una
  directory condivisa come /tmp un altro utente poteva creare il file
  per primo e leggere tutto ciò che vi veniva scritto. Ora il file è
  0600; symlink, FIFO e file già esistenti di un altro utente vengono
  rifiutati con un errore esplicito.
- Il file di configurazione (`~/.config/KlamAV-Py/KlamAV-Py.conf`), che
  elenca le cartelle monitorate dal Real-Time e i percorsi di scansione
  e quarantena, nasceva 0644 perché scritto da Qt. Viene portato a 0600
  all'avvio, insieme al file di configurazione legacy se ancora presente.
- Le risposte di clamd venivano accumulate in memoria senza limite: un
  clamd remoto via TCP, o un socket in un percorso configurabile,
  poteva far crescere il buffer fino a esaurire la RAM. Ora la lettura
  si ferma a 1 MiB.

### Modificato
- Il controllo aggiornamenti automatico all'avvio parte al massimo una
  volta ogni sei ore; il pulsante in Impostazioni resta immediato.
- Le scansioni dalla GUI escludono la directory di quarantena prima di
  leggere i file, come già faceva la CLI, invece di scartarne i
  risultati dopo averli inviati a clamd.

### Aggiunto
- Controlli automatici prima della pubblicazione su AUR: `.SRCINFO`
  allineato al `PKGBUILD` e checksum reale una volta creato il tag.
  Entrambi gli errori erano capitati con la 0.1.7.
- `conftest.py` nella radice: i test importano sempre il sorgente e non
  il pacchetto installato nel sistema. Totale: 125 test.

## 0.1.7 — 2026-09-21

### Sicurezza
- Quarantena aggirabile con hardlink + symlink. La verifica dopo lo
  spostamento usava `os.stat()`, che segue i symlink: un file infetto
  poteva farsi sostituire da un symlink verso un proprio hardlink, e in
  quarantena finiva solo il link mentre il contenuto restava fuori ed
  eseguibile, con la UI che lo mostrava come neutralizzato. Ora la
  verifica usa `os.lstat()` e i permessi sono applicati con `fchmod()`
  sul descrittore già verificato.
- Il socket IPC del single-instance era `/tmp/klamav_py_ipc`, in una
  directory condivisa da tutti gli utenti. Un altro utente locale poteva
  occupare quel nome per primo, ricevere i percorsi dei file mandati in
  scansione e impedire del tutto l'avvio della GUI. Il socket ora sta
  nella runtime directory dell'utente (`$XDG_RUNTIME_DIR`), il client
  verifica con `SO_PEERCRED` che il processo in ascolto sia dello stesso
  utente prima di inviare dati, e un fallimento di `listen()` non è più
  silenzioso: la GUI parte comunque e lo segnala.
- Controllo aggiornamenti: i dati ricevuti da GitHub vengono escapati
  prima di essere mostrati, il link è cliccabile solo se punta al
  repository ufficiale e la risposta è limitata a 256 KB.

### Corretto
- Nessun abort alla chiusura con un thread ancora attivo (ping verso un
  clamd che non risponde, controllo aggiornamenti, scansione in pausa):
  all'uscita i worker vengono fermati e attesi per al massimo 3 secondi.
- Se l'aggiornamento del database viene interrotto (logout, segnale), il
  servizio `clamav-freshclam`/`freshclam` viene comunque riavviato. Prima
  restava fermo.
- L'aggiornamento dalla GUI non riavvia più un servizio `freshclam` che
  l'utente aveva fermato di proposito.
- Una scansione avviata da Dolphin non può più sovrapporsi a una
  scansione manuale già in corso.
- Il colore dei risultati puliti segue di nuovo il tema
  (`QColor("palette(mid)")` non è un colore valido).
- Gli intervalli di pianificazione oltre ~24,8 giorni non vengono più
  troncati dal limite di `QTimer`.

### Aggiunto
- Controllo aggiornamenti dell'applicazione tramite GitHub Releases:
  pulsante in Impostazioni, controllo facoltativo all'avvio, notifica in
  tray quando è disponibile una nuova versione.
- Test sul socket IPC e sulle proprietà che il fix deve mantenere.
  Totale: 87 test.

### Modificato
- "Esci" durante l'aggiornamento del database non lo interrompe più:
  l'applicazione si chiude appena l'aggiornamento termina.
- Dopo l'aggiornamento del pacchetto, un'istanza 0.1.6 ancora aperta non
  viene riconosciuta dalla 0.1.7 (il socket IPC ha cambiato posizione):
  chiuderla dalla tray prima di riavviare l'applicazione.

## 0.1.6 — 2026-08-31

### Corretto
- Crash `QThread: Destroyed while thread is still running` (SIGABRT via
  qFatal) durante l'aggiornamento del database, osservato su Arch con
  Python 3.14 e PySide6 6.11.2. I worker emettono il segnale di fine
  dentro `run()`, quindi la slot azzerava l'ultimo riferimento Python
  mentre il thread C++ era ancora in teardown. Il rilascio passa ora da
  `_retire_qthread`, che trattiene un riferimento forte fino a
  `finished`.
- `debian/klamav-py.install` installava una directory chiamata
  `klamav-py.svg` contenente `klamav-icon.svg`: il secondo campo di quel
  file è sempre una directory di destinazione, non un nome di
  destinazione.

### Modificato
- Icona rinominata da `klamav-icon` a `klamav-py`, per coerenza con il
  nome dell'applicazione: risorsa bundlata, nome cercato nel tema e voce
  `Icon=` del `.desktop`.
- `CHANGELOG.md` riorganizzato in forma sintetica, con la storia estesa
  fino alla 0.1.3 spostata in `docs/CHANGELOG-archive.md` e le bozze
  delle nuove voci generate da `tools/changelog-stub.sh`.

### Aggiunto
- Controlli di coerenza fra le fonti che dichiarano la versione, e test
  di regressione sul rilascio dei QThread. Totale: 75 test.
