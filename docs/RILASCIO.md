# Rilascio di KlamAV-Py

Guida per chi pubblica le release. La procedura e le sue ragioni sono in
`docs/CONTESTO-PROGETTO.md`, sezione 4, «Rilascio»; qui ci sono il modello
di fiducia, la configurazione una tantum e i comandi.

## Modello di fiducia

Una chiave di rilascio, tre parti con ruoli separati (impronte in
`tools/release-keys.sh`, unico punto in cui sono scritte):

| Parte | Impronta | Dove sta | Firma |
|-------|----------|----------|-------|
| Primaria `[C]` | `EBEE 3E80 EFA3 8B42 B147  F1B9 9D7A A4F1 971F EAA9` | solo sulla macchina del maintainer | niente: certifica le sottochiavi. È l'impronta di `validpgpkeys` e del README |
| Sottochiave dei tag `[S]` | `FDC2 2208 6F32 BADB 0403  4BAF 707F 1CD9 C887 FD2E` (scade 2028-09-25) | solo sulla macchina del maintainer, **mai nella CI** | i tag `v*` (tutti, dalla 0.1.10) |
| Sottochiave della CI `[S]` | `F9F4 2835 8660 2F8D D724  C467 0888 61B0 4D8D 328D` (scade 2028-10-01) | secret dell'environment `release` | gli allegati della release (`.sig`, `SHA256SUMS.sig`) |

Cosa garantisce cosa:

- **La firma del tag** dice che il codice di quella versione l'ha approvato
  il maintainer. È quella che verificano `git tag -v` e makepkg (`?signed`):
  il pacchetto AUR si costruisce dal tag, non dagli allegati. Il job
  `verify` accetta solo la sottochiave dei tag: un tag firmato con la
  sottochiave della CI, valido per gpg, qui viene rifiutato.
- **La firma degli allegati** dice che quei file li ha prodotti la CI di
  questo repository da quel tag. Non dice nulla di più del tag.
- **Se la CI o i secret vengono compromessi**: si revoca solo la
  sottochiave della CI. Le firme dei tag passati restano valide, la
  primaria e `validpgpkeys` non cambiano (piano in «Rotazione e revoca»).

Cosa resta esposto se la CI, o l'environment `release`, viene
compromessa, finché la sottochiave della CI non è revocata e la chiave
pubblica ripubblicata:

- **allegati delle release**: chi ha la sottochiave della CI firma file a
  sua scelta, e la firma è valida per chiunque verifichi gli allegati;
  il job `publish` ha anche il permesso di caricarli sulle release
  esistenti (`--clobber`);
- **tag per makepkg**: un tag firmato con la sottochiave della CI passa la
  verifica di makepkg (vedi i limiti sotto), non quella di `verify`;
- **AUR**: la chiave SSH di AUR permette di pubblicare un PKGBUILD
  qualunque per `klamav-py` e `olladesk`, anche con un'altra sorgente o
  un altro `validpgpkeys`: è l'esposizione più ampia, perché non passa da
  nessuna firma.

Restano fuori dalla portata della CI: la primaria e la sottochiave dei
tag (mai nei secret), quindi i tag già pubblicati, la loro verifica con
`git tag -v` e con `verify`, e la possibilità di certificare una nuova
sottochiave.

Limiti da sapere:

- makepkg accetta una firma di **qualunque** sottochiave della primaria in
  `validpgpkeys`. Chi avesse la sottochiave della CI potrebbe firmare un
  tag che makepkg accetta, anche se `verify` lo rifiuta: per questo la
  sottochiave sta solo nell'environment protetto, e va revocata subito al
  primo sospetto.
- La chiave SSH di AUR nella CI dà accesso a tutto l'account AUR
  (`klamav-py` e `olladesk`): chi la ottiene può pubblicare un PKGBUILD a
  sua scelta. L'approvazione dell'environment è l'ultimo controllo prima
  di AUR.

La separazione non rende innocua una CI compromessa: la rende
rimediabile senza danni allo storico.

## Cosa fa il workflow

Al push di un tag `v<versione>` parte `.github/workflows/release.yml`:

| Job       | Cosa fa |
|-----------|---------|
| `verify`  | il tag è annotato e firmato dalla sottochiave dei tag (`tools/verify-tag.sh`); tag = versione in `__init__.py`, PKGBUILD, `debian/changelog`, voce di `CHANGELOG.md` (`tools/check-version.sh`) |
| `tests`   | la stessa matrice di `tests.yml` (Python 3.10–3.14) |
| `deb`     | in `debian:sid`: `dpkg-buildpackage -us -uc -b`, lintian (gli errori bloccano), tarball del sorgente da `git archive` |
| `arch`    | in `archlinux:base-devel`: il PKGBUILD così com'è, che clona il tag da GitHub e ne verifica la firma; namcap (gli errori bloccano), `.SRCINFO` rigenerato, installazione e prova della CLI (`tools/build-arch.sh`) |
| `publish` | **attende la tua approvazione** (environment `release`), poi: firma degli allegati con la sottochiave della CI (`tools/sign-release.sh`), release GitHub con le note dalla voce di `CHANGELOG.md` (`tools/release-notes.sh`), PKGBUILD, `klamav-py.install` e `.SRCINFO` su AUR (`tools/publish-aur.sh`) |

Solo `publish` vede i secret. Nessun altro job, e nessun altro workflow
(`tests.yml` da un branch compreso), li legge.

File allegati alla release:

```
klamav-py_X.Y.Z-1_all.deb           pacchetto Debian (costruito su sid)
klamav-py-X.Y.Z-1-any.pkg.tar.zst   pacchetto Arch (lo stesso che AUR costruisce)
klamav-py-X.Y.Z.tar.gz              sorgente del tag (git archive)
*.sig                               firma GPG staccata di ciascun file
SHA256SUMS, SHA256SUMS.sig          checksum di tutto, firmati
klamav-py-release-key.asc           chiave pubblica di rilascio
```

## Configurazione (una volta sola)

### 1. Chiave pubblica aggiornata

Dopo aver aggiunto la sottochiave della CI, la chiave pubblica va
ripubblicata ovunque: chi ha la versione vecchia non conosce la nuova
sottochiave e non riesce a verificare gli allegati.

```bash
gpg --armor --export EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 > arch/klamav-py-release-key.asc
gpg --keyserver hkps://keys.openpgp.org --send-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
gpg --keyserver hkps://keyserver.ubuntu.com --send-keys EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
```

### 2. Environment `release` su GitHub

Settings → Environments → New environment, nome `release`:

- **Required reviewers**: il maintainer. Ogni rilascio si ferma prima di
  `publish` finché non approvi.
- **Deployment branches and tags**: «Selected branches and tags», regola
  sul tag `v*`. Un job che usa l'environment da un branch non parte.
- **Environment secrets**:
  ```bash
  gpg --armor --export-secret-subkeys 'F9F4283586602F8DD724C467088861B04D8D328D!' \
      | gh secret set GPG_PRIVATE_KEY --env release -R gradia64/KlamAV-Py
  gh secret set GPG_PASSPHRASE --env release -R gradia64/KlamAV-Py
  gh secret set AUR_SSH_PRIVATE_KEY --env release -R gradia64/KlamAV-Py < ~/.ssh/aur_klamav_ci
  ```
  Il `!` esporta solo la sottochiave della CI: né la primaria né la
  sottochiave dei tag.

Poi togli i secret a livello di repository, se ci sono:

```bash
gh secret delete GPG_PRIVATE_KEY -R gradia64/KlamAV-Py
gh secret delete GPG_PASSPHRASE -R gradia64/KlamAV-Py
gh secret delete AUR_SSH_PRIVATE_KEY -R gradia64/KlamAV-Py
```

e controlla che restino solo quelli dell'environment:

```bash
gh secret list -R gradia64/KlamAV-Py            # vuoto
gh secret list --env release -R gradia64/KlamAV-Py
```

### 3. git firma con la sottochiave dei tag

Con due sottochiavi di firma nel portachiavi, gpg sceglie da sé la più
recente, cioè quella della CI, se la chiave è indicata senza `!`. Nel
repository va fissata quella dei tag (vale per tag e commit firmati):

```bash
git config --local user.signingkey 'FDC222086F32BADB04034BAF707F1CD9C887FD2E!'
```

Se non ti serve firmare gli allegati in locale, puoi anche togliere dal
portachiavi la parte privata della sottochiave della CI, dopo averla
caricata nell'environment: resta solo su GitHub, e per sostituirla basta
crearne una nuova con la primaria.

### 4. AUR

La chiave SSH della CI è `~/.ssh/aur_klamav_ci`, senza passphrase,
registrata nel profilo AUR accanto a quella per l'uso a mano
(`aur_klamav`). Senza il secret `AUR_SSH_PRIVATE_KEY` il passo viene
saltato con un avviso.

## Rilascio

1. Commit di rilascio e merge su `main` con la CI verde (CONTESTO, punti 1
   e 2). **Rileggi la voce del CHANGELOG** così come diventerà la
   descrizione della release:
   ```bash
   tools/release-notes.sh 0.1.15
   tools/check-version.sh v0.1.15
   ```
   Il workflow la pubblica così com'è: correggerla dopo vuol dire
   modificare la release a mano.
2. Tag firmato con la sottochiave dei tag (sezione 3) e push:
   ```bash
   git tag -s v0.1.15 -m "KlamAV-Py 0.1.15"
   tools/verify-tag.sh v0.1.15
   git push origin v0.1.15
   ```
   `tools/verify-tag.sh` in locale, prima del push, dice subito se il tag è
   stato firmato con la sottochiave sbagliata.
3. Segui il workflow (`gh run watch`) e approva `publish` quando `deb` e
   `arch` sono verdi. Se `publish` fallisce dopo l'upload si può
   rilanciare: l'upload sovrascrive (`--clobber`) e il push su AUR non fa
   nulla se AUR è già aggiornato.

Una release creata a mano prima del push (per esempio con note in più per
chi aggiorna) resta com'è: il workflow carica solo i file.

`arch/.SRCINFO` nel repository resta da aggiornare nel commit di rilascio
(`pkgver` e `source`); quello pubblicato su AUR lo rigenera il workflow.

## Rotazione e revoca

**Compromissione della CI o dei secret** (o solo il sospetto), in
quest'ordine:

1. Togli la chiave SSH della CI (`aur_klamav_ci`) dal profilo AUR e
   cancella i secret dell'environment `release`: niente più pubblicazioni.
2. Revoca la sottochiave della CI e creane una nuova:
   ```bash
   gpg --edit-key EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9
   #   selezionare la sottochiave F9F4…328D (key N), poi: revkey, save
   gpg --quick-add-key EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 ed25519 sign 2y
   ```
3. Ripubblica la chiave pubblica con la revoca (sezione 1: file nel
   repository, keyserver, profilo GitHub): chi non la aggiorna continua a
   considerare valida la sottochiave revocata.
4. Controlla la storia git dei pacchetti AUR (`klamav-py` e `olladesk`) e
   gli allegati delle release dopo la data sospetta; ripristina da tag
   firmati ciò che non hai pubblicato tu.
5. Aggiorna `CI_SIGNING_SUBKEY` in `tools/release-keys.sh`, ricarica
   `GPG_PRIVATE_KEY` nell'environment con la nuova sottochiave e registra
   su AUR una nuova chiave SSH della CI.

Non cambiano: la sottochiave dei tag, `TAG_SIGNING_SUBKEYS`, la primaria e
`validpgpkeys` nel PKGBUILD. Lo storico dei tag resta valido, perché
nessun tag è firmato dalla sottochiave revocata.

**Scadenze**: prorogale prima che scadano (tag 2028-09-25, CI
2028-10-01), poi ripubblica la chiave pubblica:

```bash
gpg --quick-set-expire EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 3y FDC222086F32BADB04034BAF707F1CD9C887FD2E
gpg --quick-set-expire EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 2y F9F4283586602F8DD724C467088861B04D8D328D
```

**Rotazione della sottochiave dei tag**: aggiungi la nuova a
`TAG_SIGNING_SUBKEYS` (separate da spazi) prima di firmare con essa, e
togli la vecchia solo quando non serve più verificare tag nuovi con quella.
Revocarla invaliderebbe la verifica di tutti i tag che ha firmato.

## In locale

Gli script funzionano anche fuori dalla CI:

```bash
tools/verify-tag.sh v0.1.15
tools/check-version.sh v0.1.15
tools/release-notes.sh 0.1.15
docker run --rm -v "$PWD":/src -w /src archlinux:base-devel tools/build-arch.sh
tools/sign-release.sh dist     # con il portachiavi locale (sottochiave della CI)
```
