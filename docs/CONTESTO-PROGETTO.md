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

Aggiornato alla versione 0.1.12.

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
| `acknowledged.py` | Registro delle segnalazioni già valutate (presa visione), per contenuto e firma |
| `scan_totals.py` | Riepilogo numerico di una scansione, condiviso da worker, cronologia e CLI |
| `scan_exclusions.py` | Validazione delle directory escluse dalle scansioni programmate |
| `systemd_dropin.py` | Drop-in utente per `klamav-scan.service` |
| `freshclam_service.py` | Aggiornamento firme tramite riavvio dell'unità freshclam |
| `db_update_policy.py` | Quando l'aggiornamento locale ha senso (guardia TCP) |
| `db_freshness.py`, `clamd_health.py`, `schedule.py` | Freschezza del DB, stato di clamd, scadenze della pianificazione interna |
| `private_files.py` | Creazione di file e directory privati (0600/0700) |
| `gui/` | Finestra principale, worker QThread, IPC single-instance |
| `gui/off_thread.py` | Controlli brevi (filesystem, `systemctl --user`) fuori dal thread della GUI |

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
da una manciata di test sulla logica pura a oltre 750 test, compresi test
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

**0.1.12 (28 settembre) — nessuna scansione pulita a zero file, niente
blocchi della GUI.** Chiude tutte le voci aperte fino alla 0.1.11. La
scansione della GUI rivalida quarantena ed esclusioni in `ScanWorker.run()`
prima della traversata (una quarantena antenata della cartella interna la
svuotava, preesistente dalla 0.1.10), e le scansioni programmate non
completate, clamd irraggiungibile compreso, non risultano più pulite
(`ScanWorker.aborted`). Controlli sul filesystem e chiamate a
`systemctl --user` della pagina Pianificazione e delle Impostazioni escono
dal thread della GUI (`gui/off_thread.py`). Quarantena con intento e
recupero dopo un crash, `DatabaseDirectory` letta da `freshclam.conf`,
versioni pre-release ordinate, test della gara QThread deterministico.

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
- **Lo stesso utente non attraversa un confine di privilegio.** I file di
  stato stanno nella home dell'utente o nella sua quarantena 0700: indice
  e intenti della quarantena, registro delle prese visione, cronologia,
  configurazione. Una loro manomissione da parte dello stesso utente (o
  di un processo che gira come lui) non è un'escalation: i reperti di
  questo tipo si classificano come robustezza, non come sicurezza. Restano
  da correggere quando rompono qualcosa (un'eccezione che impedisce
  l'avvio, un file dell'utente toccato fuori dalla quarantena), con la
  regola di sempre: validare, mettere da parte, mai cancellare.
- **Dire la verità scomoda dove si configura.** Le label di avviso (Real-Time
  parziale, TCP in chiaro, esclusioni attive) sono una scelta di progetto,
  non rumore.

### La classe di guasto più grave

Una **scansione che risulta pulita senza aver controllato nulla** è il
difetto più serio che questo progetto possa avere, più di molti problemi di
sicurezza classici: l'utente crede di essere protetto e non lo è. Esempi già
trovati e corretti: quarantena in `/var/tmp` distrutta da PrivateTmp,
quarantena che contiene la radice della scansione (CLI, 0.1.10), esclusione
che diventa antenata della radice (pianificazione interna, `6a7a326`),
radice non leggibile, che `os.walk` percorreva senza errori e senza file
(0.1.13).

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
  prosegue con i valori letti al clic (`_ScheduleForm`: casella,
  intervallo, unità, cartella), senza rileggere i widget. Thread daemon e non QThread: un
  controllo bloccato su un mount di rete non deve impedire l'uscita.
  Stesso meccanismo per `systemctl --user` (timeout 10 s): stato del timer
  nella label e nel salvataggio, `disable_timer()`, `daemon_reload()` dopo
  la scrittura del drop-in (Pianificazione e Impostazioni, con
  `dropin_followup`/`dropin_notes`) e avviso di doppia pianificazione. Il
  drop-in e le QSettings si scrivono nel thread della GUI (file locali);
  reload fallito e override estranei restano avvisi mostrati dopo.
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
  minuto; notifica, voce in cronologia e file di log una volta sola finché
  una scansione non arriva alla fine (un log per tentativo faceva uscire
  dalla rotazione quelli delle scansioni vere).
- **Radice non leggibile = bloccante.** Per ogni radice che è una
  directory, con o senza `strict_roots`, una sonda esplicita
  (`clamd_client.unreadable_root_problem`, `os.scandir` aperto e chiuso,
  non `os.access`) prima della traversata e della connessione a clamd:
  GUI → `aborted`, CLI → uscita 2. Un errore di `os.walk` sulla radice
  (corsa con la sonda) è `UnreadableRoot`, stesso esito. Una directory
  vuota ma leggibile resta una scansione pulita.
- **Sottocartelle non leggibili: contatore a parte, non errori.**
  `_iter_files` usa `onerror` e riporta la sola cartella più alta; ENOENT
  ed ENOTDIR (cartella sparita o sostituita durante la traversata) non si
  contano. Non entrano negli «errori» perché le EACCES permanenti (bind
  mount di container, cartelle create con sudo) produrrebbero sempre gli
  stessi errori e nasconderebbero quelli nuovi; la via d'uscita è
  l'esclusione, che funziona perché lo sfoltimento avviene prima dello
  scandir. **Limite noto, voluto:** la CLI esce comunque con 0, quindi
  sotto il timer `OnFailure` non scatta e le cartelle non leggibili si
  vedono solo nel log e nel journal, non in una notifica. È la regola dei
  codici di uscita, non un difetto.
- **Riepilogo come oggetto unico.** `progress`/`finished_scan` di
  `ScanWorker` e `HistoryManager.add_entry` passano un `ScanTotals`
  invece di interi posizionali; un contatore nuovo è un campo con default
  0, e le voci di cronologia vecchie si leggono con `from_entry`.

### Segnalazioni non spostate e presa visione

- **Il codice di uscita non cambia.** Una segnalazione «solo
  segnalazione» (firma euristica, archivio di posta) nuova esce con 1 e
  notifica: è proprio la segnalazione utile. Il problema era la
  *ripetizione* di una già valutata, e si risolve con la presa visione
  per contenuto (`acknowledged.py`), non toccando l'uscita. Scartate:
  uscita 0 per le sole segnalazioni e uscita 3 con `SuccessExitStatus=3`
  (i phishing nuovi diventerebbero invisibili nel percorso del timer),
  escludere l'archivio di posta (contro il design), i file `.fp` di
  ClamAV (semantica giusta, ma di sistema, scritti da root e validi per
  tutti) e `.ign2` (spegne la firma ovunque). Differenziare il testo della
  notifica con `MONITOR_EXIT_STATUS` (systemd ≥ 251) resta un possibile
  raffinamento, non la correzione.
- **Chiave: SHA-256 del contenuto più firma**, non il percorso (in un
  maildir un messaggio letto cambia nome). Contenuto cambiato = di nuovo
  segnalato. Voci senza riscontri da 90 giorni tolte.
- **Solo dopo la policy.** Il registro si consulta solo per i rilevamenti
  che la policy ha già deciso di segnalare soltanto: una presa visione non
  sopprime mai un rilevamento da quarantena, nemmeno con lo stesso hash
  altrove. Hash solo sui file segnalati, riletti dopo il rilevamento con
  `O_NOFOLLOW`; un errore di lettura o un registro illeggibile valgono
  come segnalazione nuova.
- **`--acknowledge` riscansiona** il file via clamd e registra solo un
  infetto «solo segnalazione» con contenuto stabile durante la verifica:
  non si fida di un percorso fornito a mano. La GUI registra invece il
  contenuto riletto subito dopo il rilevamento, e l'eliminazione passa
  solo se il percorso è ancora lo stesso inode.

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
- **Database locale**: la `DatabaseDirectory` del primo `freshclam.conf`
  esistente fra `/etc/clamav`, `/etc` e `/usr/local/etc`; senza, il
  predefinito `/var/lib/clamav`.

### Packaging

- **AUR da sorgente git con tag firmato** (`?signed` + `validpgpkeys` sulla
  chiave primaria). `sha256sums=('SKIP')` è una scelta, non un limite: l'hash
  del tag sarebbe circolare. `tests/test_changelog.py` lo impone.
- **Versione allineata** in `__init__.py`, PKGBUILD/.SRCINFO,
  `debian/changelog` e `CHANGELOG.md`, verificata dai test. Una correzione al
  codice richiede un nuovo tag, quindi una nuova versione, non un pkgrel.
- `debian/klamav-py/` è un artefatto di build in `.gitignore`.
- **Il `.deb` si costruisce su Debian sid** (`dpkg-buildpackage -us -uc -b`
  dal tag firmato), come dalla 0.1.10. `python3-setuptools (>= 77)` in
  Build-Depends lo impone. Una build su Ubuntu 24.04 (debhelper 13.14,
  setuptools da PyPI) funziona ma non è equivalente: manca `changelog.gz`
  (il debhelper vecchio non installa `CHANGELOG.md`), compare
  `init-system-helpers (>= 1.52)` fra le dipendenze, `py3compile` nel
  postinst perde `|| true` e nei metadati resta il file `WHEEL`. Va usata
  solo per prova, non come allegato della release.
- **`.SRCINFO` si rigenera con `tools/srcinfo.sh`** (container Arch, serve
  docker). Aggiornarlo a mano va bene solo per `pkgver` e `source`, e va
  comunque rigenerato prima di pubblicare su AUR.

### Rilascio

Ordine, per ogni versione:

1. Commit di rilascio: versione in `__init__.py`, PKGBUILD/.SRCINFO,
   `debian/changelog`, `CHANGELOG.md` (voce «Non rilasciato» → versione e
   data), data e versione nelle pagine man, tappa in sezione 2 di questo
   documento. `tests/test_changelog.py` e `tests/test_manpage.py`
   verificano l'allineamento.
2. Merge su `main` con la CI verde.
3. Tag `v<versione>` firmato con la chiave di rilascio
   (`EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9`) e verificato con
   `git tag -v`. Lo crea solo il maintainer: un tag non firmato, o firmato
   con un'altra chiave, fa fallire `makepkg` per tutti gli utenti AUR, e
   rifarlo con lo stesso nome dopo la pubblicazione lascia copie
   sbagliate in giro.
4. `.deb` costruito su Debian sid dal tag.
5. Release GitHub `KlamAV-Py <versione>`: descrizione = voce di
   `CHANGELOG.md` (Modificato, Aggiunto, Corretto) più eventuali note per
   chi aggiorna; `.deb` allegato. Il controllo aggiornamenti della GUI la
   segnala da lì.
6. `.SRCINFO` rigenerato e pacchetto AUR aggiornato.

Un assistente automatico (sessione LLM in un ambiente cloud) può preparare i
punti 1, 2 e le note del punto 5, ma non 3 e 4: non ha la chiave di rilascio,
e la rete dell'ambiente può non raggiungere i mirror Debian.

### Quarantena

- **Intento prima dello spostamento.** `quarantine_file` scrive
  `.<nome>.intent` (con flock tenuto per tutta l'operazione) prima di
  toccare il file, e `.<nome>.copy` prima di una copia fra filesystem.
  `recover_interrupted()`, eseguita alla creazione di `Quarantine`, salta
  gli intenti con il lock tenuto (operazione in corso in un altro processo)
  e per gli altri: rename riuscito → voce nell'indice e 0400; copia
  completa (0400, scritto solo dopo fsync) con l'originale già tolto →
  voce, ed eventuale nome temporaneo `.klamav-quarantena-*` cancellato;
  copia incompleta o originale ancora al suo posto → copia cancellata,
  originale lasciato dov'è (la prossima scansione lo rileva). Non si
  ripete mai lo spostamento dell'originale nel recupero: vorrebbe dire
  ripeterne le verifiche.
- **L'intento ha la fiducia dell'indice** (0.1.13). Si valida con le
  stesse regole (`_entry_problem`), `original_path` assoluto, `dev`/`ino`
  interi non negativi, e `staging` deve stare nella directory
  dell'originale con il nome `.klamav-quarantena-<32 hex>`: è l'unico
  file fuori dalla quarantena che il recupero può cancellare. Un intento
  non valido, o qualunque errore imprevisto nel recupero, si mette da
  parte come `.<nome>.intent.corrupt-*` (mai cancellato) e lascia `dest`
  visibile in `orphans()`: il costruttore di `Quarantine` non solleva per
  un file di servizio. Se `staging` esiste ma non è il file verificato
  (salvataggio con temporaneo+rename durante la copia), la voce si
  aggiunge comunque e `staging` resta per il recupero manuale.
- **Contare non è recuperare.** Chi deve solo guardare l'indice (le
  Impostazioni che contano le voci della quarantena che si lascia) usa
  `peek_entries()`: niente lock, niente recupero, niente creazione della
  directory.

### Aggiornamento dell'applicazione

- **Versioni con pre-release** (`update_check_worker.version_key`): stadi
  dev < a/alpha < b/beta < rc/pre/c < finale, numero di stadio numerico,
  zeri finali ignorati, metadati dopo `+` ignorati; un suffisso
  sconosciuto è una pre-release del livello più basso.

### Qt

- **Worker QThread ritirati con `_retire_qthread()`** e un set di riferimenti
  forti a livello di modulo: `deleteLater` da solo non basta a evitare la
  distruzione del wrapper Python con il thread ancora vivo (SIGABRT osservato
  su Arch). `PingWorker` fa eccezione perché ha un parent Qt.
- **Il test della gara (`test_qthread_retire.py`) è deterministico**: lo
  script di riproduzione tiene vivo `run()` 50 ms dopo l'emit, così la
  controprova senza correzione aborta sempre. Un'uscita pulita della
  controprova è un fallimento, non uno skip.

---

## 5. Problemi noti e aperti

Già tracciati: segnalarli di nuovo è utile solo se si aggiunge uno scenario,
una riproduzione o una correzione migliore.

Emersi dalle revisioni della 0.1.12 e non corretti nella 0.1.13:

- **Recupero della quarantena nel thread della GUI.** Il costruttore di
  `Quarantine` esegue il recupero, e si chiama nel thread della GUI
  all'avvio e in `_apply_quarantine_dir`. La 0.1.13 ha tolto solo il caso
  del conteggio (`peek_entries`); spostare fuori thread gli altri due è
  il lavoro previsto per la 0.1.14.
- **Finestra fra `open_private_fd(O_EXCL)` e `flock` in `_write_intent`:**
  un recupero concorrente può eliminare l'intento di un'operazione viva
  (esito: orfano visibile, come prima della 0.1.12). Correzione possibile:
  intento su nome temporaneo, poi rename.
- **`_clear_intent` nel `finally` di `quarantine_file`:** se la scrittura
  dell'indice fallisce dopo lo spostamento, l'intento sparisce e il
  recupero automatico non avviene.
- **Scansione manuale bloccata:** la riga di errore compare, ma stato e
  referto dicono «Completata senza problemi» (`ScanPage` non collega
  `aborted`). Vale anche per la radice non leggibile della 0.1.13.
- **`ScanWorker.run()`:** costruzione del client e di `Quarantine` fuori
  dal `try`; un `OSError` lascia la pagina «in corso» fino al riavvio.
- **Pagina Pianificazione:** con un controllo bloccato su un mount di
  rete, le aggiunte di esclusioni vengono scartate in silenzio e il
  pulsante di salvataggio resta disattivato senza spiegazione.
- Il messaggio «verrà ritentata finché il problema non è risolto» non ha
  seguito dopo il primo tentativo.
- `Quarantine.recovered` non è letto da nessun consumatore: valutare di
  mostrare gli esiti del recupero (tray una volta, stderr nella CLI).
- **Prese visione nella scansione programmata interna:** i problemi del
  registro (file non rileggibile per l'hash, registro messo da parte)
  arrivano sul segnale `error`, che la programmata non collega: si
  vedono nella scansione manuale e nella CLI, non nel log della
  programmata. L'esito resta corretto (segnalazione nuova).
- **Hash riletto dopo il rilevamento:** fra il verdetto di clamd e la
  rilettura il contenuto può cambiare, e la GUI registrerebbe la presa
  visione del contenuto nuovo. Richiede che lo stesso utente modifichi il
  file in quella finestra; la CLI (`--acknowledge`) confronta l'hash prima
  e dopo la scansione.
- Residuo nei test: `tests/test_settings_exclusions.py` imposta
  `_schedule_missing_noted`, attributo non più usato in produzione.

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
  fallire. Riportarli come ambientali, non come difetti. Il test della gara
  QThread non rientra più fra gli skip: dalla 0.1.12 si riproduce in modo
  deterministico, e se la controprova non aborta è un fallimento.
- **Test con thread veri**: alcuni test della pagina Pianificazione usano
  `run_off_gui_thread` reale, con una funzione bloccata di proposito; gli
  altri la sostituiscono con una versione in linea nelle fixture `env`.
  Un test nuovo che salva la Pianificazione o le Impostazioni deve usare
  una delle due strade, altrimenti controlla lo stato prima che l'esito
  arrivi.
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
   Un reperto di gravità alta o bloccante va accompagnato da una
   riproduzione, e l'output riportato va confrontato con la tesi prima di
   concludere: più di un reperto è stato scartato perché l'output della
   sua stessa riproduzione diceva altro.
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
