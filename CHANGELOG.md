# Changelog

Cronologia delle versioni di KlamAV-Py, indipendente dalla
distribuzione: è il changelog che leggono gli utenti Debian, Arch e
chiunque installi da sorgente.

- Metadati del pacchetto Debian: `debian/changelog` (gestito con `dch`).
- Dettaglio esteso delle versioni fino alla 0.1.3 e tornate di audit:
  `docs/CHANGELOG-archive.md`.

## Come si aggiorna

Una sezione per versione, in ordine anti-cronologico, con
intestazione `## <versione> — <AAAA-MM-GG>`. Le voci vanno raggruppate
sotto `### Sicurezza`, `### Corretto`, `### Aggiunto`, `### Modificato`
(solo quelle che servono), **una riga per voce**. La motivazione lunga
va nelle note di rilascio su GitHub o nel messaggio di commit, non qui:
questo file deve restare scorrevole a colpo d'occhio.

Non si scrive a mano da zero. Dopo `dch`:

```sh
tools/changelog-stub.sh      # genera la bozza dalla voce in cima a debian/changelog
```

poi si accorcia la bozza e si taglia il superfluo. `pytest` fallisce se
la versione in cima a questo file non coincide con
`klamav_py.__version__`, quindi non può restare indietro senza che te
ne accorga.

---

## 0.1.5 — 2026-08-30

### Sicurezza
- Apertura dei file con `O_NOFOLLOW|O_NONBLOCK` e verifica `fstat()` sul
  descrittore già aperto: chiusa la finestra TOCTOU tra il controllo del
  tipo e la lettura.

### Corretto
- FIFO, socket, device e symlink non vengono più inviati a clamd. Una
  sola FIFO senza scrittore bloccava l'intera scansione nel kernel,
  senza errore né timeout.
- I symlink pendenti non producono più falsi errori `[Errno 2]`: il
  filtro usa `lstat()`, che non segue il link.
- Servicemenu Dolphin di nuovo a `0755` (regressione della 0.1.4-1): da
  KFrameworks 5.85 i servicemenu KIO richiedono il bit di esecuzione. Il
  launcher in `/usr/share/applications` resta `0644` — le due
  destinazioni hanno requisiti opposti.
- Timeout di attesa del verdetto separato da quello di
  connessione/invio (120s contro 30s), con secondo tentativo su
  sessione pulita. I file non verificati nemmeno al secondo tentativo
  hanno ora un messaggio distinto.
- Real-Time: il monitoraggio è ricorsivo. `QFileSystemWatcher` richiede
  la registrazione esplicita di ogni directory.
- Real-Time: i file già presenti in cartelle scoperte dopo l'avvio
  vengono scansionati invece che registrati come stato di partenza
  (parametro `scan_existing`).
- Real-Time: label di stato leggibile, non più `palette(mid)` — grigio
  su grigio su molti temi Plasma.

### Aggiunto
- `--version` alla CLI: la versione era visibile solo nella GUI.
- Test per symlink, FIFO e socket con timeout espliciti, `pytest-timeout`
  fra le dipendenze di sviluppo, test di coerenza fra `__version__` e
  `debian/changelog`. Totale: 67 test.

### Modificato
- `pyproject.toml` deriva la versione da `klamav_py.__version__` invece
  di ridichiararla: era una terza fonte di verità ed era rimasta
  indietro alla 0.1.4.

## 0.1.4-2 — 2026-08-29

### Corretto
- La radice di ogni scansione viene risolta con `Path.resolve()` dentro
  `scan_stream()`, quindi per tutti i punti di ingresso (CLI, GUI,
  Dolphin/IPC): il referto mostra il percorso reale e non un eventuale
  symlink. Non è un fix di sicurezza.

## 0.1.4 — 2026-08-29

### Sicurezza
- TOCTOU in quarantena e ripristino: rifiuto dei symlink, verifica di
  integrità dopo lo spostamento, reclamo atomico della destinazione.
- Socket IPC single-instance ristretto al solo utente proprietario
  (`UserAccessOption`), payload validato per dimensione e decodificato
  in UTF-8 in modo robusto.
- Eliminata la costruzione a runtime della stringa di shell per
  `pkexec`/`freshclam`, sostituita da uno script fisso spedito dal
  pacchetto.
- `os.system()` sostituito da `subprocess.run()` senza shell per la
  rigenerazione della cache servizi KDE.

### Aggiunto
- Versione visibile nel titolo della finestra, nel tooltip della tray e
  in Impostazioni (fonte unica: `klamav_py.__version__`).
- Tooltip della tray aggiornato anche durante le scansioni manuali, con
  contatori in corso e stato "In pausa".
- 9 test (quarantena TOCTOU/symlink/FIFO, payload IPC).

### Modificato
- Configurazione migrata da `~/.config/KlamAV/KlamAV.conf` a
  `~/.config/KlamAV-Py/KlamAV-Py.conf`; al primo avvio le chiavi
  esistenti vengono copiate una volta sola, il vecchio file resta su
  disco.
- Permessi dei `.desktop` dell'integrazione Dolphin portati a `0644`.
  **Scelta poi rivista nella 0.1.5**: per i servicemenu era sbagliata.

## 0.1.3 — 2026-08-29

Emersa da test reali su una home da oltre 330.000 file e da una seconda
tornata di audit. Voci principali; il dettaglio completo è in
`docs/CHANGELOG-archive.md`.

### Aggiunto
- Pausa/Ripresa nella pagina Scansione, con granularità di file intero e
  sessione clamd ricreata dopo pause superiori a `IdleTimeout`.
- Log persistente delle scansioni programmate, con rotazione, progresso
  live e tooltip nella tray.
- CLI: `--exclude` ripetibile e `--max-stream-size`.
- 17 nuovi test (45 totali).

### Corretto
- Guard "una scansione alla volta" fra scansione manuale e programmata.
- La directory di quarantena è esclusa dall'attraversamento e i file già
  in quarantena non possono essere ri-quarantenati.
- Pre-check dimensionale `TOO_LARGE`: i file oltre `StreamMaxLength` non
  vengono più inviati a clamd.
- Sessione `IDSESSION` marcata come morta quando clamd chiude la
  connessione, per evitare fallimenti a cascata.
- `fcntl.flock` sulle sequenze read-modify-write di `index.json`.

### Modificato
- Traversata da `rglob` a `os.walk` con pruning e memoria costante.

## 0.1.2 — 5 problemi corretti (audit)

Permessi e atomicità della quarantena, passaggio a unit systemd utente,
classificazione `TOO_LARGE`, limiti inotify e riconciliazione dei watch.
Dettaglio in `docs/CHANGELOG-archive.md`.

## 0.1.1 — 4 problemi corretti (bug report pre-release)

Report di fine scansione, aggiornamento in tempo reale della pagina
Quarantena, copia del log, sfarfallio della finestra durante scansioni
molto grandi. Dettaglio in `docs/CHANGELOG-archive.md`.

## 0.1.0

Rilascio iniziale: CLI e GUI, scansione via protocollo nativo di clamd
(INSTREAM/IDSESSION), quarantena con indice JSON condiviso, Real-Time,
pianificazione, integrazione Dolphin, pacchettizzazione `.deb`.
