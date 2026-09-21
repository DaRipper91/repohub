#!/bin/sh
# Build the Arch package inside an Arch container (podman), as a normal user (makepkg refuses to run as root).
#   packaging/arch/build.sh [IMAGE]       default: an Arch Linux ARM image on aarch64, the official image on x86_64
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
case "$(uname -m)" in
    aarch64) image=${1:-docker.io/menci/archlinuxarm:latest}; keyring="archlinuxarm" ;;
    *)       image=${1:-docker.io/library/archlinux:latest}; keyring="archlinux" ;;
esac
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
mkdir -p "$repo/packaging/dist"
podman run --rm -v "$repo":/src:ro,Z -v "$repo/packaging/dist":/out:Z "$image" sh -ec "
  pacman-key --init >/dev/null 2>&1; pacman-key --populate $keyring >/dev/null 2>&1
  pacman --disable-sandbox -Sy --noconfirm --needed base-devel git python 2>&1 | tail -3
  useradd -m builder
  install -d -o builder /home/builder/pkg && cd /home/builder/pkg
  git config --global --add safe.directory '*'
  (cd /src && git ls-files -z -c -o --exclude-standard | tar --null -T - --transform 's,^,repohub-$VERSION/,' -czf /home/builder/pkg/repohub-$VERSION.tar.gz)
  sed 's/@VERSION@/$VERSION/' /src/packaging/arch/PKGBUILD > PKGBUILD
  chown -R builder .
  su builder -c 'makepkg -f --noconfirm --nodeps' 2>&1 | tail -3
  cp *.pkg.tar.* /out/
  ls -la /out/*.pkg.tar.*
"
