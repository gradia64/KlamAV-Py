# Guida per chi revisiona KlamAV-Py

Questo documento raccoglie il contesto che serve per revisionare il codice di
KlamAV-Py senza doverlo ricostruire dalla storia del progetto: modello di
minaccia, decisioni già prese, problemi noti e formato atteso dei rapporti.
Vale per revisori umani e per revisioni assistite da LLM; in quest'ultimo
caso va allegato al prompt insieme al diff o al commit da esaminare.

Non sostituisce la lettura del codice: i docstring dei moduli spiegano il
*perché* delle singole scelte e restano la fonte primaria. Qui c'è ciò che
dal codice non si vede, cioè da dove viene una regola e cosa è già stato
valutato e scartato.

Aggiornato alla versione 0.1.11 più il commit `6a7a326` su `main`.

---

## 1. Il progetto in breve

KlamAV-Py è una riscrittura in Python/PySide6 di KlamAV 0.22, vecchio
frontend KDE3 per ClamAV. Offre scansione manuale, Real-Time, scansione
programmata, quarantena, aggiornamento delle firme e integrazione con
Dolphin. Due entry point: `klamav-py` (CLI, nessuna dipendenza da Qt) e la
GUI.

Piattaforme: Debian (primaria, pacchetto `.deb`) e Arch Linux (AUR, sorgente
git con tag firmato). CI su Ubuntu 26.04, Python 3.10–3.14.

Moduli principali, tutti in `klamav_py/`:

| Modulo | Ruolo |
| --- | --- |
| `clamd_client.py` | Protocollo nativo di clamd (INSTREAM, PING, VERSION) su socket Unix o TCP; `ClamdEndpoint`; traversata `_iter_files` |
| `cli.py` | CLI, usata anche dal timer systemd |
| `quarantine.py` | Spostamento, indice, ripristino e cancellazione dei file in quarantena |
| `quarantine_location.py` | Validazione della directory di quarantena scelta dall'utente |
| `quarantine_policy.py` | Quando un infetto va solo segnalato e non spostato |
| `scan_exclusions.py` | Validazione delle directory escluse dalle scansioni programmate |
| `systemd_dropin.py` | Drop-in utente per `klamav-scan.service` |
| `freshclam_service.py` | Aggiornamento firme tramite riavvio dell'unità freshclam |
| `db_update_policy.py` | Quando l'aggiornamento locale ha senso (guardia TCP) |
| `db_freshness.py`, `clamd_health.py`, `schedule.py` | Freschezza del DB, stato di clamd, scadenze della pianificazione interna |
| `private_files.py` | Creazione di file e directory privati (0600/0700) |
| `gui/` | Finestra principale, worker QThread, IPC single-instance |

I moduli fuori da `gui/` non importano Qt: la CLI deve funzionare senza
PySide6.

---

## 2. Modello di minaccia e principi

Il progetto è un antivirus: tratta per definizione file ostili, e gira con i
privilegi dell'utente di sessione. I principi che seguono guidano la
valutazione della gravità.

- **Niente shell, niente binari esterni per la scansione.** Si parla con
  clamd direttamente. È il motivo stesso della riscrittura: KlamAV 0.22
  costruiva comandi shell con i percorsi dei file.
- **I binari privilegiati sono fidati solo se verificati.** `pkexec`,
  `systemctl` e `journalctl` si usano con percorso assoluto dopo
  `trusted_binary()` (proprietà root, non scrivibili da group/other). argv
  fisso, unità ammesse da whitelist.
- **I file scansionati sono ostili.** La quarantena usa `O_NOFOLLOW`, verifica
  dell'inode dopo il rename, `fchmod` sul descrittore; le protezioni TOCTOU
  contro hardlink e symlink sono state oggetto di un advisory pubblicato e non
  vanno indebolite.
- **I dati dell'utente sono privati.** Cronologia, log e configurazione
  contengono percorsi e nomi di file infetti: 0600/0700 tramite
  `private_files.py`, anche su home 0755.
- **L'antivirus non deve danneggiare i dati dell'utente.** Per questo le
  rilevazioni euristiche e gli archivi di posta sono solo segnalati
  (`quarantine_policy.py`), e il recupero dell'indice corrotto è non
  distruttivo.
- **Fallire in modo visibile.** Una configurazione sbagliata deve produrre un
  errore o una notifica, mai un comportamento degradato in silenzio. Niente
  fallback automatici fra trasporti.
- **Dire la verità scomoda dove si configura.** Le label di avviso (Real-Time
  parziale, TCP in chiaro, esclusioni attive) sono una scelta di progetto,
  non rumore.

### La classe di guasto più grave

Una **scansione che risulta pulita senza aver controllato nulla** è il
difetto più serio che questo progetto possa avere, più di molti problemi di
sicurezza classici: l'utente crede di essere protetto e non lo è. Esempi già
trovati e corretti: quarantena in `/var/tmp` distrutta da PrivateTmp,
quarantena che contiene la radice della scansione (CLI, 0.1.10), esclusione
che diventa antenata della radice (pianificazione interna, `6a7a326`).

Il meccanismo tipico è `_iter_files` (`clamd_client.py`): se la radice della
scansione sta dentro un'esclusione, la traversata restituisce zero file. È un
comportamento voluto per la quarantena, quindi la protezione deve stare a
monte, nella validazione. Qualsiasi percorso che porta a zero file senza
errore merita attenzione prioritaria.

---

## 3. Decisioni già prese

Le voci seguenti sono state valutate e decise. Riproporle va bene, ma con un
argomento nuovo: un rapporto che le segnala come difetti senza aggiungere
nulla fa perdere tempo.

### Protocollo e connessione

- **Solo INSTREAM.** `scan_file()`/CONTSCAN è stata rimossa nella 0.1.10:
  CONTSCAN interpreterebbe il percorso sul filesystem di clamd, sbagliato con
  TCP verso un altro host.
- **`ClamdEndpoint` è l'unico costruttore di `ClamdClient`**, immutabile, con
  `transport` esplicito e mai dedotto. I worker lo ricevono per valore.
- **Nessun fallback unix↔TCP, niente TLS, niente failover.** Chi vuole
  cifratura usa un tunnel. Il TCP è in chiaro e la GUI lo dice.
- **Nessuna migrazione dei setting per il TCP**: l'assenza di
  `clamd_transport` è il valore legacy.

### Scansione programmata

- **Timer systemd e pianificazione interna della GUI sono alternativi**, non
  complementari, per evitare doppie scansioni. L'unificazione è rinviata.
- **Una sola lista di esclusioni per entrambi**, validata contro entrambe le
  radici (home per il timer, `schedule_target` per la pianificazione
  interna), perché passare dall'uno all'altro non cambi la copertura in
  silenzio.
- **Forme salvate diverse, di proposito.** Le esclusioni dell'utente si
  salvano espanse e assolute ma *non risolte*, e si risolvono a ogni
  scansione (un symlink segue la destinazione attuale). La directory di
  quarantena invece si salva *già risolta*. Tenerne conto quando si ragiona
  su scenari con symlink ripuntati.
- **Le esclusioni di file sono un errore**, non un avviso: `_iter_files`
  sfoltisce solo directory.
- **Rivalidazione a ogni scansione**: nella CLI (e quindi nel timer) e, da
  `6a7a326`, nella pianificazione interna per le esclusioni dell'utente.

### Unit systemd e drop-in

- **Unit utente**, non di sistema: una unit di sistema non poteva leggere home
  private senza allargarne i permessi. Sandbox completa nella unit spedita.
- **Un solo drop-in, generato dallo stato completo** (quarantena, endpoint,
  esclusioni), con `ExecStart=` azzerato. Chi salva una parte deve passare
  anche le altre.
- **La unit spedita non si modifica** per le personalizzazioni: per il TCP il
  drop-in aggiunge `PrivateNetwork=no` e *estende*
  `RestrictAddressFamilies`, mantenendo il filtro.
- **Codici di uscita della CLI**: 0 nessuna infezione (anche con errori di
  lettura), 1 infezioni, 2 errore bloccante. `OnFailure` notifica 1 e 2.

### Aggiornamento firme

- **Riavvio dell'unità freshclam via `pkexec`**, mai script root (rimosso
  nella 0.1.9). L'esito si verifica confrontando la versione del DB riportata
  da clamd prima e dopo, non il codice di ritorno di systemctl.
- **Con clamd via TCP** l'aggiornamento locale è consentito solo su loopback e
  solo se il daily locale coincide con quello di clamd (0.1.11). I nomi host
  non si risolvono nel thread della GUI.

### Packaging

- **AUR da sorgente git con tag firmato** (`?signed` + `validpgpkeys` sulla
  chiave primaria). `sha256sums=('SKIP')` è una scelta, non un limite: l'hash
  del tag sarebbe circolare. `tests/test_changelog.py` lo impone.
- **Versione allineata** in `__init__.py`, PKGBUILD/.SRCINFO,
  `debian/changelog` e `CHANGELOG.md`, verificata dai test. Una correzione al
  codice richiede un nuovo tag, quindi una nuova versione, non un pkgrel.
- `debian/klamav-py/` è un artefatto di build in `.gitignore`.

### Qt

- **Worker QThread ritirati con `_retire_qthread()`** e un set di riferimenti
  forti a livello di modulo: `deleteLater` da solo non basta a evitare la
  distruzione del wrapper Python con il thread ancora vivo (SIGABRT osservato
  su Arch). `PingWorker` fa eccezione perché ha un parent Qt.

---

## 4. Problemi noti e aperti

Già tracciati: segnalarli di nuovo è utile solo se si aggiunge uno scenario,
una riproduzione o una correzione migliore.

- **Contenenza quarantena/radice nella GUI.** `quarantine_location.decide`
  confronta la quarantena solo con la home; né le Impostazioni né la
  Pianificazione confrontano la quarantena con `schedule_target`. Con una
  quarantena antenata del target interno la scansione programmata della GUI
  percorre zero file. La CLI è protetta. Preesistente dalla 0.1.10.
  Direzione prevista per la 0.1.12: parametro `roots` in
  `quarantine_location.decide` e rivalidazione di tutte le esclusioni della
  traversata dentro `ScanWorker.run()`.
- **Syscall sul filesystem nel thread GUI.** La rivalidazione di `6a7a326`
  esegue `resolve()`/`stat()` nel thread principale; un'esclusione su un mount
  di rete `hard` non raggiungibile bloccherebbe l'interfaccia.
  `_refresh_exclusions` rivaluta a ogni tasto nel campo target. Stessa
  soluzione del punto precedente.
- **Target della pianificazione interna non validato come directory** al
  salvataggio: un file scritto a mano produce blocchi e avvisi incoerenti con
  la CLI.
- **Scansione programmata bloccata senza traccia in cronologia** (vale sia per
  esclusioni non valide sia per target mancante).
- **`LOCAL_DB_DIR` fisso su `/var/lib/clamav`** in `db_update_policy.py`: un
  `DatabaseDirectory` personalizzato con TCP su loopback disabilita
  l'aggiornamento con un messaggio fuorviante. Esito comunque sicuro.
- **`systemctl --user` sincrono nel thread GUI** (`timer_enabled()`,
  `refresh_system_timer()`), timeout 10 s.
- **`_copy_across_filesystems`**: un crash fra copia e rename lascia un orfano
  0400 in quarantena e il file infetto al suo posto. Esito sicuro, tenuto
  come nota di comportamento.
- **`_version_compare`** tronca i suffissi pre-release (`-rc1`).
- **Test di regressione della gara QThread** (`test_qthread_retire.py`) saltato
  dove la gara non si riproduce: da riverificare con PySide6 più recenti.

---

## 5. Non-obiettivi

TLS verso clamd; failover o riconnessione verso host alternativi;
incoraggiare `TCPSocket` in `clamd.conf` (il socket Unix con i suoi permessi
è più restrittivo: con TCP attivo ogni utente locale può usare clamd come
oracolo); scansione di home altrui; esecuzione come root.

---

## 6. Verificare in pratica

- Test: `QT_QPA_PLATFORM=offscreen python -m pytest`. `conftest.py` fa
  importare sempre il sorgente, non il pacchetto installato. Su una macchina
  di sviluppo con copie diverse installate (`.deb`, venv, albero) conviene
  comunque `pip install -e .` nel venv.
- **Skip e fallimenti ambientali attesi**: da root diversi test sui permessi
  si saltano; senza `::1` o senza locale italiano alcuni test si saltano; in
  un sandbox in sola lettura i test che invocano `systemd-analyze` possono
  fallire. Riportarli come ambientali, non come difetti.
- **`INVOCATION_ID` cambia il comportamento della CLI**: se presente (come
  dentro una unit systemd, o in certe sessioni), una quarantena su percorso
  volatile è un errore invece di un avviso. Nelle riproduzioni isolare le
  regole con un ambiente pulito, altrimenti un rifiuto può venire dalla regola
  sbagliata.
- Riproduzioni in una directory temporanea, senza toccare l'albero del
  repository. Per i test con symlink e permessi usare un utente non root.
- Indicare sempre il commit esaminato: i numeri di riga cambiano spesso.

---

## 7. Formato del rapporto

Un verdetto iniziale in una o due frasi, poi i reperti. Se non c'è nulla da
segnalare, scriverlo: un rapporto breve e vuoto vale più di uno riempito.

Per ogni reperto:

1. **Titolo** che descrive l'effetto, non il meccanismo.
2. **Origine**: introdotto dal commit in esame, oppure preesistente (da quale
   versione, se determinabile), oppure non determinabile. Un difetto
   preesistente reso visibile da un commit non è un difetto del commit.
3. **Gravità** (bloccante, alta, media, bassa, cosmetico) **e probabilità**
   dello scenario, motivate separatamente. Una conseguenza grave in uno
   scenario improbabile va detta così.
4. **Stato della prova**: verificato con riproduzione (riportare comando e
   output essenziale), verificato su lettura del codice, oppure ipotesi.
5. **Riferimenti** `file:riga` al commit indicato.
6. **Correzione proposta**, con le alternative se ce ne sono, e il suo
   rapporto con le voci aperte della sezione 4: una correzione che va in
   direzione opposta a una voce già pianificata va segnalata come tale.

Separare i reperti per peso: pochi reperti importanti in evidenza, i minori
raggruppati e brevi. Non mettere sullo stesso piano un difetto della classe
«scansione pulita senza aver controllato nulla» e un'incoerenza di messaggi.

---

## Manutenzione di questo documento

Aggiornarlo a ogni release: spostare in sezione 3 le voci della sezione 4
che vengono risolte con una decisione, aggiungere le nuove decisioni e i
problemi aperti emersi dalle revisioni, aggiornare la riga con la versione
di riferimento in cima.
