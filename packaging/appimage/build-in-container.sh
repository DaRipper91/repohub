#!/bin/sh
# Build the AppImage inside a clean Fedora container (podman): nothing is installed on this machine.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
mkdir -p "$repo/packaging/dist"
podman run --rm -v "$repo":/src:ro,Z -v "$repo/packaging/dist":/out:Z registry.fedoraproject.org/fedora:44 sh -ec "
  dnf -y -q install curl tar gzip python3 squashfs-tools git findutils file >/dev/null
  cp -a /src /work && cd /work
  OUT=/out sh packaging/appimage/build.sh ${1:-3.12}
"
