"""The packaging files: they cannot be built in a normal test run, but they can be checked for drift and safety."""
import importlib.util
import os
import re
import shutil
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from platformdirs import user_data_dir

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "packaging"
COMMON = PKG / "common"
APP_ID = "io.github.DaRipper91.RepoHub"
SCRIPTS = sorted(list(PKG.rglob("*.sh")) + [PKG / "appimage" / "AppRun"])


def scripts_from_pyproject() -> dict[str, tuple[str, str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
    return {name: tuple(target.split(":")) for name, target in data.items()}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_shell_scripts_parse_and_are_executable(script):
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    assert os.access(script, os.X_OK), f"{script} is not executable"


def test_python_generator_compiles():
    compile((PKG / "flatpak" / "gen-sources.py").read_text(), "gen-sources.py", "exec")


# ---------------------------------------------------------------- no drift between the entry points and the launchers

def test_launchers_match_pyproject_entry_points():
    expected = scripts_from_pyproject()
    stage = (COMMON / "stage.sh").read_text()
    found = {m.group(1): (m.group(2), m.group(3)) for m in re.finditer(r"^launcher (\S+) (\S+) (\S+)$", stage, re.M)}
    assert found == expected


def test_flatpak_and_appimage_dispatch_the_same_targets():
    expected = scripts_from_pyproject()
    fp = (PKG / "flatpak" / f"{APP_ID}.yml.in").read_text()
    for name, (mod, fn) in expected.items():
        assert f"{name}:{mod}:{fn}" in fp
    apprun = (PKG / "appimage" / "AppRun").read_text()
    for name, (mod, fn) in expected.items():
        assert f"from {mod} import {fn}" in apprun, name


def test_every_entry_point_target_exists():
    for name, (mod, fn) in scripts_from_pyproject().items():
        spec = importlib.util.find_spec(mod)
        assert spec is not None and fn in Path(spec.origin).read_text(), (name, mod, fn)


# ---------------------------------------------------------------- metadata

def test_desktop_files_reference_real_commands_and_the_icon():
    commands = set(scripts_from_pyproject())
    for f in COMMON.glob("*.desktop"):
        text = f.read_text()
        assert f"Icon={APP_ID}" in text and text.startswith("[Desktop Entry]")
        assert re.search(r"^Exec=(\S+)", text, re.M).group(1) in commands
        assert f.name.startswith(APP_ID)
    assert (COMMON / f"{APP_ID}.svg").is_file()


@pytest.mark.skipif(shutil.which("desktop-file-validate") is None, reason="desktop-file-utils not installed")
def test_desktop_files_validate():
    for f in COMMON.glob("*.desktop"):
        r = subprocess.run(["desktop-file-validate", str(f)], capture_output=True, text=True)
        assert r.returncode == 0 and "error" not in r.stdout.lower(), r.stdout


def test_icon_is_wellformed_svg():
    root = ET.parse(COMMON / f"{APP_ID}.svg").getroot()
    assert root.tag.endswith("svg") and root.attrib["viewBox"] == "0 0 256 256"
    assert "<script" not in (COMMON / f"{APP_ID}.svg").read_text().lower()


def test_metainfo_is_wellformed_and_matches_the_desktop_file():
    root = ET.parse(COMMON / f"{APP_ID}.metainfo.xml").getroot()
    assert root.findtext("id") == APP_ID and root.findtext("project_license") == "MIT"
    assert root.find("launchable").text == f"{APP_ID}.desktop"
    assert {b.text for b in root.find("provides")} == set(scripts_from_pyproject())
    assert (COMMON / f"{APP_ID}.desktop").is_file()
    release = root.find("releases/release")
    assert release.attrib["version"] == "@VERSION@" and release.attrib["date"] == "@DATE@"


def test_version_placeholders_are_filled_by_the_build_scripts():
    for f in (PKG / "rpm" / "repohub.spec", PKG / "arch" / "PKGBUILD"):
        assert "@VERSION@" in f.read_text()
    for f in (PKG / "rpm" / "build.sh", PKG / "arch" / "build.sh", COMMON / "stage.sh"):
        assert "VERSION" in f.read_text() and "pyproject.toml" in f.read_text()


def test_systemd_unit_is_loopback_and_hardened():
    unit = (COMMON / "repohub-web.service").read_text()
    assert "ExecStart=/usr/bin/repohub-web --port 8765" in unit and "WantedBy=default.target" in unit
    assert "NoNewPrivileges=yes" in unit and "--host" not in unit  # never binds beyond loopback
    assert "Restart=on-failure" in unit


def test_packages_depend_on_the_exact_python_they_bundle():
    assert "Requires:       /usr/bin/python%{pyver}" in (PKG / "rpm" / "repohub.spec").read_text()
    assert "Depends: python$PYVER" in (PKG / "deb" / "build.sh").read_text()
    assert 'depends=("python>=$_pyver" "python<$_pynext"' in (PKG / "arch" / "PKGBUILD").read_text()


def test_stage_script_uses_final_paths_and_only_wheels():
    stage = (COMMON / "stage.sh").read_text()
    assert "--only-binary=:all:" in stage and 'exec %s/venv/bin/python' in stage
    assert "-s \"$DESTDIR\" -p \"\"" in stage  # byte-code records the final path, not the staging directory


# ---------------------------------------------------------------- flatpak wheel picker

def load_generator():
    spec = importlib.util.spec_from_file_location("gen_sources", PKG / "flatpak" / "gen-sources.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def f(name):
    return {"filename": name, "url": "https://x/" + name, "digests": {"sha256": "0" * 64}}


def test_picker_prefers_pure_wheels_and_matches_arch_and_python():
    g = load_generator()
    pure = [f("a-1-py3-none-any.whl"), f("a-1-cp313-cp313-manylinux_2_28_x86_64.whl")]
    assert g.pick(pure, "3.13", None)["filename"] == "a-1-py3-none-any.whl"
    binary = [f("b-1-cp312-cp312-manylinux_2_28_x86_64.whl"), f("b-1-cp313-cp313-manylinux_2_28_x86_64.whl"),
              f("b-1-cp313-cp313-manylinux_2_28_aarch64.whl"), f("b-1-cp313-cp313-musllinux_1_2_x86_64.whl"),
              f("b-1-cp313-cp313-win_amd64.whl"), f("b-1-cp38-abi3-manylinux_2_17_aarch64.whl")]
    assert g.pick(binary, "3.13", None) is None
    assert g.pick(binary, "3.13", "x86_64")["filename"] == "b-1-cp313-cp313-manylinux_2_28_x86_64.whl"
    assert g.pick(binary, "3.13", "aarch64")["filename"].endswith("manylinux_2_28_aarch64.whl")
    assert g.pick([f("c-1-cp312-cp312-manylinux_2_28_x86_64.whl")], "3.13", "x86_64") is None


def test_picker_never_chooses_free_threaded_wheels():
    g = load_generator()
    only_t = [f("m-1-cp313-cp313t-manylinux2014_aarch64.whl")]
    assert g.pick(only_t, "3.13", "aarch64") is None
    both = only_t + [f("m-1-cp313-cp313-manylinux2014_aarch64.whl")]
    assert g.pick(both, "3.13", "aarch64")["filename"] == "m-1-cp313-cp313-manylinux2014_aarch64.whl"


def test_flatpak_manifest_is_offline_for_python_and_pins_git():
    text = (PKG / "flatpak" / f"{APP_ID}.yml.in").read_text()
    assert "python-deps.json" in text and "--no-index" in text and "NO_RUST=1" in text
    # the git source is pinned by a real sha256 (equal to kernel.org's published sums), not computed at build time
    assert "sha256: 457fdb04dc8728e007d4688695e6912e6f680727920f2a40bf11eacc17505357" in text and "@GIT_SHA256@" not in text
    assert "git-2.55.0.tar.xz" in text and "--share=network" in text


def test_flatpak_does_not_expose_the_home_folder():
    text = (PKG / "flatpak" / f"{APP_ID}.yml.in").read_text()
    grants = re.findall(r"^\s*- --filesystem=(\S+)", text, re.M)
    assert grants == ["~/playground:create"], grants  # only the clone folder; nothing like home, host or ~/.ssh
    assert "--filesystem=home" not in text and "--filesystem=host" not in text


# ---------------------------------------------------------------- the install script must never touch user data

def run_install(home: Path, *args, env_extra=None):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"), **(env_extra or {})}
    return subprocess.run(["sh", str(PKG / "install" / "install.sh"), *args], capture_output=True, text=True, env=env, timeout=60)


def fake_install(home: Path):
    """What a finished install leaves behind, without running pip."""
    (home / ".local/share/repohub-app/venv/bin").mkdir(parents=True)
    (home / ".local/bin").mkdir(parents=True)
    for tool in ("repohub", "repohub-web", "repohub-tui", "repohub-mcp"):
        (home / ".local/bin" / tool).symlink_to(home / ".local/share/repohub-app/venv/bin" / tool)
    (home / ".local/share/applications").mkdir(parents=True)
    (home / ".local/share/applications" / f"{APP_ID}.desktop").write_text("x")
    data = home / ".local/share/repohub"
    data.mkdir(parents=True)
    (data / "repohub.db").write_text("favorites")
    (home / ".config/repohub").mkdir(parents=True)
    (home / ".config/repohub/hosts.yaml").write_text("yaml")


def test_the_app_folder_is_not_the_data_folder(tmp_path):
    r = run_install(tmp_path, "--dry-run")
    assert r.returncode == 0, r.stderr
    app_dir = tmp_path / ".local/share/repohub-app"
    data_dir = Path(user_data_dir("repohub", appauthor=False))
    assert str(app_dir) in r.stdout and app_dir != data_dir
    assert str(tmp_path / ".local/share/repohub/") not in r.stdout.replace(str(app_dir), "")


def test_uninstall_keeps_favorites_and_settings(tmp_path):
    fake_install(tmp_path)
    r = run_install(tmp_path, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local/share/repohub-app").exists() and not (tmp_path / ".local/bin/repohub").is_symlink()
    assert not (tmp_path / ".local/share/applications" / f"{APP_ID}.desktop").exists()
    assert (tmp_path / ".local/share/repohub/repohub.db").read_text() == "favorites"  # the bug this test guards against
    assert (tmp_path / ".config/repohub/hosts.yaml").read_text() == "yaml"
    assert "kept" in r.stdout


def test_purge_deletes_only_repohubs_own_folders(tmp_path):
    fake_install(tmp_path)
    (tmp_path / ".local/share/other-app").mkdir()
    (tmp_path / ".local/share/other-app/keep.txt").write_text("k")
    r = run_install(tmp_path, "--uninstall", "--purge")
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / ".local/share/repohub").exists() and not (tmp_path / ".config/repohub").exists()
    assert (tmp_path / ".local/share/other-app/keep.txt").read_text() == "k"


def test_uninstall_on_a_clean_system_is_harmless(tmp_path):
    assert run_install(tmp_path, "--uninstall").returncode == 0


def test_dry_run_changes_nothing_and_bad_options_are_refused(tmp_path):
    r = run_install(tmp_path, "--dry-run", "--service")
    assert r.returncode == 0 and "dry run: nothing was changed" in r.stdout and list(tmp_path.iterdir()) == []
    bad = run_install(tmp_path, "--frobnicate")
    assert bad.returncode == 2 and "unknown option" in bad.stderr


# ---------------------------------------------------------------- review follow-ups

def test_every_launcher_ignores_the_current_directory():
    """`python -c` puts the cwd first on sys.path: a cloned repository could shadow a real module. -P prevents it."""
    stage = (COMMON / "stage.sh").read_text()
    assert "venv/bin/python -P -c" in stage
    assert "exec python3 -P -c" in (PKG / "flatpak" / f"{APP_ID}.yml.in").read_text()
    apprun = (PKG / "appimage" / "AppRun").read_text()
    assert apprun.count('exec "$PY" -P -c') == 4 and 'exec "$PY" -c' not in apprun


def test_the_launcher_command_really_ignores_a_shadowing_module(tmp_path):
    import sys

    (tmp_path / "json.py").write_text("print('PWNED via cwd')\nraise SystemExit(0)\n")
    code = "import json; print(json.__file__)"
    safe = subprocess.run([sys.executable, "-P", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert "PWNED" not in safe.stdout and str(tmp_path) not in safe.stdout and safe.returncode == 0
    unsafe = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert "PWNED" in unsafe.stdout  # without -P the attack works, so this test would notice a regression


def test_appimage_does_not_put_its_python_on_path():
    apprun = (PKG / "appimage" / "AppRun").read_text()
    assert not re.search(r'^\s*export PATH=', apprun, re.M)


def test_appimage_downloads_are_pinned_and_verified():
    pins = (PKG / "appimage" / "pins.sh").read_text()
    for key in ("PBS_SHA256_aarch64", "PBS_SHA256_x86_64", "APPIMAGETOOL_SHA256_aarch64", "APPIMAGETOOL_SHA256_x86_64"):
        assert re.search(rf"^{key}=[0-9a-f]{{64}}\b", pins, re.M), key
    assert re.search(r"^PBS_TAG=\d{8}", pins, re.M) and re.search(r"^APPIMAGETOOL_VERSION=\d", pins, re.M)
    build = (PKG / "appimage" / "build.sh").read_text()
    assert "sha256sum -c" in build and build.count("checksum mismatch") == 2
    assert "continuous" not in build and "releases/latest" not in build


def test_dependencies_are_hash_locked_and_installed_with_require_hashes():
    lock = (PKG / "requirements.lock").read_text()
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==", lock, re.M)
    assert len(pins) >= 25
    blocks = re.split(r"^(?=[A-Za-z0-9_.-]+==)", lock, flags=re.M)[1:]
    assert all("--hash=sha256:" in b for b in blocks), "every pinned package needs hashes"
    for script in (COMMON / "stage.sh", PKG / "appimage" / "build.sh"):
        assert "--require-hashes" in script.read_text() and "requirements.lock" in script.read_text()


def test_flatpak_dependency_list_is_committed_and_agrees_with_the_lock():
    import json

    deps = json.loads((PKG / "flatpak" / "python-deps.json").read_text())
    lock = (PKG / "requirements.lock").read_text().lower().replace("_", "-")
    names = set()
    for src in deps["sources"]:
        base = src["url"].rsplit("/", 1)[1].split("-")
        name, version = base[0].lower().replace("_", "-"), base[1]
        names.add(name)
        assert f"{name}=={version}" in lock, f"{name} {version} is not in requirements.lock"
        assert re.fullmatch(r"[0-9a-f]{64}", src["sha256"])
    assert len(names) >= 25
    assert "REGEN" in (PKG / "flatpak" / "build.sh").read_text()


def test_flatpak_build_refuses_to_delete_outside_its_cache():
    text = (PKG / "flatpak" / "build.sh").read_text()
    assert 'case "$work" in "$HOME"/.cache/repohub-packaging/*)' in text and "refusing to delete" in text


def test_workflow_has_least_privilege_permissions():
    assert re.search(r"^permissions:\s*\n\s+contents: read", (ROOT / ".github/workflows/packages.yml").read_text(), re.M)


def test_systemd_unit_has_the_hardening_it_claims():
    unit = (COMMON / "repohub-web.service").read_text()
    for line in ("NoNewPrivileges=yes", "PrivateTmp=yes", "LockPersonality=yes", "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
                 "SystemCallArchitectures=native"):
        assert line in unit


# ---- install.sh: only remove what it installed, refuse unsafe input

def test_uninstall_keeps_links_and_units_it_did_not_create(tmp_path):
    fake_install(tmp_path)
    foreign = tmp_path / "pipx-repohub"
    foreign.write_text("#!/bin/sh")
    link = tmp_path / ".local/bin/repohub"
    link.unlink()
    link.symlink_to(foreign)  # e.g. a pipx or editable install that owns this name
    unit_dir = tmp_path / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "repohub-web.service").write_text("[Service]\nExecStart=/somewhere/else/repohub-web\n")
    r = run_install(tmp_path, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert link.is_symlink() and link.resolve() == foreign.resolve() and "not installed by this script" in r.stdout
    assert (unit_dir / "repohub-web.service").exists()  # not ours: ExecStart points elsewhere
    assert not (tmp_path / ".local/bin/repohub-web").is_symlink()  # ours: removed


@pytest.mark.parametrize("prefix", ["relative/dir", "/tmp/with space", "/tmp/a|b", "/tmp/a&b", "/tmp/a$b", "/tmp/a%b", "/", ""])
def test_unsafe_prefixes_are_refused(tmp_path, prefix):
    r = run_install(tmp_path, "--dry-run", "--prefix", prefix)
    assert r.returncode != 0 and list(tmp_path.iterdir()) == []


def test_from_cannot_be_an_option_for_pip(tmp_path):
    r = run_install(tmp_path, "--dry-run", "--from", "--index-url=https://evil.example/simple")
    assert r.returncode == 2 and "not an option" in r.stderr


def test_a_plain_prefix_is_accepted_and_used(tmp_path):
    r = run_install(tmp_path, "--dry-run", "--prefix", str(tmp_path / "opt-home"))
    assert r.returncode == 0 and str(tmp_path / "opt-home" / "share" / "repohub-app") in r.stdout


def test_purge_dry_run_does_not_claim_data_was_deleted(tmp_path):
    fake_install(tmp_path)
    r = run_install(tmp_path, "--uninstall", "--purge", "--dry-run")
    assert "would be deleted" in r.stdout and "were deleted" not in r.stdout
    assert (tmp_path / ".local/share/repohub/repohub.db").exists()
