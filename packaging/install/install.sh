#!/bin/sh
# Install RepoHub for the current user (no root). Run it from a RepoHub checkout or an unpacked source archive.
#
#   packaging/install/install.sh [--service] [--no-desktop] [--from SOURCE] [--prefix DIR] [--dry-run]
#   packaging/install/install.sh --uninstall [--purge]
#
#   --from SOURCE   what to install: a checkout (default: this one), a wheel file, or any pip requirement
#   --service       also install and start a systemd user service that keeps the web app running
#   --no-desktop    skip the desktop entries and icon
#   --prefix DIR    install under DIR (default ~/.local): DIR/bin, DIR/share/repohub-app, DIR/share/applications, ...
#   --uninstall     remove everything this script installed; your favorites, notes and settings are kept
#   --purge         with --uninstall: also delete RepoHub's data and config folders
#   --dry-run       print what would happen and change nothing
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
common="$repo/packaging/common"
prefix=${HOME}/.local
source_spec=$repo
service=0; desktop=1; uninstall=0; purge=0; dry=0

while [ $# -gt 0 ]; do
    case "$1" in
        --service) service=1 ;;
        --no-desktop) desktop=0 ;;
        --from) source_spec=${2:?--from needs a value}; shift ;;
        --prefix) prefix=${2:?--prefix needs a folder}; shift ;;
        --uninstall) uninstall=1 ;;
        --purge) purge=1 ;;
        --dry-run) dry=1 ;;
        -h|--help) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done

bin=$prefix/bin
share=$prefix/share
app=$share/repohub-app  # NOT $share/repohub: that is where RepoHub keeps your favorites and settings
venv=$app/venv
apps=$share/applications
icons=$share/icons/hicolor/scalable/apps
unitdir=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
id=io.github.DaRipper91.RepoHub

say() { printf '%s\n' "$*"; }
run() { if [ "$dry" = 1 ]; then say "  would run: $*"; else "$@"; fi; }
have_systemd_user() { command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; }

if [ "$uninstall" = 1 ]; then
    say "Removing RepoHub from $prefix"
    if have_systemd_user && [ -f "$unitdir/repohub-web.service" ]; then
        run systemctl --user disable --now repohub-web.service || true
    fi
    for f in "$unitdir/repohub-web.service" "$bin/repohub" "$bin/repohub-web" "$bin/repohub-tui" "$bin/repohub-mcp" \
             "$apps/$id.desktop" "$apps/$id.Tui.desktop" "$icons/$id.svg"; do
        [ -e "$f" ] || [ -L "$f" ] && run rm -f "$f"
    done
    [ -d "$app" ] && run rm -rf "$app"
    have_systemd_user && run systemctl --user daemon-reload || true
    if [ "$purge" = 1 ]; then
        for d in "${XDG_DATA_HOME:-$HOME/.local/share}/repohub" "${XDG_CONFIG_HOME:-$HOME/.config}/repohub" "${XDG_CACHE_HOME:-$HOME/.cache}/repohub"; do
            [ -d "$d" ] && run rm -rf "$d"
        done
        say "Your RepoHub data and settings were deleted."
    else
        say "Your favorites, notes and settings were kept (use --purge to delete them)."
    fi
    exit 0
fi

# ---- find a Python new enough (3.11 or later)
py=
for c in python3.12 python3.13 python3.11 python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
        py=$(command -v "$c"); break
    fi
done
[ -n "$py" ] || { echo "RepoHub needs Python 3.11 or newer, and none was found." >&2; exit 1; }
"$py" -c 'import venv' 2>/dev/null || { echo "Python's venv module is missing (Debian/Ubuntu: apt install python3-venv)." >&2; exit 1; }
say "Installing RepoHub with $("$py" --version 2>&1) into $app"

run mkdir -p "$bin" "$app"
run "$py" -m venv "$venv"
run "$venv/bin/python" -m pip install --disable-pip-version-check -q --upgrade "$source_spec"
for tool in repohub repohub-web repohub-tui repohub-mcp; do
    run ln -sf "$venv/bin/$tool" "$bin/$tool"
done

if [ "$desktop" = 1 ]; then
    if [ -d "$common" ]; then
        run mkdir -p "$apps" "$icons"
        for d in "$id.desktop" "$id.Tui.desktop"; do  # absolute Exec paths: a desktop session may not have ~/.local/bin on PATH
            if [ "$dry" = 1 ]; then say "  would install $apps/$d"; else
                sed "s|^Exec=repohub|Exec=$bin/repohub|" "$common/$d" > "$apps/$d"
            fi
        done
        run cp "$common/$id.svg" "$icons/$id.svg"
        command -v update-desktop-database >/dev/null 2>&1 && run update-desktop-database "$apps" >/dev/null 2>&1 || true
    else
        say "Desktop entries skipped: $common not found (run this from a checkout)."
    fi
fi

if [ "$service" = 1 ]; then
    if have_systemd_user; then
        run mkdir -p "$unitdir"
        if [ "$dry" = 1 ]; then say "  would install $unitdir/repohub-web.service"; else
            sed "s|^ExecStart=/usr/bin/repohub-web|ExecStart=$bin/repohub-web|" "$common/repohub-web.service" > "$unitdir/repohub-web.service"
        fi
        run systemctl --user daemon-reload
        run systemctl --user enable --now repohub-web.service
        [ "$dry" = 1 ] || say "The web app now runs in the background at http://127.0.0.1:8765/ (systemctl --user status repohub-web)."
    else
        say "No systemd user session found: the service was not installed."
    fi
fi

case ":$PATH:" in *":$bin:"*) ;; *) say "Note: $bin is not on your PATH; add it to run 'repohub' from anywhere." ;; esac
say "Done. Try: repohub --version   |   repohub-tui   |   repohub-web --open"
[ "$dry" = 1 ] && say "(dry run: nothing was changed)"
exit 0
