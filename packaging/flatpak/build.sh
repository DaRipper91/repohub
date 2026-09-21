#!/bin/sh
# Build the Flatpak bundle (needs flatpak-builder: `flatpak install flathub org.flatpak.Builder`, and the SDK).
#   packaging/flatpak/build.sh
# Result: packaging/dist/RepoHub-<version>-<arch>.flatpak. Needs network (dependency wheels, git source).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$repo/pyproject.toml" | head -1)
ARCH=$(uname -m)
# under $HOME: the Flatpak Builder runs in a sandbox that cannot see /tmp
work=${WORK:-$HOME/.cache/repohub-packaging/flatpak}
rm -rf "$work"
mkdir -p "$work" "$repo/packaging/dist"

echo "== wheel"
python3 -m venv "$work/venv"
"$work/venv/bin/python" -m pip wheel --no-deps --disable-pip-version-check -q -w "$work" "$repo"

echo "== dependency wheels"
python3 "$here/gen-sources.py" "$work/python-deps.json" --python 3.13
GIT_SHA=$(curl -fsSL https://www.kernel.org/pub/software/scm/git/git-2.55.0.tar.xz | sha256sum | cut -d' ' -f1)
# Layout: $work/common (the shared assets) and $work/m (manifest, wheels), so the manifest's ../common resolves.
cp -r "$repo/packaging/common" "$work/common"
sed -i -e "s/@VERSION@/$VERSION/" -e "s/@DATE@/$(date -u +%Y-%m-%d)/" "$work/common/io.github.DaRipper91.RepoHub.metainfo.xml"
mkdir -p "$work/m"
mv "$work/python-deps.json" "$work"/repohub-*.whl "$work/m/"
sed -e "s/@VERSION@/$VERSION/g" -e "s/@DATE@/$(date -u +%Y-%m-%d)/g" -e "s/@GIT_SHA256@/$GIT_SHA/" \
    "$here/io.github.DaRipper91.RepoHub.yml.in" > "$work/m/io.github.DaRipper91.RepoHub.yml"

echo "== flatpak-builder"
BUILDER="flatpak run org.flatpak.Builder"
command -v flatpak-builder >/dev/null 2>&1 && BUILDER=flatpak-builder
STATE=${STATE:-$HOME/.cache/repohub-packaging/flatpak-state}  # keeps built modules (git) between runs
$BUILDER --user --force-clean --state-dir="$STATE" --install-deps-from=flathub --disable-rofiles-fuse --repo="$work/repo" "$work/build" "$work/m/io.github.DaRipper91.RepoHub.yml"
flatpak build-bundle "$work/repo" "$repo/packaging/dist/RepoHub-$VERSION-$ARCH.flatpak" io.github.DaRipper91.RepoHub
ls -la "$repo/packaging/dist/RepoHub-$VERSION-$ARCH.flatpak"
