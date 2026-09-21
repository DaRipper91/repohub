import os
import re
import threading
from pathlib import Path

import pytest

from repohub.core import hosts
from repohub.core.hostsconfig import (
    MAX_HOSTS_BYTES,
    LoadedHosts,
    configure_hosts,
    load_hosts,
    personal_hosts_path,
)

BUILTIN_IDS = ("github", "gitlab", "codeberg")
RAW_CONTROL = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f‪-‮⁦-⁩‎‏]")


def write(tmp_path: Path, text: str, name: str = "hosts.yaml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def entry(**kw) -> str:
    base = {"id": "myforge", "kind": "forgejo", "url": "https://git.example.org"}
    base.update(kw)
    out = []
    first = True
    for k, v in base.items():
        if v is None:
            continue
        out.append(("- " if first else "  ") + f"{k}: " + _q(v))
        first = False
    return "\n".join(out) + "\n"


def _q(v: object) -> str:
    import json
    return json.dumps(v)  # JSON strings are valid YAML double-quoted scalars


def load(tmp_path, text):
    return load_hosts(write(tmp_path, text))


def assert_builtins_only(loaded: LoadedHosts):
    assert loaded.registry.ids == BUILTIN_IDS


def assert_clean(problems):
    for p in problems:
        assert not RAW_CONTROL.search(p), repr(p)


def test_personal_hosts_path_honours_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert personal_hosts_path() == tmp_path / "repohub" / "hosts.yaml"


def test_missing_file_gives_builtins_silently(tmp_path):
    loaded = load_hosts(tmp_path / "nope.yaml")
    assert_builtins_only(loaded)
    assert loaded.problems == []


def test_empty_and_null_files_mean_none(tmp_path):
    for text in ("", "null\n", "~\n", "# only a comment\n"):
        loaded = load(tmp_path, text)
        assert_builtins_only(loaded)
        assert loaded.problems == []


def test_valid_file_appends_hosts_in_order(tmp_path):
    text = (entry(id="myforge", url="https://Git.Example.ORG/", name="My Forge")
            + entry(id="other-one", url="https://code.example.net", token_env="REPOHUB_OTHER_FORGE_TOKEN"))
    loaded = load(tmp_path, text)
    assert loaded.problems == []
    assert loaded.registry.ids == BUILTIN_IDS + ("myforge", "other-one")
    a = loaded.registry.get("myforge")
    assert (a.kind, a.name, a.domain, a.api_base) == (
        "forgejo", "My Forge", "git.example.org", "https://git.example.org/api/v1")
    assert a.token_env == ("REPOHUB_MYFORGE_TOKEN",)
    assert a.builtin is False
    b = loaded.registry.get("other-one")
    assert b.name == "other-one"  # default name is the id
    assert b.token_env == ("REPOHUB_OTHER_FORGE_TOKEN",)
    assert b.domain == "code.example.net"
    assert loaded.registry.get("github").builtin is True


def test_default_token_replaces_hyphen(tmp_path):
    loaded = load(tmp_path, entry(id="my-forge"))
    assert loaded.registry.get("my-forge").token_env == ("REPOHUB_MY_FORGE_TOKEN",)


def test_name_is_cleaned_and_capped(tmp_path):
    loaded = load(tmp_path, entry(name="Nice\x1b[31m‮Name" + "x" * 100))
    name = loaded.registry.get("myforge").name
    assert len(name) <= 40
    assert not RAW_CONTROL.search(name)
    assert name.startswith("Nice[31mName")


def test_load_does_not_change_active_registry_but_configure_does(tmp_path):
    p = write(tmp_path, entry(id="myforge") + "- 5\n")
    before = hosts.registry()
    loaded = load_hosts(p)
    assert hosts.registry() is before
    assert "myforge" in loaded.registry
    problems = configure_hosts(p)
    assert "myforge" in hosts.registry()
    assert len(problems) == 1 and problems[0].startswith("hosts file entry #1:")


def test_configure_hosts_with_broken_file_keeps_builtins(tmp_path):
    problems = configure_hosts(write(tmp_path, "{a: 1}\n"))
    assert hosts.registry().ids == BUILTIN_IDS
    assert len(problems) == 1


@pytest.mark.parametrize("bad_id", ["Bad", "1x", "a" * 21, "", "has_underscore", "sp ace", "github",
                                    "gitlab", "codeberg", "aa\n", "aa\u202e"])
def test_bad_ids(tmp_path, bad_id):
    loaded = load(tmp_path, entry(id=bad_id))
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 1
    assert loaded.problems[0].startswith("hosts file entry #0: ")


def test_non_string_id_and_missing_id(tmp_path):
    loaded = load(tmp_path, "- {id: 5, kind: forgejo, url: 'https://a.example.org'}\n"
                            "- {kind: forgejo, url: 'https://b.example.org'}\n")
    assert_builtins_only(loaded)
    assert [p.split(":")[0] for p in loaded.problems] == ["hosts file entry #0", "hosts file entry #1"]
    assert "missing required key 'id'" in loaded.problems[1]


def test_duplicate_id(tmp_path):
    loaded = load(tmp_path, entry(id="dup", url="https://a.example.org")
                  + entry(id="dup", url="https://b.example.org", token_env="REPOHUB_DUP2_TOKEN"))
    assert loaded.registry.ids == BUILTIN_IDS + ("dup",)
    assert len(loaded.problems) == 1
    assert loaded.problems[0].startswith("hosts file entry #1: ") and "already used" in loaded.problems[0]


@pytest.mark.parametrize("kind", ["github", "gitlab", "Forgejo", 5, None])
def test_wrong_kind(tmp_path, kind):
    loaded = load(tmp_path, entry(kind=kind) if kind is not None else entry(kind="x").replace(
        "kind: \"x\"", "kind: null"))
    assert_builtins_only(loaded)
    assert loaded.problems == ["hosts file entry #0: only kind 'forgejo' is supported for extra hosts"]


def test_missing_kind_and_url(tmp_path):
    loaded = load(tmp_path, "- {id: aa, url: 'https://a.example.org'}\n- {id: bb, kind: forgejo}\n")
    assert_builtins_only(loaded)
    assert "missing required key 'kind'" in loaded.problems[0]
    assert "missing required key 'url'" in loaded.problems[1]


@pytest.mark.parametrize("url", [
    "http://git.example.org",
    "ftp://git.example.org",
    "git.example.org",
    "//git.example.org",
    "https://u:p@git.example.org",
    "https://user@git.example.org",
    "https://git.example.org:3000",
    "https://git.example.org:443",
    "https://git.example.org/git",
    "https://git.example.org//",
    "https://git.example.org/?a=1",
    "https://git.example.org?a=1",
    "https://git.example.org/#frag",
    "https://git.example.org#",
    "https://git.example.org/;p=1",
    "https://git.example.org\\@evil.example",
    "https://git.example.org/ ",
    " https://git.example.org",
    "https://git.example.org\n",
    "https://gït.example.org",
    "https://пример.рф",
    "https://git_forge.example.org",
    "https://git forge.example.org",
    "https://-git.example.org",
    "https://git-.example.org",
    "https://git..example.org",
    "https://.git.example.org",
    "https://git.example.org.",
    "https://" + "a" * 64 + ".example.org",
    "https://" + ".".join(["a" * 60] * 5),
    "https://",
    "https:///",
    "",
    "https://127.0.0.1",
    "https://10.0.0.5/",
    "https://0x7f.0x0.0x0.0x1",
    "https://2130706433",
    "https://[::1]",
    "https://[::1]:3000",
    "https://localhost",
    "https://LOCALHOST/",
    "https://forge.localhost",
    "https://intranet",
    "https://git.example.org\x1b[31m",
    "https://git.example.org‮",
])
def test_bad_urls(tmp_path, url):
    loaded = load(tmp_path, entry(url=url))
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 1, url
    assert loaded.problems[0].startswith("hosts file entry #0: "), url
    assert_clean(loaded.problems)


def test_non_string_url(tmp_path):
    loaded = load(tmp_path, "- {id: aa, kind: forgejo, url: 5}\n- {id: bb, kind: forgejo, url: [a]}\n")
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 2


def test_ip_and_localhost_messages_are_clear(tmp_path):
    p = load(tmp_path, entry(url="https://127.0.0.1")).problems[0]
    assert "IP address" in p
    p = load(tmp_path, entry(url="https://localhost")).problems[0]
    assert "localhost" in p


@pytest.mark.parametrize("url", [
    "https://git.example.org",
    "https://git.example.org/",
    "HTTPS://GIT.EXAMPLE.ORG",
    "https://xn--bcher-kva.example",
    "https://a.b.c.d.example.co.uk/",
    "https://" + "a" * 63 + ".org",
    "https://3d.example.org",
])
def test_good_urls(tmp_path, url):
    loaded = load(tmp_path, entry(url=url))
    assert loaded.problems == [], url
    assert "myforge" in loaded.registry
    domain = loaded.registry.get("myforge").domain
    assert domain == domain.lower() and "/" not in domain


def test_domain_equal_to_builtin_or_repeated(tmp_path):
    text = (entry(id="aa", url="https://github.com")
            + entry(id="bb", url="https://CODEBERG.org/")
            + entry(id="cc", url="https://gitlab.com")
            + entry(id="dd", url="https://ok.example.org")
            + entry(id="ee", url="https://OK.example.org/", token_env="REPOHUB_EE_OTHER_TOKEN"))
    loaded = load(tmp_path, text)
    assert loaded.registry.ids == BUILTIN_IDS + ("dd",)
    assert [p.split(":")[0] for p in loaded.problems] == [f"hosts file entry #{n}" for n in (0, 1, 2, 4)]
    assert all("already configured" in p for p in loaded.problems)


@pytest.mark.parametrize("token", [
    "MYTOKEN", "MY_TOKEN_", "my_token", "_X_TOKEN", "1X_TOKEN", "TOKEN", "MY-X_TOKEN", "MY X_TOKEN",
    "X_TOKEN\n", 5, "", "MYFORGE_TOKEN", "AWS_SESSION_TOKEN", "NPM_TOKEN", "CI_JOB_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN", "CODEBERG_TOKEN",
    "repohub_x_token", "Repohub_X_TOKEN", " REPOHUB_X_TOKEN", "REPOHUB_X_TOKEN ", "REPOHUB_X_TOKEN\n",
    "REPOHUB_TOKEN", "REPOHUB__TOKEN", "REPOHUB_1X_TOKEN", "REPOHUB_X_TOKEN_", "REPOHUB_" + "A" * 60 + "_TOKEN",
])
def test_bad_token_env(tmp_path, token):
    loaded = load(tmp_path, entry(token_env=token))
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 1 and "token_env" in loaded.problems[0]


def test_token_env_without_prefix_says_repohub(tmp_path):
    p = load(tmp_path, entry(token_env="AWS_SESSION_TOKEN")).problems[0]
    assert "REPOHUB_" in p


def test_token_env_length_limit_boundary(tmp_path):
    ok = "REPOHUB_A" + "B" * 50 + "_TOKEN"  # 65 characters is the maximum
    assert len(ok) == 65
    assert load(tmp_path, entry(token_env=ok)).problems == []
    assert len(load(tmp_path, entry(token_env=ok.replace("_A", "_AA", 1))).problems) == 1


def test_default_token_is_prefixed_and_never_a_bare_name(tmp_path):
    loaded = load(tmp_path, entry(id="aws-session") + entry(id="npm", url="https://n.example.org"))
    assert loaded.problems == []
    assert loaded.registry.get("aws-session").token_env == ("REPOHUB_AWS_SESSION_TOKEN",)
    assert loaded.registry.get("npm").token_env == ("REPOHUB_NPM_TOKEN",)
    for s in loaded.registry.specs:
        if not s.builtin:
            assert all(t.startswith("REPOHUB_") for t in s.token_env)


def test_default_token_for_one_letter_and_max_length_ids(tmp_path):
    loaded = load(tmp_path, entry(id="x") + entry(id="a" + "b-" * 9 + "c", url="https://l.example.org"))
    assert loaded.problems == []
    assert loaded.registry.get("x").token_env == ("REPOHUB_X_TOKEN",)


def test_token_env_unique_across_extras(tmp_path):
    text = (entry(id="aa", url="https://a.example.org", token_env="REPOHUB_SHARED_TOKEN")
            + entry(id="bb", url="https://b.example.org", token_env="REPOHUB_SHARED_TOKEN"))
    loaded = load(tmp_path, text)
    assert loaded.registry.ids == BUILTIN_IDS + ("aa",)
    assert loaded.problems[0].startswith("hosts file entry #1: ")


def test_explicit_token_env_that_is_another_hosts_default(tmp_path):
    # aa's default is REPOHUB_AA_TOKEN; bb explicitly asks for it (second) and must be refused.
    text = (entry(id="aa", url="https://a.example.org")
            + entry(id="bb", url="https://b.example.org", token_env="REPOHUB_AA_TOKEN"))
    loaded = load(tmp_path, text)
    assert loaded.registry.ids == BUILTIN_IDS + ("aa",)
    assert len(loaded.problems) == 1


def test_default_token_collisions(tmp_path):
    # first takes AA_BB_TOKEN explicitly, so the default of id "aa-bb" collides
    text = (entry(id="first", url="https://a.example.org", token_env="REPOHUB_AA_BB_TOKEN")
            + entry(id="aa-bb", url="https://b.example.org"))
    loaded = load(tmp_path, text)
    assert loaded.registry.ids == BUILTIN_IDS + ("first",)
    assert "default token variable REPOHUB_AA_BB_TOKEN" in loaded.problems[0]
    # the same id with an explicit variable works
    loaded = load(tmp_path, text + "  token_env: REPOHUB_AA_OTHER_TOKEN\n")
    assert loaded.registry.ids == BUILTIN_IDS + ("first", "aa-bb")


def test_builtin_variables_are_never_bound_to_extras(tmp_path):
    used = {t for h in hosts.BUILTIN_HOSTS for t in h.token_env}
    loaded = load(tmp_path, entry(id="gh") + entry(id="github2", url="https://g.example.org"))
    assert loaded.problems == []
    for s in loaded.registry.specs:
        if not s.builtin:
            assert not used & set(s.token_env)


def test_unknown_keys(tmp_path):
    loaded = load(tmp_path, entry(extra="x", **{"api_base": "https://evil.example"}))
    assert_builtins_only(loaded)
    assert "unknown key(s)" in loaded.problems[0] and "'api_base'" in loaded.problems[0]


@pytest.mark.parametrize("item", ["justastring", "5", "[a, b]", "null", "true", "1.5"])
def test_non_mapping_entries(tmp_path, item):
    loaded = load(tmp_path, f"- {item}\n")
    assert_builtins_only(loaded)
    assert loaded.problems == [f"hosts file entry #0: expected a mapping, got "
                               f"{type(__import__('yaml').safe_load(item)).__name__}"]


def test_name_must_be_string(tmp_path):
    loaded = load(tmp_path, "- {id: aa, kind: forgejo, url: 'https://a.example.org', name: [x]}\n")
    assert_builtins_only(loaded)
    assert "'name' must be a string" in loaded.problems[0]


def test_invalid_entry_does_not_stop_later_ones_and_numbering_is_zero_based(tmp_path):
    text = ("- 1\n" + entry(id="good1", url="https://a.example.org")
            + entry(id="Bad", url="https://b.example.org")
            + entry(id="good2", url="https://c.example.org", token_env="REPOHUB_G2_TOKEN") + "- x\n")
    loaded = load(tmp_path, text)
    assert loaded.registry.ids == BUILTIN_IDS + ("good1", "good2")
    nums = [re.match(r"hosts file entry #(\d+): ", p).group(1) for p in loaded.problems]
    assert nums == ["0", "2", "4"]


# ---- file-level -------------------------------------------------------------------------

def file_problem(loaded, path):
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 1
    assert loaded.problems[0].startswith("hosts file (")
    assert_clean(loaded.problems)
    return loaded.problems[0]


def test_directory_is_a_problem(tmp_path):
    d = tmp_path / "hosts.yaml"
    d.mkdir()
    assert "not a regular file" in file_problem(load_hosts(d), d)


def _run_with_timeout(fn):
    box = {}

    def target():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "loader hung (opened a FIFO, or expanded an alias bomb)"
    assert "e" not in box, box.get("e")
    return box["v"]


def test_fifo_is_a_problem_and_does_not_hang(tmp_path):
    fifo = tmp_path / "hosts.yaml"
    os.mkfifo(fifo)
    loaded = _run_with_timeout(lambda: load_hosts(fifo))
    assert "not a regular file" in file_problem(loaded, fifo)


def test_socket_is_a_problem(tmp_path):
    import socket
    sock_path = tmp_path / "s.sock"
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(str(sock_path))
        loaded = _run_with_timeout(lambda: load_hosts(sock_path))
    finally:
        s.close()
    assert "not a regular file" in file_problem(loaded, sock_path)


def test_char_device_is_a_problem():
    loaded = _run_with_timeout(lambda: load_hosts("/dev/null"))
    assert "not a regular file" in file_problem(loaded, "/dev/null")


def test_symlink_to_regular_file_works(tmp_path):
    real = write(tmp_path, entry(), "real.yaml")
    link = tmp_path / "hosts.yaml"
    link.symlink_to(real)
    loaded = load_hosts(link)
    assert loaded.problems == [] and "myforge" in loaded.registry


def test_dangling_symlink_is_a_problem(tmp_path):
    link = tmp_path / "hosts.yaml"
    link.symlink_to(tmp_path / "gone.yaml")
    assert "broken symlink" in file_problem(load_hosts(link), link)


def test_symlink_loop_is_a_problem(tmp_path):
    a, b = tmp_path / "a.yaml", tmp_path / "hosts.yaml"
    a.symlink_to(b)
    b.symlink_to(a)
    assert "broken symlink" in file_problem(load_hosts(b), b)


def test_symlink_to_directory_is_not_a_regular_file(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    link = tmp_path / "hosts.yaml"
    link.symlink_to(d)
    assert "not a regular file" in file_problem(load_hosts(link), link)


def test_plain_missing_file_stays_silent(tmp_path):
    assert load_hosts(tmp_path / "missing.yaml").problems == []


def test_empty_string_path_is_not_the_personal_path(tmp_path, monkeypatch):
    def boom():
        raise AssertionError("personal path must not be used")

    monkeypatch.setattr("repohub.core.hostsconfig.personal_hosts_path", boom)
    monkeypatch.chdir(tmp_path)
    for fn in (load_hosts, lambda p: LoadedHosts(hosts.registry(), configure_hosts(p))):
        loaded = fn("")
        assert loaded.problems and "not a regular file" in loaded.problems[0]
        assert_clean(loaded.problems)


def test_duplicate_keys_are_a_problem(tmp_path):
    for text in ('- {id: a, id: b, kind: forgejo, url: "https://a.example.org"}\n',
                 '- id: aa\n  kind: forgejo\n  url: "https://a.example.org"\n  kind: forgejo\n',
                 '- {id: aa}\n- {id: bb}\n'.replace("- {id: bb}", "- {x: 1, x: 2}")):
        p = write(tmp_path, text)
        assert "duplicate key" in file_problem(load_hosts(p), p)
    # a distinct key set still loads
    assert load(tmp_path, entry()).problems == []


# ---- alias bombs -------------------------------------------------------------------------

def _bomb(kind="list", depth=9):
    """An inline YAML value whose str() expands exponentially but which loads instantly."""
    if kind == "list":
        parts = ["&l0 [" + ", ".join(["x"] * 10) + "]"]
        parts += [f"&l{i} [" + ", ".join([f"*l{i-1}"] * 10) + "]" for i in range(1, depth)]
        return "[" + ", ".join(parts) + f", *l{depth-1}]"
    parts = ["&l0 {" + ", ".join(f"k{j}: x" for j in range(10)) + "}"]
    parts += [f"&l{i} {{" + ", ".join(f"k{j}: *l{i-1}" for j in range(10)) + "}" for i in range(1, depth)]
    return "[" + ", ".join(parts) + f", *l{depth-1}]"


def _timed(fn):
    import time
    t0 = time.perf_counter()
    out = _run_with_timeout(fn)
    return out, time.perf_counter() - t0


def _assert_bounded(loaded, elapsed):
    assert elapsed < 2, elapsed
    assert_builtins_only(loaded)
    assert loaded.problems
    assert all(len(p) < 400 for p in loaded.problems)
    assert_clean(loaded.problems)


@pytest.mark.parametrize("kind", ["list", "dict"])
@pytest.mark.parametrize("field", ["id", "token_env", "name", "url", "kind"])
def test_alias_bomb_in_field_is_bounded(tmp_path, kind, field):
    bomb = _bomb(kind)
    base = {"id": '"aa"', "kind": '"forgejo"', "url": '"https://a.example.org"'}
    base[field] = bomb
    text = "- {" + ", ".join(f"{k}: {v}" for k, v in base.items()) + "}\n"
    if field == "name":  # name is only checked after the others; force an error afterwards
        text = text.replace("}\n", ", token_env: 5}\n")
    assert len(text) < 256 * 1024
    p = write(tmp_path, text)
    loaded, elapsed = _timed(lambda: load_hosts(p))
    _assert_bounded(loaded, elapsed)


def test_alias_bomb_as_unknown_key_value_and_entry(tmp_path):
    for text in ("- " + _bomb() + "\n", "- {id: aa, kind: forgejo, url: 'https://a.example.org', " +
                 "extra: " + _bomb() + "}\n"):
        p = write(tmp_path, text)
        loaded, elapsed = _timed(lambda: load_hosts(p))
        _assert_bounded(loaded, elapsed)


def test_alias_bomb_as_key(tmp_path):
    p = write(tmp_path, "- {" + "? " + _bomb() + " : 1}\n")
    loaded, elapsed = _timed(lambda: load_hosts(p))
    _assert_bounded(loaded, elapsed)


def test_bomb_defined_in_earlier_entries_used_in_later_ones(tmp_path):
    defs = "- &l0 [" + ", ".join(["x"] * 10) + "]\n"
    for i in range(1, 9):
        defs += f"- &l{i} [" + ", ".join([f"*l{i-1}"] * 10) + "]\n"
    text = defs + "- {id: *l8, kind: *l8, url: *l8, name: *l8, token_env: *l8}\n"
    p = write(tmp_path, text)
    loaded, elapsed = _timed(lambda: load_hosts(p))
    _assert_bounded(loaded, elapsed)
    assert len(loaded.problems) == 10


def test_self_referencing_anchor(tmp_path):
    for text in ("- &a [*a]\n", "- &a {id: *a}\n", "- {id: &a [*a], kind: forgejo, url: 'https://a.example.org'}\n"):
        p = write(tmp_path, text)
        loaded, elapsed = _timed(lambda: load_hosts(p))
        _assert_bounded(loaded, elapsed)


def test_file_over_limit(tmp_path):
    p = tmp_path / "hosts.yaml"
    p.write_text("# " + "x" * MAX_HOSTS_BYTES + "\n", encoding="utf-8")
    assert "too large" in file_problem(load_hosts(p), p)


def test_file_exactly_at_limit_is_fine(tmp_path):
    p = tmp_path / "hosts.yaml"
    p.write_bytes(b"#" + b"x" * (MAX_HOSTS_BYTES - 2) + b"\n")
    assert p.stat().st_size == MAX_HOSTS_BYTES
    loaded = load_hosts(p)
    assert loaded.problems == []


def test_invalid_utf8(tmp_path):
    p = tmp_path / "hosts.yaml"
    p.write_bytes(b"- id: \xff\xfe\n")
    assert "UTF-8" in file_problem(load_hosts(p), p)


def test_top_level_dict_and_scalar(tmp_path):
    for text in ("id: aa\nkind: forgejo\nurl: https://a.example.org\n", "just a string\n", "5\n"):
        p = write(tmp_path, text)
        assert "must contain a list" in file_problem(load_hosts(p), p)


def test_too_many_entries_skips_whole_file(tmp_path):
    ok = "".join(entry(id=f"h{i}", url=f"https://h{i}.example.org") for i in range(20))
    assert load(tmp_path, ok).problems == []
    too_many = ok + entry(id="h20", url="https://h20.example.org")
    p = write(tmp_path, too_many)
    msg = file_problem(load_hosts(p), p)
    assert "too many entries" in msg


def test_python_object_tags_are_refused(tmp_path):
    text = "- !!python/object/apply:os.system ['echo pwned']\n"
    p = write(tmp_path, text)
    assert "invalid YAML" in file_problem(load_hosts(p), p)
    p = write(tmp_path, "- !!python/name:os.system\n")
    assert "invalid YAML" in file_problem(load_hosts(p), p)


def test_deeply_nested_yaml(tmp_path):
    p = write(tmp_path, "[" * 100000)
    loaded = _run_with_timeout(lambda: load_hosts(p))
    file_problem(loaded, p)


def test_deeply_nested_yaml_in_entry_position(tmp_path):
    p = write(tmp_path, "- " + "[" * 50000 + "]" * 50000 + "\n")
    loaded = _run_with_timeout(lambda: load_hosts(p))
    assert_builtins_only(loaded)
    assert loaded.problems  # either a file-level or an entry problem, never a raise
    assert_clean(loaded.problems)


def test_yaml_alias_bomb_is_bounded(tmp_path):
    lines = ["a: &a [x, x, x, x, x, x, x, x, x]"]
    prev = "a"
    for i in range(9):
        name = chr(ord("b") + i)
        lines.append(f"{name}: &{name} [*{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}, *{prev}]")
        prev = name
    p = write(tmp_path, "- {" + ", ".join(lines) + "}\n")
    loaded = _run_with_timeout(lambda: load_hosts(p))
    assert_builtins_only(loaded)
    assert_clean(loaded.problems)


def test_top_level_alias_bomb_as_entries(tmp_path):
    text = "- &a [x, x, x, x, x, x, x, x, x]\n" + "".join(f"- &b{i} [*a, *a, *a, *a, *a]\n" for i in range(3))
    loaded = load(tmp_path, text)
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 4


def test_yaml_error_is_one_problem_with_path(tmp_path):
    p = write(tmp_path, "- {id: [unclosed\n")
    msg = file_problem(load_hosts(p), p)
    assert "invalid YAML" in msg and str(p) in msg


def test_file_problem_path_is_capped_and_cleaned(tmp_path):
    d = tmp_path / ("d" * 150) / ("e" * 150)
    d.mkdir(parents=True)
    p = d / "hosts.yaml"
    p.mkdir()
    msg = file_problem(load_hosts(p), p)
    inner = msg[len("hosts file ("):msg.index("): ")]
    assert len(inner) <= 200


def test_hostile_text_never_leaks_raw_controls(tmp_path):
    hostile = "\x1b[31mred\x9b2J‮evil⁦\r\n\x07"
    cases = [
        entry(id=hostile),
        entry(url="https://" + hostile),
        entry(url=hostile),
        entry(name=hostile),  # valid, name is cleaned
        entry(token_env=hostile),
        entry(kind=hostile),
        "- {" + '"' + hostile.replace("\n", "").replace("\r", "") + '": 1}\n',
        '- {"id": "aa", "kind": "forgejo", "url": "https://a.example.org", ' + '"'
        + "\\u001b[31m\\u202e" + '": 1}\n',
    ]
    for text in cases:
        loaded = load(tmp_path, text)
        assert_clean(loaded.problems)
        for s in loaded.registry.specs:
            for v in (s.id, s.name, s.domain, s.api_base):
                assert not RAW_CONTROL.search(v)
    # and a problem does exist for the bad ones, with the echoed text short
    loaded = load(tmp_path, entry(id=hostile * 20))
    assert loaded.problems and len(loaded.problems[0]) < 200
    assert_clean(loaded.problems)


def test_hostile_yaml_key_in_file_level_error(tmp_path):
    p = write(tmp_path, '- {"\\u001b[31m\\u202e\\x9b": [}\n')
    loaded = load_hosts(p)
    assert_builtins_only(loaded)
    assert_clean(loaded.problems)


def test_echoed_untrusted_text_is_capped(tmp_path):
    loaded = load(tmp_path, entry(id="a" * 500))
    assert len(loaded.problems[0]) < 200


def test_builtins_not_mutated_by_extras(tmp_path):
    load(tmp_path, entry())
    assert [s.id for s in hosts.BUILTIN_HOSTS] == list(BUILTIN_IDS)
    assert hosts.registry().ids == BUILTIN_IDS


@pytest.mark.parametrize("reserved", ["all", "both"])
def test_reserved_ids_are_rejected_with_a_clear_message(tmp_path, reserved):
    loaded = load(tmp_path, entry(id=reserved))
    assert_builtins_only(loaded)
    assert len(loaded.problems) == 1 and "reserved" in loaded.problems[0]
