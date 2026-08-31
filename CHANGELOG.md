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
