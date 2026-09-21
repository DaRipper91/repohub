#!/bin/sh
# Build the .deb inside a clean Debian/Ubuntu container (podman).
#   packaging/deb/build-in-container.sh IMAGE TAG     e.g. docker.io/library/debian:trixie deb13
#                                                          docker.io/library/ubuntu:24.04 ubuntu2404
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
image=${1:-docker.io/library/debian:trixie}
tag=${2:-deb13}
mkdir -p "$repo/packaging/dist"
podman run --rm -v "$repo":/src:ro,Z -v "$repo/packaging/dist":/out:Z "$image" sh -ec "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq >/dev/null
  apt-get install -y -qq --no-install-recommends python3 python3-venv dpkg-dev git tar ca-certificates >/dev/null
  git config --global --add safe.directory '*'
  mkdir /work && cd /src && git ls-files -z -c -o --exclude-standard | tar --null -T - -cf - | tar -xf - -C /work
  cd /work && OUT=/out sh packaging/deb/build.sh $tag
"
