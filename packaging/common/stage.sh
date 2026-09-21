#!/bin/sh
# Stage a self-contained RepoHub install tree for a package.
#
#   stage.sh DESTDIR [WHEEL]
#
# Environment: PYTHON (default python3; use a versioned one such as python3.12 so the package can depend on
# exactly that interpreter), OPT (default /opt/repohub), BIN (default /usr/bin).
#
# Layout under DESTDIR: $OPT/venv (the app and its dependencies), $BIN/repohub{,-web,-tui,-mcp} launchers,
# and desktop entry, icon, AppStream metadata, systemd user unit and docs under /usr/share and /usr/lib.
# Every path baked into the tree is the FINAL path (not DESTDIR), so it can be moved into place as is.
set -eu

DESTDIR=${1:?usage: stage.sh DESTDIR [WHEEL]}
WHEEL=${2:-}
PYTHON=${PYTHON:-python3}
OPT=${OPT:-/opt/repohub}
BIN=${BIN:-/usr/bin}
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
DATE=${SOURCE_DATE_EPOCH:+$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%d)}
DATE=${DATE:-$(date -u +%Y-%m-%d)}

venv="$DESTDIR$OPT/venv"
mkdir -p "$DESTDIR$OPT"
"$PYTHON" -m venv "$venv"
if [ -z "$WHEEL" ]; then  # build with the venv's own pip: the system Python may not have one
    "$venv/bin/python" -m pip wheel --no-deps --disable-pip-version-check -q -w "$tmp/wheel" "$repo"
    WHEEL=$(ls "$tmp"/wheel/repohub-*.whl)
fi
# Dependencies come from a hash-locked list (packaging/requirements.lock) and only as ready-made wheels: no compiler,
# and a tampered or swapped package fails the hash check. Regenerate the list with packaging/lock.sh.
"$venv/bin/python" -m pip install --disable-pip-version-check --no-compile --only-binary=:all: --require-hashes --no-deps -q -r "$repo/packaging/requirements.lock"
"$venv/bin/python" -m pip install --disable-pip-version-check --no-compile --no-deps -q "$WHEEL"
"$venv/bin/python" -m pip uninstall -y -q pip >/dev/null 2>&1 || true
if "$venv/bin/python" -m pip --version >/dev/null 2>&1; then echo "pip is still present" >&2; exit 1; fi

# Launchers replace the venv's own scripts (their shebangs would point at DESTDIR).
find "$venv/bin" -mindepth 1 ! -name 'python*' -delete
sed -i '/^command = /d' "$venv/pyvenv.cfg"
find "$venv" -name '__pycache__' -type d -prune -exec rm -rf {} +
"$venv/bin/python" -m compileall -q -j 0 --invalidation-mode checked-hash -s "$DESTDIR" -p "" "$venv/lib" >/dev/null

launcher() {  # name module func
    mkdir -p "$DESTDIR$BIN"
    printf '#!/bin/sh\n# -P: never import from the current directory (it may be an untrusted clone)\nexec %s/venv/bin/python -P -c '\''from %s import %s; %s()'\'' "$@"\n' "$OPT" "$2" "$3" "$3" > "$DESTDIR$BIN/$1"
    chmod 755 "$DESTDIR$BIN/$1"
}
launcher repohub repohub.cli run
launcher repohub-web repohub.web.app main
launcher repohub-tui repohub.tui.app main
launcher repohub-mcp repohub.mcp main

id=io.github.DaRipper91.RepoHub
install -Dm644 "$here/$id.desktop" "$DESTDIR/usr/share/applications/$id.desktop"
install -Dm644 "$here/$id.Tui.desktop" "$DESTDIR/usr/share/applications/$id.Tui.desktop"
install -Dm644 "$here/$id.svg" "$DESTDIR/usr/share/icons/hicolor/scalable/apps/$id.svg"
mkdir -p "$DESTDIR/usr/share/metainfo"
sed -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$DATE/" "$here/$id.metainfo.xml" > "$DESTDIR/usr/share/metainfo/$id.metainfo.xml"
chmod 644 "$DESTDIR/usr/share/metainfo/$id.metainfo.xml"
install -Dm644 "$here/repohub-web.service" "$DESTDIR/usr/lib/systemd/user/repohub-web.service"
install -Dm644 "$repo/README.md" "$DESTDIR/usr/share/doc/repohub/README.md"
install -Dm644 "$repo/SECURITY.md" "$DESTDIR/usr/share/doc/repohub/SECURITY.md"
install -Dm644 "$repo/LICENSE" "$DESTDIR/usr/share/licenses/repohub/LICENSE"
echo "staged repohub $VERSION into $DESTDIR (python: $("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])'))"
