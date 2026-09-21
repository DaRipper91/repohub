#!/bin/sh
# Build an RPM from the current working tree (tracked and untracked, not ignored, files).
#   packaging/rpm/build.sh [PYVER]        e.g. 3.12   (default: the system python3's version)
# The result lands in packaging/dist/.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
PYVER=${1:-$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
top=$(mktemp -d)
trap 'rm -rf "$top"' EXIT
mkdir -p "$top"/SOURCES "$top"/SPECS "$top"/src
(cd "$repo" && git ls-files -z -c -o --exclude-standard | tar --null -T - --transform "s,^,repohub-$VERSION/," -czf "$top/SOURCES/repohub-$VERSION.tar.gz")
sed "s/@VERSION@/$VERSION/g" "$here/repohub.spec" > "$top/SPECS/repohub.spec"
rpmbuild -bb --define "_topdir $top" --define "repohub_pyver $PYVER" "$top/SPECS/repohub.spec"
OUT=${OUT:-$repo/packaging/dist}
mkdir -p "$OUT"
cp "$top"/RPMS/*/repohub-*.rpm "$OUT/"
ls -la "$OUT"/repohub-*.rpm
