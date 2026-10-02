# Ruoli delle chiavi di rilascio, letti da tools/verify-tag.sh e
# tools/sign-release.sh. Unico punto in cui sono scritti: vedi il modello di
# fiducia in docs/RILASCIO.md.
#
# Una primaria, solo certificazione, sempre offline sulla macchina del
# maintainer: è l'impronta di validpgpkeys nel PKGBUILD e quella pubblicata
# nel README.
RELEASE_PRIMARY="${KLAMAV_RELEASE_PRIMARY:-EBEE3E80EFA38B42B147F1B99D7AA4F1971FEAA9}"
# Sottochiavi che possono firmare i TAG: solo quelle del maintainer, mai
# presenti nella CI. Più di una, separate da spazi, solo durante una
# rotazione.
TAG_SIGNING_SUBKEYS="${KLAMAV_TAG_SIGNING_SUBKEYS:-FDC222086F32BADB04034BAF707F1CD9C887FD2E}"
# Sottochiave che firma gli ALLEGATI della release, l'unica nei secret
# (environment «release» di GitHub). Se la CI viene compromessa si revoca
# solo questa: i tag passati restano verificabili.
CI_SIGNING_SUBKEY="${KLAMAV_CI_SIGNING_SUBKEY:-F9F4283586602F8DD724C467088861B04D8D328D}"
