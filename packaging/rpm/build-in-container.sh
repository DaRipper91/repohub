#!/bin/sh
# Build the RPM inside a clean Fedora container (podman), so nothing is installed on this machine.
#   packaging/rpm/build-in-container.sh [FEDORA_RELEASE] [PYVER]     e.g. 44 3.12
# Needs network (image + dependency wheels). The RPM lands in packaging/dist/.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
release=${1:-44}
pyver=${2:-3.12}
mkdir -p "$repo/packaging/dist"
podman run --rm -v "$repo":/src:ro,Z -v "$repo/packaging/dist":/out:Z "registry.fedoraproject.org/fedora:$release" sh -ec "
  dnf -y -q install rpm-build git python$pyver desktop-file-utils libappstream-glib >/dev/null
  git config --global --add safe.directory '*'
  cp -a /src /work && cd /work
  OUT=/out sh packaging/rpm/build.sh $pyver
"
