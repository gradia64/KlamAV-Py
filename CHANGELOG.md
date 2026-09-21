# Changelog

Cronologia delle versioni per gli utenti, indipendente dalla
distribuzione. Il dettaglio esteso fino alla 0.1.3 e le tornate di audit
sono in `docs/CHANGELOG-archive.md`.

---

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
