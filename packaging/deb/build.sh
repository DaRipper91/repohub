#!/bin/sh
# Build a .deb on a Debian or Ubuntu system (run it inside a matching container: build-in-container.sh).
#   packaging/deb/build.sh [TAG]      TAG becomes part of the version, e.g. deb13 -> 0.12.0-1~deb13
# The package depends on the exact python3.X it was built with. Needs: python3 python3-venv dpkg-dev git tar
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
TAG=${1:-}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
PYVER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
ARCH=$(dpkg --print-architecture)
DEBVER="$VERSION-1${TAG:+~$TAG}"
OUT=${OUT:-$repo/packaging/dist}
root=$(mktemp -d)
trap 'rm -rf "$root"' EXIT

PYTHON="python$PYVER" sh "$repo/packaging/common/stage.sh" "$root"
# Debian wants the license as usr/share/doc/<pkg>/copyright
install -Dm644 "$repo/LICENSE" "$root/usr/share/doc/repohub/copyright"
rm -rf "$root/usr/share/licenses"

mkdir -p "$root/DEBIAN"
size=$(du -sk "$root" | cut -f1)
cat > "$root/DEBIAN/control" <<CONTROL
Package: repohub
Version: $DEBVER
Section: devel
Priority: optional
Architecture: $ARCH
Depends: python$PYVER, git
Recommends: gh
Installed-Size: $size
Maintainer: DaRipper91 <theripper81791@gmail.com>
Homepage: https://github.com/DaRipper91/repohub
Description: browse GitHub, GitLab and Codeberg like an app store
 RepoHub is a local app for finding and trying repositories. It searches
 GitHub, GitLab, Codeberg and any Forgejo server at once, keeps favorites with
 tags and notes, recommends projects, shows which repositories you already
 cloned and whether one can run on your machine, and clones safely.
 .
 It runs as a web app (repohub-web), a terminal app (repohub-tui) and a
 read-only command line (repohub), and has a read-only MCP server
 (repohub-mcp) for Claude Code. Light and dark themes are included.
 .
 This package bundles its Python dependencies in /opt/repohub.
CONTROL
# the venv's interpreter is a symlink to /usr/bin/python$PYVER: fine, the package depends on it

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$root" "$OUT/repohub_${DEBVER}_${ARCH}.deb" >/dev/null
ls -la "$OUT"/repohub_"${DEBVER}"_"${ARCH}".deb
