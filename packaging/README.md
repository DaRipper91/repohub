# Packaging RepoHub for Linux

Everything here builds from the source tree and reuses one staging script (`common/stage.sh`), so the packages
behave the same way. All builds were tested on **aarch64** (Fedora 44 Asahi host, clean containers for the rest);
the scripts are architecture-neutral, so an x86-64 machine builds x86-64 packages the same way (see
`.github/workflows/packages.yml`).

| Format | How it is built | Tested in | Result |
|---|---|---|---|
| **RPM** (Fedora, RHEL family) | `rpm/build-in-container.sh [fedora] [pyver]` | clean Fedora 44 container: install, run as a normal user, remove | `dist/repohub-<v>-1.fc44.<arch>.rpm` |
| **DEB** (Debian, Ubuntu) | `deb/build-in-container.sh <image> <tag>` | Debian 13 and Ubuntu 24.04 containers | `dist/repohub_<v>-1~deb13_<arch>.deb` |
| **Arch** | `arch/build.sh` (PKGBUILD + makepkg in a container) | Arch Linux ARM container: `pacman -U`, run, remove | `dist/repohub-<v>-1-<arch>.pkg.tar.xz` |
| **AppImage** | `appimage/build-in-container.sh` | run natively (FUSE) and with `--appimage-extract-and-run` | `dist/RepoHub-<v>-<arch>.AppImage` |
| **Flatpak** | `flatpak/build.sh` (needs `flatpak-builder` and the SDK) | see "Flatpak" below | `dist/RepoHub-<v>-<arch>.flatpak` |
| **User install script** | `install/install.sh` (no root) | real install, upgrade, uninstall, purge in a throwaway home | none: installs from a checkout |

Build outputs go to `packaging/dist/` (git-ignored). Builds need network access: dependency wheels come from PyPI.

## How the bundled packages work

RPM, DEB and Arch packages carry their own Python environment in `/opt/repohub/venv` (RepoHub plus its
dependencies, all from binary wheels, no compiler needed) on top of the distribution's own Python, and put four
small launchers in `/usr/bin`: `repohub`, `repohub-web`, `repohub-tui`, `repohub-mcp`. They also install a desktop
entry for the web app and one for the terminal app, an icon, AppStream metadata, and a systemd **user** unit
(`repohub-web.service`, off until you enable it: `systemctl --user enable --now repohub-web`).

The environment's interpreter is a symlink to `/usr/bin/python3.X`, so each package depends on that exact
version and is built per distribution release (a Debian 13 package is not an Ubuntu 24.04 package). That is why
the build scripts take the distribution as an argument. Distribution policy purists would rather package every
dependency separately; this trades that for packages that work today on any release with a recent Python.

## The install script (no root)

```
packaging/install/install.sh [--service] [--no-desktop] [--from SOURCE] [--prefix DIR] [--dry-run]
packaging/install/install.sh --uninstall [--purge]
```

Installs into `~/.local` (or `--prefix`): a virtual environment in `share/repohub-app`, symlinks in `bin`, desktop
entries and the icon, and with `--service` a running systemd user service. **The app folder is deliberately not
`share/repohub`**, which is where RepoHub keeps your favorites and settings; uninstalling removes the app and
keeps your data unless you add `--purge` (tests guard this).

## AppImage

Bundles its own Python (python-build-standalone), so it needs nothing from the host. Modes: run it as is for the
web app (opens your browser), or `RepoHub.AppImage tui|cli|mcp|web [args]`; symlinks named `repohub`,
`repohub-tui`, `repohub-mcp` or `repohub-web` pick that mode.

## Flatpak

The sandbox is real, so some things work differently. It is built and installed for testing, but treat it as the
most limited format:

- **Network** is allowed. **Home is read-only**, with `~/playground` writable (the clone folder), so scanning for
  clones and reading `.git/config` work; guided install can only use tools inside the sandbox (Python and git), not
  your system's compilers.
- **Sign-in:** the `gh` CLI is not in the sandbox. Give RepoHub a token with
  `flatpak override --user --env=GITHUB_TOKEN="$(gh auth token)" io.github.DaRipper91.RepoHub`
  (this stores the token in Flatpak's override file).
- git is bundled (the runtime has none). Run the terminal app with
  `flatpak run --command=repohub-tui io.github.DaRipper91.RepoHub`, the CLI with `--command=repohub`.
- Flathub's rules (no network at build time, and so on) are not met yet; this is a local-build manifest.

Building needs `flatpak install flathub org.flatpak.Builder org.freedesktop.Sdk//25.08` (about 2 GB).

## Not done

- x86-64 builds were not run here (no x86-64 machine); the scripts and the workflow are architecture-neutral.
- RPM built for Fedora only; other RPM distributions need their own Python-version argument and a matching container.
- No package signing, repositories or automatic updates.
