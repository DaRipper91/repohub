#!/bin/sh
# Regenerate the pinned, hash-locked dependency lists used by every package build. Review the diff before committing.
#   packaging/lock.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
cd "$repo"
uv pip compile pyproject.toml --universal --python-version 3.11 --generate-hashes --no-header -q -o packaging/requirements.lock
python3 packaging/flatpak/gen-sources.py packaging/flatpak/python-deps.json --python 3.13
echo "updated packaging/requirements.lock and packaging/flatpak/python-deps.json"
