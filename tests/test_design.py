"""The look of the web app: contrast in both themes, the theme switch, and accessibility basics."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import FakeProvider, make_hub, mk
from repohub.core.browse import Shelf
from repohub.web.app import CSP, create_app

STATIC = Path(__import__("repohub").__file__).parent / "web" / "static"
TEMPLATES = Path(__import__("repohub").__file__).parent / "web" / "templates"
CSS = (STATIC / "app.css").read_text()


def tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\s*;", block))


def blocks() -> dict[str, dict[str, str]]:
    light = re.search(r"^:root\s*\{(.*?)^\}", CSS, re.S | re.M).group(1)
    auto_dark = re.search(r':root:not\(\[data-theme="light"\]\)\s*\{(.*?)\n  \}', CSS, re.S).group(1)
    forced_dark = re.search(r'^:root\[data-theme="dark"\]\s*\{(.*?)^\}', CSS, re.S | re.M).group(1)
    return {"light": tokens(light), "auto-dark": tokens(auto_dark), "dark": tokens(forced_dark)}


def lum(h: str) -> float:
    r, g, b = (int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# every foreground token on every background it is drawn on
PAIRS = [("text", "bg"), ("text", "surface"), ("text", "surface2"), ("text", "info-bg"), ("text", "warn-bg"), ("text", "bad-bg"),
         ("dim", "bg"), ("dim", "surface"), ("dim", "surface2"), ("dim", "info-bg"),
         ("accent", "bg"), ("accent", "surface"), ("accent", "surface2"), ("accent", "info-bg"),
         ("accent-hover", "surface"), ("on-accent", "accent"), ("on-accent", "accent-hover"),
         ("ok", "ok-bg"), ("ok", "surface"), ("warn", "warn-bg"), ("warn", "surface"), ("bad", "bad-bg"), ("bad", "surface")]


def test_light_and_dark_define_the_same_tokens_and_dark_copies_match():
    b = blocks()
    assert set(b["light"]) == set(b["dark"]) and len(b["light"]) >= 15
    assert b["auto-dark"] == b["dark"]  # following the system and choosing Dark look identical


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("fg,bg", PAIRS)
def test_text_meets_wcag_aa_contrast_in_both_themes(theme, fg, bg):
    t = blocks()[theme]
    assert ratio(t[fg], t[bg]) >= 4.5, f"{theme}: {fg} on {bg} is {ratio(t[fg], t[bg]):.2f}:1"


def test_the_two_themes_really_differ():
    b = blocks()
    assert lum(b["light"]["bg"]) > 0.8 and lum(b["dark"]["bg"]) < 0.05
    assert lum(b["light"]["text"]) < 0.05 and lum(b["dark"]["text"]) > 0.6


def test_theme_follows_the_system_unless_a_choice_was_made():
    assert "@media (prefers-color-scheme: dark)" in CSS
    assert ':root:not([data-theme="light"])' in CSS and ':root[data-theme="dark"]' in CSS
    assert "color-scheme: light" in CSS and "color-scheme: dark" in CSS


def test_stylesheet_uses_tokens_not_hardcoded_colors():
    outside = CSS
    for m in re.finditer(r"^:root\b.*?^\}|@media \(prefers-color-scheme: dark\) \{.*?\n\}\n|@media print \{.*?\}\s*$", CSS, re.S | re.M):
        outside = outside.replace(m.group(0), "")
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", outside), "colors belong in the token blocks"
    assert "rgba(" not in outside


def test_focus_motion_and_print_are_handled():
    assert ":focus-visible" in CSS and "outline: 3px" in CSS
    assert "prefers-reduced-motion: reduce" in CSS and "@media print" in CSS


# ---------------------------------------------------------------- the pages

@pytest.fixture
def client(tmp_path):
    gh = FakeProvider("github", [mk("github", "o/r", 5)])
    app = create_app(make_hub(gh), tmp_path, session_token="t", shelves=[Shelf("s", topic="x")])
    return TestClient(app, base_url="http://localhost")


def test_header_has_a_three_way_theme_switch(client):
    t = client.get("/").text
    assert 'id="theme-select"' in t and 'aria-label="Theme"' in t
    for v in ("auto", "light", "dark"):
        assert f'<option value="{v}">' in t
    assert '<meta name="color-scheme" content="light dark">' in t


def test_theme_script_loads_first_and_the_page_has_no_inline_script(client):
    t = client.get("/").text
    assert t.index("/static/theme.js") < t.index("/static/app.css")
    assert "<script>" not in t and "onclick" not in t and "onchange" not in t
    assert "script-src 'self'" in CSP and "unsafe-inline" not in CSP.split("script-src")[1].split(";")[0]
    assert client.get("/static/theme.js").status_code == 200 and client.get("/static/app.js").status_code == 200


def test_theme_scripts_persist_the_choice_safely_and_never_throw():
    js = (STATIC / "theme.js").read_text()
    assert "localStorage" in js and "try" in js and "catch" in js and "repohub-theme" in js
    app_js = (STATIC / "app.js").read_text()
    assert "theme-select" in app_js and "removeAttribute" in app_js and app_js.count("catch") >= 1
    assert 'v === "auto"' in app_js and "localStorage.removeItem" in app_js
    for bad in ("eval(", "innerHTML", "document.write"):
        assert bad not in js and bad not in app_js


def test_skip_link_landmarks_and_labels(client):
    t = client.get("/").text
    assert 'class="skip-link" href="#main"' in t and '<main id="main"' in t
    assert '<nav class="site-nav" aria-label="Main">' in t and 'role="search"' in t
    assert t.count("<h1") <= 1 and 'lang="en"' in t


def test_filters_are_tucked_away_but_open_when_used(client):
    plain = client.get("/").text
    assert "<details class=\"filters\" >" in plain or '<details class="filters" >' in plain.replace("\n", "")
    used = client.get("/search", params={"q": "x", "language": "Rust"}).text
    assert re.search(r'<details class="filters"\s+open>', used)
    for field in ('name="language"', 'name="min_stars"', 'name="days"', 'name="host"', 'name="sort"', 'name="archived"', 'name="hide_forks"'):
        assert field in plain  # still in the page, so nothing was lost


def test_every_template_page_still_renders(client):
    for url in ("/", "/favorites", "/favorites/releases", "/accounts", "/folders", "/repo/github/o/r", "/search?q=x"):
        r = client.get(url)
        assert r.status_code == 200 and 'id="theme-select"' in r.text, url


def test_no_template_hardcodes_colors():
    for f in TEMPLATES.glob("*.html"):
        assert not re.search(r'style="[^"]*#[0-9a-fA-F]{3,6}', f.read_text()), f.name


# ---------------------------------------------------------------- terminal app

from repohub.core.settings import Settings


def test_settings_theme_defaults_to_dark_and_only_accepts_known_values(tmp_path):
    s = Settings()
    assert s.get_theme() == "dark"
    s.set_theme("light")
    assert s.get_theme() == "light"
    with pytest.raises(ValueError):
        s.set_theme("neon")
    db = str(tmp_path / "s.db")
    Settings(db).set_theme("light")
    assert Settings(db).get_theme() == "light"
    other = Settings(db)
    other._db.execute("INSERT OR REPLACE INTO settings (name, value) VALUES ('theme', 'garbage')")
    other._db.commit()
    assert Settings(db).get_theme() == "dark"  # an unknown stored value falls back safely


async def _tui(tmp_path, theme=None):
    from repohub.core.actionlog import ActionLog
    from repohub.core.awareness import Awareness
    from repohub.core.machine import Machine
    from repohub.core.runner import Runner
    from repohub.tui.app import RepoHubApp

    st = Settings()
    if theme:
        st.set_theme(theme)
    aw = Awareness(tmp_path, Machine("arm64", "Linux", 7.0, frozenset()))
    runner = Runner(aw, st, ActionLog(), environ={})
    return RepoHubApp(make_hub(FakeProvider("github", [])), tmp_path, shelves=[Shelf("s", topic="x")], awareness=aw, runner=runner), st


async def test_tui_starts_dark_and_f7_switches_and_remembers(tmp_path):
    app, st = await _tui(tmp_path)
    async with app.run_test() as pilot:
        assert app.theme == "textual-dark"
        await pilot.press("f7")
        await pilot.pause()
        assert app.theme == "textual-light" and st.get_theme() == "light"
        await pilot.press("f7")
        await pilot.pause()
        assert app.theme == "textual-dark" and st.get_theme() == "dark"


async def test_tui_starts_in_the_saved_theme(tmp_path):
    app, _ = await _tui(tmp_path, "light")
    async with app.run_test():
        assert app.theme == "textual-light"


async def test_tui_theme_key_works_while_typing_and_over_a_modal(tmp_path):
    from textual.widgets import Input

    from repohub.tui.app import ConfirmWrite

    app, _ = await _tui(tmp_path)
    async with app.run_test() as pilot:
        app.query_one(Input).focus()
        await pilot.press("f7")
        await pilot.pause()
        assert app.theme == "textual-light"
        app.push_screen(ConfirmWrite("x"))
        await pilot.pause()
        await pilot.press("f7")
        await pilot.pause()
        assert app.theme == "textual-dark"


def test_tui_styles_use_theme_variables_only():
    from repohub.tui.app import RepoHubApp

    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", RepoHubApp.CSS) and "$primary" in RepoHubApp.CSS
    assert "ConfirmWrite" in RepoHubApp.CSS and "align: center middle" in RepoHubApp.CSS
