# Rilascio di KlamAV-Py

Guida per chi pubblica le release. La procedura e le sue ragioni sono in
`docs/CONTESTO-PROGETTO.md`, sezione 4, «Rilascio»; qui ci sono la
configurazione una tantum e i comandi.

## Cosa fa il workflow

Al push di un tag `v<versione>` parte `.github/workflows/release.yml`:

| Job       | Cosa fa |
|-----------|---------|
| `verify`  | il tag è annotato e firmato dalla chiave di rilascio (`tools/verify-tag.sh`); tag = versione in `__init__.py`, PKGBUILD, `debian/changelog`, voce di `CHANGELOG.md` (`tools/check-version.sh`); secret di firma presente |
| `tests`   | la stessa matrice di `tests.yml` (Python 3.10–3.14) |
| `deb`     | in `debian:sid`: `dpkg-buildpackage -us -uc -b`, lintian (gli errori bloccano), tarball del sorgente da `git archive` |
| `arch`    | in `archlinux:base-devel`: il PKGBUILD così com'è, che clona il tag da GitHub e ne verifica la firma; namcap (gli errori bloccano), `.SRCINFO` rigenerato, installazione e prova della CLI (`tools/build-arch.sh`) |
| `publish` | firma di tutti gli artefatti con la sottochiave (`tools/sign-release.sh`), release GitHub con le note dalla voce di `CHANGELOG.md` (`tools/release-notes.sh`), PKGBUILD, `klamav-py.install` e `.SRCINFO` su AUR (`tools/publish-aur.sh`) |

Il workflow **non firma tag**: li firma solo il maintainer. Un tag non
firmato, o firmato con un'altra chiave, si ferma al primo job senza
pubblicare nulla.

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

I secret valgono per singolo repository: quelli di OllaDesk non servono
qui, e la sua chiave è un'altra.

### 1. Sottochiave di firma

La chiave di rilascio ha già una sottochiave di sola firma:

```
pub  ed25519  EBEE 3E80 EFA3 8B42 B147  F1B9 9D7A A4F1 971F EAA9   [C]   (primaria)
sub  ed25519  FDC2 2208 6F32 BADB 0403  4BAF 707F 1CD9 C887 FD2E   [S]   scade il 2028-09-25
```

Nei secret va **solo la sottochiave**: il `!` dopo l'ID esclude tutto il
resto, e la primaria resta sulla tua macchina come stub. Senza file
intermedi:

```bash
gpg --armor --export-secret-subkeys '707F1CD9C887FD2E!' \
    | gh secret set GPG_PRIVATE_KEY -R gradia64/KlamAV-Py
gh secret set GPG_PASSPHRASE -R gradia64/KlamAV-Py     # chiede la passphrase
```

Le firme fatte con la sottochiave si verificano con la chiave pubblica di
sempre (`arch/klamav-py-release-key.asc`, impronta della primaria), e
makepkg le accetta con il `validpgpkeys` attuale.

Se la CI o i secret venissero compromessi: revoca la sottochiave
(`gpg --edit-key EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9`, `key 1`,
`revkey`), aggiungine una nuova (`addkey`), ripubblica la chiave pubblica
(file nel repository e keyserver) e aggiorna il secret. La primaria e
`validpgpkeys` non cambiano.

Prima del 2028-09-25 proroga la sottochiave:

```bash
gpg --quick-set-expire EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 3y FDC222086F32BADB04034BAF707F1CD9C887FD2E
gpg --armor --export EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9 > arch/klamav-py-release-key.asc
```

poi committa la chiave pubblica, ripubblicala sui keyserver e riesporta il
secret come sopra.

### 2. AUR

Se l'account AUR che mantiene `klamav-py` è lo stesso di OllaDesk puoi
riusare la sua chiave SSH dedicata; altrimenti creane una e aggiungila al
profilo AUR:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/aur_klamav_py -C "klamav-py CI"
gh secret set AUR_SSH_PRIVATE_KEY -R gradia64/KlamAV-Py < ~/.ssh/aur_klamav_py
```

Senza questo secret il passo AUR viene saltato con un avviso e il resto
della release procede.

## Rilascio

1. Commit di rilascio e merge su `main` con la CI verde (CONTESTO, punti 1
   e 2).
2. Tag firmato e push:
   ```bash
   git tag -s v0.1.15 -m "KlamAV-Py 0.1.15"
   git tag -v v0.1.15
   git push origin v0.1.15
   ```
3. Segui il workflow (`gh run watch`). Se `publish` fallisce dopo l'upload
   si può rilanciare il job: l'upload sovrascrive (`--clobber`) e il push
   su AUR non fa nulla se AUR è già aggiornato.

Una release creata a mano prima del push (per esempio con note in più per
chi aggiorna) resta com'è: il workflow carica solo i file.

`arch/.SRCINFO` nel repository resta da aggiornare nel commit di rilascio
(`pkgver` e `source`); quello pubblicato su AUR lo rigenera il workflow.

## In locale

Gli script funzionano anche fuori dalla CI:

```bash
tools/verify-tag.sh v0.1.15
tools/check-version.sh v0.1.15
tools/release-notes.sh 0.1.15
docker run --rm -v "$PWD":/src -w /src archlinux:base-devel tools/build-arch.sh
tools/sign-release.sh dist     # con il portachiavi locale
```
