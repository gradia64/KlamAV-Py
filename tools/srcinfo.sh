#!/bin/sh
# Rigenera arch/.SRCINFO in un container archlinux:latest, come utente
# corrente e con arch/ in sola lettura: makepkg scrive solo in /tmp.
set -e
cd "$(dirname "$0")/.."
docker run --rm --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e BUILDDIR=/tmp -e PKGDEST=/tmp -e SRCDEST=/tmp \
  -e SRCPKGDEST=/tmp -e LOGDEST=/tmp \
  -v "$PWD/arch:/pkg:ro" -w /pkg \
  archlinux:latest makepkg --printsrcinfo > arch/.SRCINFO.new
mv arch/.SRCINFO.new arch/.SRCINFO
grep -E "pkgver|pkgrel|sha256sums" arch/.SRCINFO
