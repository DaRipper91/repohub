#!/bin/sh
# Build RepoHub-<version>-<arch>.AppImage. Bundles its own Python (python-build-standalone), so it needs nothing
# from the host but a Linux kernel with glibc. Needs: curl, tar, python3, mksquashfs (squashfs-tools), network.
#   packaging/appimage/build.sh [PYVER]          default 3.12
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
PYVER=${1:-3.12}
OUT=${OUT:-$repo/packaging/dist}
case "$(uname -m)" in aarch64) A=aarch64 ;; x86_64) A=x86_64 ;; *) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;; esac
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
app="$work/AppDir"
mkdir -p "$app/opt" "$OUT"

echo "== bundled Python $PYVER ($A)"
url=$(curl -fsSL https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest | PYVER="$PYVER" A="$A" python3 -c '
import json, os, sys
v, a = os.environ["PYVER"], os.environ["A"]
assets = json.load(sys.stdin)["assets"]
pick = [x["browser_download_url"] for x in assets
        if x["name"].startswith("cpython-%s." % v) and x["name"].endswith("%s-unknown-linux-gnu-install_only.tar.gz" % a)]
print(sorted(pick)[-1] if pick else "")')
[ -n "$url" ] || { echo "no python-build-standalone build for $PYVER/$A" >&2; exit 1; }
curl -fsSL "$url" -o "$work/python.tar.gz"
tar -xzf "$work/python.tar.gz" -C "$app/opt"   # unpacks to opt/python

echo "== RepoHub $VERSION"
PY="$app/opt/python/bin/python3"
"$PY" -m pip wheel --no-deps --disable-pip-version-check -q -w "$work/wheel" "$repo"
"$PY" -m pip install --disable-pip-version-check --no-compile --only-binary=:all: -q "$work"/wheel/repohub-*.whl
"$PY" -m pip uninstall -y -q pip setuptools >/dev/null 2>&1 || true
find "$app/opt/python" -name '__pycache__' -type d -prune -exec rm -rf {} +
"$PY" -m compileall -q -j 0 "$app/opt/python/lib" >/dev/null 2>&1 || true
rm -rf "$app/opt/python/lib/python$PYVER/test" "$app/opt/python/lib/python$PYVER/idlelib" "$app/opt/python/lib/python$PYVER/tkinter" 2>/dev/null || true

id=io.github.DaRipper91.RepoHub
install -m755 "$here/AppRun" "$app/AppRun"
sed 's|^Exec=.*|Exec=repohub-web --open|' "$repo/packaging/common/$id.desktop" > "$app/$id.desktop"
install -Dm644 "$repo/packaging/common/$id.svg" "$app/$id.svg"
ln -s "$id.svg" "$app/.DirIcon"
mkdir -p "$app/usr/share/metainfo"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$(date -u +%Y-%m-%d)/" "$repo/packaging/common/$id.metainfo.xml" > "$app/usr/share/metainfo/$id.appdata.xml"
install -Dm644 "$repo/LICENSE" "$app/usr/share/licenses/repohub/LICENSE"

echo "== appimagetool"
tool="$work/appimagetool"
curl -fsSL "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$A.AppImage" -o "$tool"
chmod +x "$tool"
# run it without FUSE (containers usually have none)
ARCH=$A "$tool" --appimage-extract-and-run --no-appstream "$app" "$OUT/RepoHub-$VERSION-$A.AppImage" >/dev/null 2>&1 \
    || { ARCH=$A "$tool" --appimage-extract-and-run --no-appstream "$app" "$OUT/RepoHub-$VERSION-$A.AppImage"; exit 1; }
chmod +x "$OUT/RepoHub-$VERSION-$A.AppImage"
ls -la "$OUT/RepoHub-$VERSION-$A.AppImage"
