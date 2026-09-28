# KlamAV-Py: contesto del progetto

Questo documento è il punto di partenza per chi arriva al progetto da
esterno: cosa è KlamAV-Py, cosa è stato fatto finora e perché, quali
decisioni sono già state prese e quali problemi sono noti. Serve anche a chi
revisiona il codice, umano o assistito da LLM: in quest'ultimo caso va
allegato al prompt insieme al diff o al commit da esaminare, e la sezione 8
descrive il formato atteso del rapporto.

Non sostituisce la lettura del codice: i docstring dei moduli spiegano il
*perché* delle singole scelte e restano la fonte primaria. Qui c'è ciò che
dal codice non si vede, cioè da dove viene una regola e cosa è già stato
valutato e scartato. La sezione 9 indica dove trovare il resto.

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

## 2. Cosa è stato fatto

La storia per tappe, con il motivo di ogni svolta. Il dettaglio completo è
in `CHANGELOG.md` e, fino alla 0.1.3, in `docs/CHANGELOG-archive.md`.

Il metodo è rimasto lo stesso dall'inizio: ogni release è passata per
revisioni indipendenti del codice (umane e assistite da LLM), e ogni
reperto è stato verificato e, dove possibile, riprodotto con un test prima
di essere corretto. Le gravità proposte dai revisori sono state ricalibrate
sul modello di minaccia reale, in entrambe le direzioni. La suite è passata
da una manciata di test sulla logica pura a circa 700 test, compresi test
della GUI in modalità offscreen e test con socket reali.

**0.1.0–0.1.5 (agosto 2026) — la riscrittura.** CLI e GUI che parlano con
clamd attraverso il suo protocollo nativo, eliminando alla radice
l'invocazione via shell di KlamAV 0.22. Quarantena con indice JSON,
Real-Time, pianificazione, integrazione Dolphin. Le prime tornate di audit
hanno portato: permessi dei file in quarantena (un file eseguibile restava
eseguibile), passaggio dalla unit di sistema con `DynamicUser` a una unit
utente, stato dedicato per i file oltre `StreamMaxLength`, gestione dei
limiti di inotify. I test reali su una home da oltre 330.000 file hanno
portato pausa e ripresa, esclusione della quarantena dalla traversata,
lock sull'indice e log persistenti delle scansioni programmate. Le 0.1.4 e
0.1.5 sono rilasci di rifinitura e packaging (vedi tag e
`debian/changelog`).

**0.1.6 (31 agosto) — stabilità dei thread.** Crash `QThread: Destroyed
while thread is still running`, osservato solo su Arch: nato il pattern
`_retire_qthread`. Controlli di coerenza fra le fonti della versione.

**0.1.7 (21 settembre) — due vulnerabilità locali.** Quarantena aggirabile
con hardlink e symlink: il file infetto poteva restare fuori mentre la UI lo
mostrava neutralizzato (advisory pubblicato su GitHub). Socket IPC del
single-instance in `/tmp`, occupabile da un altro utente: spostato in
`$XDG_RUNTIME_DIR` con verifica `SO_PEERCRED`. Arrivano anche il controllo
aggiornamenti via GitHub Releases e la chiusura ordinata dei worker.

**0.1.8 (22 settembre) — dati privati.** Cronologia, log e configurazione
nascevano leggibili da altri utenti: ora 0600/0700, anche per i file
esistenti. Tetto di 1 MiB alle risposte di clamd, log della CLI protetto da
symlink e file altrui.

**0.1.9 (24 settembre) — chiusura dell'audit.** Aggiornamento delle firme
delegato all'unità systemd di freshclam tramite `pkexec systemctl`: rimosso
lo script eseguito come root. Sandbox completa della unit di scansione,
recupero delle scansioni programmate mancate, controllo periodico di clamd,
firme euristiche e archivi di posta solo segnalati, selezione multipla da
Dolphin. Con questa release non restavano debiti di sicurezza noti dagli
audit: da qui in avanti il lavoro è evoluzione funzionale.

**0.1.10 (26 settembre) — TCP e scansione programmata di sistema.**
Connessione a clamd via TCP con `ClamdEndpoint`, rimozione di CONTSCAN, tag
di rilascio firmati con chiave dedicata e AUR da sorgente git verificato.
Quarantena del timer configurabile tramite drop-in, con rifiuto delle
directory volatili (una quarantena in `/var/tmp` veniva distrutta da
`PrivateTmp`). clamd irraggiungibile ora produce uscita 2 invece di una
scansione «pulita». Pagine man in italiano e inglese.

**0.1.11 (27 settembre) — esclusioni.** Cartelle escluse configurabili
dalla GUI, una lista per timer e pianificazione interna, validate con una
regola condivisa con la CLI; `--exclude` rifiuta i casi che prima
svuotavano la scansione in silenzio. Aggiornamento del database disabilitato
quando clamd, via TCP, usa un database diverso da quello locale.

**Dopo la 0.1.11.** Su `main`, in attesa della 0.1.12: la pianificazione
interna rivalida le esclusioni a ogni avvio, come la CLI (`6a7a326`); la
rivalidazione è poi passata in `ScanWorker.run()`, estesa alla quarantena,
e le scansioni programmate non completate non risultano più pulite. La
pagina Pianificazione non tocca più il filesystem nel thread della GUI, e la
cartella interna è validata come directory al salvataggio.

---

## 3. Modello di minaccia e principi

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

## 4. Decisioni già prese

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
- **Rivalidazione a ogni scansione**: nella CLI (e quindi nel timer) e in
  `ScanWorker.run()` per le scansioni della GUI, prima della traversata e
  fuori dal thread principale. Il worker confronta con ogni radice che è
  una directory sia le esclusioni dell'utente (`scan_exclusions.decide`)
  sia la quarantena (`quarantine_location.root_inside`); un file come radice
  non si controlla, perché `_iter_files` non gli applica le esclusioni.
- **Controlli sul filesystem fuori dal thread della GUI.** La pagina
  Pianificazione valuta esclusioni, cartella interna e quarantena con
  `gui/off_thread.run_off_gui_thread` (thread daemon, risultato consegnato
  con un segnale queued), una valutazione per tipo alla volta; il
  salvataggio disattiva il pulsante finché la validazione non risponde e
  prosegue con i valori letti al clic. Thread daemon e non QThread: un
  controllo bloccato su un mount di rete non deve impedire l'uscita.
- **Cartella della pianificazione interna**: al salvataggio, con la
  pianificazione interna attiva, dev'essere un percorso assoluto di una
  directory esistente. A ogni scansione la verifica il worker
  (`strict_roots`), che la risolve prima della traversata; una cartella
  mancante è una scansione non completata come le altre.
- **Quarantena e radici della GUI**: `quarantine_location.decide` accetta
  `roots`; le Impostazioni passano home e `schedule_target`, la
  Pianificazione controlla la regola inversa al salvataggio.
- **Scansione non completata** (`ScanWorker.aborted`): per la pianificazione
  interna non aggiorna `schedule_last_run`, quindi viene ritentata al
  minuto; notifica e voce in cronologia una volta sola finché una scansione
  non arriva alla fine.

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

## 5. Problemi noti e aperti

Già tracciati: segnalarli di nuovo è utile solo se si aggiunge uno scenario,
una riproduzione o una correzione migliore.

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

## 6. Non-obiettivi

TLS verso clamd; failover o riconnessione verso host alternativi;
incoraggiare `TCPSocket` in `clamd.conf` (il socket Unix con i suoi permessi
è più restrittivo: con TCP attivo ogni utente locale può usare clamd come
oracolo); scansione di home altrui; esecuzione come root.

---

## 7. Verificare in pratica

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

## 8. Per chi revisiona: formato del rapporto

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
   rapporto con le voci aperte della sezione 5: una correzione che va in
   direzione opposta a una voce già pianificata va segnalata come tale.

Separare i reperti per peso: pochi reperti importanti in evidenza, i minori
raggruppati e brevi. Non mettere sullo stesso piano un difetto della classe
«scansione pulita senza aver controllato nulla» e un'incoerenza di messaggi.

---

## 9. Dove trovare cosa

- `README.md`: installazione, uso, integrazione con Dolphin, motivazioni di
  alcune scelte (per esempio unit utente invece che di sistema).
- `CHANGELOG.md`: cosa cambia in ogni versione, per gli utenti;
  `docs/CHANGELOG-archive.md` per le versioni fino alla 0.1.3 e le prime
  tornate di audit.
- `docs/man/`: pagine man di CLI e GUI, in italiano e inglese.
- `SECURITY.md`: come segnalare una vulnerabilità in privato. Le
  vulnerabilità non vanno aperte come issue pubbliche né aggiunte a questo
  documento prima della correzione.
- Docstring dei moduli in `klamav_py/`: il perché delle scelte, modulo per
  modulo.
- `tests/`: ogni correzione importante ha un test che ne descrive lo
  scenario; spesso è il modo più rapido per capire un comportamento.

---

## Manutenzione di questo documento

Aggiornarlo a ogni release: aggiungere la tappa in sezione 2, spostare in
sezione 4 le voci della sezione 5 che vengono risolte con una decisione,
aggiungere le nuove decisioni e i problemi aperti emersi dalle revisioni,
aggiornare la riga con la versione di riferimento in cima.
