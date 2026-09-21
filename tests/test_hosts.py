import pytest

from repohub.core import hosts
from repohub.core.hosts import BUILTIN_HOSTS, HostRegistry, HostSpec


def _spec(id="x", kind="forgejo", domain="x.example"):
    return HostSpec(id, kind, id.title(), domain, f"https://{domain}/api/v1")


def test_builtin_ids_and_kinds():
    reg = hosts.registry()
    assert reg.ids == ("github", "gitlab", "codeberg")
    assert [s.kind for s in reg.specs] == ["github", "gitlab", "forgejo"]
    assert all(s.builtin for s in BUILTIN_HOSTS)


def test_codeberg_spec():
    cb = hosts.registry().get("codeberg")
    assert cb.api_base == "https://codeberg.org/api/v1"
    assert cb.web_base == "https://codeberg.org"
    assert cb.token_env == ("CODEBERG_TOKEN",)


def test_contains_and_get():
    reg = hosts.registry()
    assert "github" in reg and "codeberg" in reg
    assert "nope" not in reg
    assert reg.get("gitlab").domain == "gitlab.com"
    assert reg.get("nope") is None


def test_rank():
    reg = hosts.registry()
    assert (reg.rank("github"), reg.rank("gitlab"), reg.rank("codeberg")) == (0, 1, 2)
    assert reg.rank("unknown") == 3


def test_id_for_domain():
    reg = hosts.registry()
    assert reg.id_for_domain("github.com") == "github"
    assert reg.id_for_domain("CodeBerg.ORG") == "codeberg"
    assert reg.id_for_domain("evil.example") is None


def test_clone_domains():
    assert hosts.registry().clone_domains() == {
        "github": "github.com", "gitlab": "gitlab.com", "codeberg": "codeberg.org"}


@pytest.mark.parametrize("host", ["github", "codeberg"])
def test_slug_ok_two_segments(host):
    reg = hosts.registry()
    assert reg.slug_ok(host, "o/r")
    assert not reg.slug_ok(host, "o/r/x")
    assert not reg.slug_ok(host, "o/../x")
    assert not reg.slug_ok(host, "o/r\n")


def test_slug_ok_gitlab_nested():
    reg = hosts.registry()
    assert reg.slug_ok("gitlab", "g/sub/p")
    assert reg.slug_ok("gitlab", "g/p")
    assert not reg.slug_ok("gitlab", "g/../p")


def test_slug_ok_unknown_host():
    assert hosts.registry().slug_ok("nope", "o/r") is False


def test_slug_ok_custom_forgejo_host_is_two_segments():
    reg = HostRegistry([_spec("mine")])
    assert reg.slug_ok("mine", "o/r")
    assert not reg.slug_ok("mine", "o/r/x")


def test_rejects_unknown_kind():
    with pytest.raises(ValueError):
        HostRegistry([_spec(kind="bitbucket")])


@pytest.mark.parametrize("bad", ["Bad", "1x", "a" * 21, "", "-x", "a_b", "aa\n", "aa "])
def test_rejects_invalid_id(bad):
    with pytest.raises(ValueError):
        HostRegistry([_spec(id=bad)])


def test_accepts_max_length_id():
    assert HostRegistry([_spec(id="a" * 20)]).ids == ("a" * 20,)


def test_rejects_duplicate_id():
    with pytest.raises(ValueError):
        HostRegistry([_spec("a", domain="a.example"), _spec("a", domain="b.example")])


def test_rejects_duplicate_domain():
    with pytest.raises(ValueError):
        HostRegistry([_spec("a", domain="same.example"), _spec("b", domain="same.example")])


def test_set_registry_and_reset():
    custom = HostRegistry([_spec("only")])
    try:
        hosts.set_registry(custom)
        assert hosts.registry() is custom
        assert hosts.registry().ids == ("only",)
    finally:
        hosts.reset_registry()
    assert hosts.registry().ids == ("github", "gitlab", "codeberg")


def test_autouse_fixture_resets_after_leak_part1():
    hosts.set_registry(HostRegistry([_spec("leaky")]))
    assert hosts.registry().ids == ("leaky",)


def test_autouse_fixture_resets_after_leak_part2():
    # Passes in either order: the autouse fixture restored the built-ins.
    assert hosts.registry().ids == ("github", "gitlab", "codeberg")


@pytest.mark.parametrize("bad", ["Git.Example.org", "git.example.org\n", "", "-x.org", "x.org-", ".x.org",
                                 "x_y.org", "x y.org", "git.example.org/", "gït.org", "a" * 254])
def test_rejects_invalid_domain(bad):
    with pytest.raises(ValueError):
        HostRegistry([_spec("a", domain=bad)])


def test_accepts_valid_domains():
    assert HostRegistry([_spec("a", domain="x"), _spec("b", domain="git.example.org"),
                         _spec("c", domain="a" + "b" * 251 + "c")]).ids == ("a", "b", "c")


@pytest.mark.parametrize("reserved", ["all", "both"])
def test_reserved_ids_are_rejected_by_the_registry(reserved):
    with pytest.raises(ValueError, match="reserved"):
        HostRegistry([_spec(reserved)])
