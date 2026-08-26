# klamav-py

Riscrittura minimale, in Python, dell'idea alla base di KlamAV 0.22
(frontend a ClamAV), senza le parti diventate tecnologia morta
(Dazuko, DCOP, Qt3) e senza i problemi di sicurezza dell'originale
(shell injection via `KShellProcess`).

## Requisiti

- `clamav-daemon` installato e attivo (`clamd`), non i soli binari
  `clamscan`/`freshclam`: questo progetto parla col demone via socket,
  non invoca eseguibili esterni per la scansione.
- Python 3.10+ (usa il walrus operator in `clamd_client.py`).
- CLI: nessuna dipendenza esterna a runtime, solo libreria standard —
  gira anche con il Python di sistema, senza venv.
- GUI: `PySide6`, va installato in un venv dedicato (vedi sotto), non
  nel Python di sistema.
- GUI, solo per l'aggiornamento del database virus dal pulsante
  "Aggiorna Database": `freshclam` nel `PATH` e `pkexec` (PolicyKit)
  disponibile, dato che l'operazione richiede privilegi di root.
- GUI, solo per l'integrazione col menu contestuale di Dolphin:
  un ambiente KDE Plasma con `kbuildsycoca5`/`kbuildsycoca6`.
- Sviluppo/test: `pytest` (vedi `requirements-dev.txt`), non richiesto
  a runtime.

## Setup del venv (solo per la GUI)

```bash
cd klamav-py
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Da lì in poi, con il venv attivo:

```bash
python3 -m klamav_py.gui.app
```

Se preferisci non attivare il venv ogni volta, puoi chiamare direttamente
l'interprete del venv:

```bash
klamav-py/venv/bin/python -m klamav_py.gui.app
```

Questo è anche il motivo per cui la `ExecStart=` in
`systemd/klamav-scan.service` punta a `/opt/klamav-py/venv/bin/klamav-py`
e non a un comando globale: la CLI non ha bisogno del venv (nessuna
dipendenza esterna), ma se installi tutto insieme in un unico venv sotto
`/opt/klamav-py/venv` funziona comunque, sia per CLI che per GUI.

## Uso rapido

```bash
# CLI: verifica che clamd risponda (funziona anche senza venv)
python3 -m klamav_py.cli ping

# CLI: scansione con quarantena automatica
python3 -m klamav_py.cli scan /home/utente/Scaricati --quarantine /var/lib/klamav-py/quarantine

# CLI: scansione con log dettagliato degli errori su file
python3 -m klamav_py.cli scan /home --log-errors /tmp/klamav-errors.log

# GUI (richiede il venv attivo o l'interprete del venv, vedi sopra)
python3 -m klamav_py.gui.app
# oppure, con socket/quarantena non standard:
python3 -m klamav_py.gui.app --socket /run/clamav/clamd.ctl --quarantine-dir ~/.local/share/klamav-py/quarantine
# oppure per avviare direttamente la scansione di un percorso (usato
# anche dall'integrazione Dolphin, vedi sotto):
python3 -m klamav_py.gui.app --scan-target /percorso/da/scansionare
```

## Connessione persistente a clamd

Di default `scan_stream` riusa una singola connessione (sessione
`IDSESSION` di clamd) per più file di fila invece di aprirne una nuova
per ognuno — su alberi con decine di migliaia di file evita l'overhead
di connect ripetuto che si vedeva nel primo test (90698 file
scansionati con una connessione per file). La sessione viene comunque
richiusa e riaperta ogni 500 file (`--session-batch-size` per la CLI)
per limitare l'impatto di eventuali limiti non documentati su sessioni
molto lunghe, e viene ricreata da zero ogni volta che qualcosa va
storto: un file problematico non compromette il resto della scansione,
esattamente come nella versione precedente non persistente.

Se sospetti che un problema sia legato specificamente alla sessione
persistente, `--no-persistent` torna al comportamento "una connessione
per file" per isolare il caso.

A fine scansione la CLI stampa anche un riepilogo degli errori
raggruppati per tipo (es. "2 Permission denied", "1 Broken pipe"),
invece di dover fare `grep`/`sort`/`uniq -c` manualmente sull'output.

## GUI: panoramica dell'interfaccia

La GUI (`klamav_py/gui/`) è un'applicazione PySide6 a finestra unica
con una barra laterale e uno stile ispirato a KDE Plasma (usa sempre i
colori della palette di sistema, mai colori hardcoded, tranne per gli
stati semantici come "infetto"). È organizzata in sette sezioni:

- **Scansione** — avvia una scansione manuale su un percorso a scelta.
  Esegue in un `QThread` separato (`ScanWorker`), quindi la finestra
  resta reattiva anche su directory grandi, e mostra i risultati man
  mano che arrivano invece di bloccare fino alla fine come faceva
  l'originale con `KProcess` gestito male. Durante la scansione, la
  barra di stato mostra il file che si sta processando in quel momento
  (non solo il risultato a cose fatte).

  **Quarantena manuale di default.** La casella "Metti in quarantena
  automaticamente i file infetti" è disattivata di default: i file
  segnalati come infetti restano dove sono, e li sposti tu manualmente
  selezionandoli nella lista e premendo "Metti in quarantena i
  selezionati" — comodo per controllare un eventuale falso positivo
  prima di spostare qualcosa. Se preferisci il comportamento
  automatico ("vecchio stile"), spunta la casella prima di avviare la
  scansione.

- **Cronologia** — registro persistente (`~/.local/share/klamav-py/history.json`,
  ultime 1000 voci) di tutte le scansioni eseguite, manuali,
  programmate o real-time, con data/ora, percorso, numero di file
  scansionati, infetti ed errori.

- **Quarantena** — legge lo stesso indice JSON usato dalla CLI
  (`index.json` nella directory di quarantena), quindi i due strumenti
  sono intercambiabili sugli stessi dati. Permette di ripristinare un
  file nella posizione originale o eliminarlo definitivamente.

- **Aggiornamenti** — scarica le definizioni virus lanciando
  `freshclam --stdout` tramite `pkexec` (autenticazione PolicyKit),
  fermando e riavviando automaticamente il demone `clamav-freshclam`/
  `freshclam` di sistema per evitare conflitti di lock sul file di
  log. L'output del comando è mostrato in tempo reale in una console.
  Di default parte automaticamente 1.5s dopo l'avvio dell'app
  (disattivabile in Impostazioni).

- **Real-Time** — mostra il log delle scansioni automatiche eseguite
  sulle cartelle monitorate (impostabili in Impostazioni). Il
  monitoraggio usa `QFileSystemWatcher`: quando un file in una
  cartella osservata viene creato o modificato, la scansione parte
  dopo un debounce di 3 secondi (per non scansionare un file ancora in
  fase di scrittura) e i file infetti vengono **sempre** messi in
  quarantena automaticamente, a differenza della pagina Scansione dove
  di default è manuale.

  **Scelta di design intenzionale**, non un'incoerenza: le cartelle
  monitorate in Real-Time sono tipicamente destinazioni di file appena
  scaricati/ricevuti da fuori (es. `~/Scaricati`), non file già
  presenti da tempo sul sistema — il rischio di un falso positivo su
  un file "nuovo" e ancora non toccato da nessuno è basso, quindi ha
  senso agire subito. La pagina Scansione, invece, può essere puntata
  su cartelle con file di sistema o dati importanti, dove un falso
  positivo va verificato prima di spostare qualcosa: da qui la
  quarantena manuale di default.

- **Pianificazione** — scansione automatica ricorrente (ogni N ore o
  giorni) su una cartella a scelta, tramite un `QTimer` interno.
  Richiede che l'applicazione resti in esecuzione (anche minimizzata
  nella system tray): non è un timer di sistema come
  `systemd/klamav-scan.timer`, che invece funziona anche a GUI chiusa
  ma va configurato ed eseguito separatamente (vedi sezione dedicata
  più sotto).

- **Impostazioni** — socket di clamd, directory di quarantena,
  cartelle monitorate per il Real-Time, autostart al login,
  avvio minimizzato nella system tray, aggiornamento automatico del
  database all'avvio, e installazione/rimozione dell'integrazione col
  menu contestuale di Dolphin.

Tutte le impostazioni sono salvate con `QSettings` (organizzazione
"KlamAV", applicazione "KlamAV" — su Linux tipicamente
`~/.config/KlamAV/KlamAV.conf`).

### System tray e single-instance

L'app resta attiva nella system tray anche chiudendo la finestra (la
`X` la nasconde, non la termina: si esce dal menu della tray o da
"Esci"). È pensata per restare in esecuzione in background per la
pianificazione e il monitoraggio real-time.

È inoltre single-instance: se lanci `klamav_py.gui.app` mentre un'altra
istanza è già attiva, la seconda istanza invia il proprio
`--scan-target` (se presente) alla prima via `QLocalServer`/
`QLocalSocket` locale e termina subito, invece di aprire una seconda
finestra. Questo è il meccanismo che rende sensata l'integrazione con
Dolphin: cliccando "Scansiona con KlamAV-Py" su più file/cartelle in
sessioni diverse, tutte le richieste finiscono nella stessa finestra
già aperta.

### Integrazione Dolphin

Dal pannello Impostazioni si può installare una voce "Scansiona con
KlamAV-Py" nel menu contestuale di Dolphin, scrivendo un file
`.desktop` in `~/.local/share/kservices5/ServiceMenus/` e
`~/.local/share/kio/servicemenus/` e rigenerando la cache dei servizi
KDE (`kbuildsycoca5`/`6`). Vedi la sezione "Problemi noti" più sotto
per una precisazione sul quoting del percorso passato dalla shell.

### Autostart

Se abilitato, scrive un file `.desktop` in `~/.config/autostart/` che
lancia la GUI con l'interprete Python correntemente in uso; se
disabilitato, rimuove il file.

## Cosa manca rispetto a KlamAV originale (di proposito)

- **Nessun on-access scanning kernel-level.** Il monitoraggio
  Real-Time di questa GUI è basato su `QFileSystemWatcher` (polling
  lato Qt su modifiche a directory osservate esplicitamente, non
  ricorsivo, con qualche secondo di latenza) — non è un sostituto di
  un vero on-access scanner. Per quello ClamAV fornisce già
  `clamonacc` (fanotify-based): non ha senso reimplementare un
  equivalente di Dazuko. Se ti serve on-access reale, configuralo via
  `clamd.conf`/`clamav-clamonacc.service`, non è compito di questo
  progetto.
- **Nessuna integrazione mail client-side.** Se ti serve scansione
  della posta, oggi ha più senso lato server (milter) che lato client
  KMail/Evolution come faceva `klammail`.

## Problemi noti (audit) — stato

Punti emersi rileggendo l'intero codice dopo le ultime modifiche.

**Corretti:**

- **Avvio non più bloccante.** Il ping a clamd all'apertura della
  finestra ora gira in un `QThread` dedicato (`gui/ping_worker.py`,
  `PingWorker`) invece che in modo sincrono nel thread della UI: la
  finestra appare subito, l'eventuale avviso "clamd non raggiungibile"
  arriva in modo asincrono quando il ping risponde (o va in timeout).
- **Quoting nel file `.desktop` di Dolphin.** Rimosso il wrapper
  `sh -c '... "%f"'`: ora `Exec=` invoca direttamente l'interprete
  Python con `%f` come argomento, senza passare da una shell
  intermedia. Elimina il rischio che un nome file con `"`, `` ` `` o
  `$` rompa il quoting — esattamente il tipo di problema che il
  progetto dichiara di aver eliminato rispetto a klamav 0.22.
- **Gestione errori in `_manage_autostart()`.** Ora ritorna
  `(successo, messaggio_errore)` invece di sollevare un'eccezione non
  gestita: se la scrittura in `~/.config/autostart/` fallisce (es. per
  permessi), `SettingsPage` mostra un avviso invece di un traceback.
- **Import inutilizzato** in `gui/scan_worker.py` (`ScanResult`)
  rimosso; verificato con `pyflakes` che non ce ne siano altri.
- **Test automatici aggiunti** in `tests/` (pytest) per la parte di
  logica pura, senza dipendenze da clamd o da Qt: parsing delle
  risposte del protocollo clamd (`test_clamd_client.py`), gestione
  della quarantena su filesystem temporaneo (`test_quarantine.py`) e
  parsing/raggruppamento errori della CLI (`test_cli.py`). Si lanciano
  dentro il venv (vedi "Setup del venv" più sopra):

  ```bash
  cd klamav-py
  python3 -m venv venv        # se non l'hai già creato
  source venv/bin/activate
  pip install -r requirements-dev.txt
  python3 -m pytest tests/
  ```

  Su distribuzioni con Python "externally managed" (Debian/Ubuntu
  recenti, PEP 668), `pip install -r requirements-dev.txt` **fuori**
  da un venv fallisce con `externally-managed-environment`: è il
  comportamento atteso del sistema, non un problema di questo
  progetto — usa il venv come sopra, non `--break-system-packages`.

  La GUI (widget PySide6, worker `QThread`) non è ancora coperta da
  test automatici: richiederebbe `pytest-qt` o un mocking più
  strutturato di `ClamdClient`, valutabile come prossimo passo.

**Non un problema — comportamento confermato intenzionale:** la
quarantena sempre automatica nella sezione Real-Time (vedi la nota
nella descrizione della sezione più sopra).

Nessun problema di sicurezza rilevato in `clamd_client.py`, `cli.py` o
`update_worker.py`: quest'ultimo passa a `pkexec sh -c` una stringa di
comando statica, senza interpolazione di input proveniente dall'utente
o dal filesystem, quindi non è iniettabile.

## Estensioni naturali

- Notifiche desktop (`notify-send` o D-Bus diretto) quando il timer
  trova un'infezione — oggi già presente in forma di notifica tramite
  system tray (`QSystemTrayIcon.showMessage`) sia per la pianificazione
  che per il Real-Time; da valutare se serve anche un canale D-Bus
  nativo per desktop diversi da quelli con supporto tray.
- Esportazione dell'indice quarantena in un formato che un frontend
  web/TUI possa leggere, se vuoi vederlo dal NAS invece che da riga di
  comando.
- Rendere configurabile da UI se il Real-Time debba mettere in
  quarantena automaticamente o solo segnalare (vedi "Problemi noti").
