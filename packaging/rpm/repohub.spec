# RepoHub, bundled build: the application and its Python dependencies live in /opt/repohub/venv, on top of the
# distribution's own Python (which must be the exact version the package was built with).
# Building needs network access (to fetch dependency wheels), so build outside mock or use `mock --enable-network`.
%global pyver      %{?repohub_pyver}%{!?repohub_pyver:3.12}
%global debug_package %{nil}
%global __brp_mangle_shebangs %{nil}
%global __brp_python_bytecompile %{nil}
%global __os_install_post %{nil}
%global _binary_payload w19.zstdio

Name:           repohub
Version:        @VERSION@
Release:        1%{?dist}
Summary:        Browse GitHub, GitLab and Codeberg like an app store
License:        MIT
URL:            https://github.com/DaRipper91/repohub
Source0:        repohub-%{version}.tar.gz
BuildArch:      %{_arch}
BuildRequires:  python%{pyver}
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
# The venv's interpreter is a symlink to this exact file, so depend on it explicitly.
Requires:       /usr/bin/python%{pyver}
Requires:       git
Recommends:     gh
AutoReqProv:    no

%description
RepoHub is a local app for finding and trying repositories. It searches GitHub, GitLab, Codeberg and any Forgejo
server at once, keeps favorites with tags and notes, recommends projects, shows which repositories you already
cloned and whether one can run on your machine, and clones safely. It runs as a web app (repohub-web), a terminal
app (repohub-tui) and a read-only command line (repohub), and has a read-only MCP server (repohub-mcp) for
Claude Code. Light and dark themes are included.

%prep
%autosetup -n repohub-%{version}

%build
# nothing: the staging script builds the wheel and the virtual environment

%install
PYTHON=python%{pyver} sh packaging/common/stage.sh %{buildroot}

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/*.desktop
appstream-util validate-relax --nonet %{buildroot}%{_datadir}/metainfo/*.metainfo.xml || :

%files
%license /usr/share/licenses/repohub/LICENSE
%doc /usr/share/doc/repohub/README.md
%doc /usr/share/doc/repohub/SECURITY.md
/opt/repohub
%{_bindir}/repohub
%{_bindir}/repohub-web
%{_bindir}/repohub-tui
%{_bindir}/repohub-mcp
%{_datadir}/applications/io.github.DaRipper91.RepoHub.desktop
%{_datadir}/applications/io.github.DaRipper91.RepoHub.Tui.desktop
%{_datadir}/icons/hicolor/scalable/apps/io.github.DaRipper91.RepoHub.svg
%{_datadir}/metainfo/io.github.DaRipper91.RepoHub.metainfo.xml
/usr/lib/systemd/user/repohub-web.service

%changelog
* Tue Sep 22 2026 DaRipper91 <theripper81791@gmail.com> - @VERSION@-1
- Bundled build of RepoHub @VERSION@
