import repohub


def test_version():
    assert repohub.__version__ == "0.4.0"


import pytest


@pytest.mark.parametrize("module,prog", [("repohub.tui.app", "repohub-tui"), ("repohub.web.app", "repohub-web")])
def test_help_exits_without_building_a_hub(monkeypatch, capsys, module, prog):
    import importlib

    import repohub.config

    def boom(*a, **k):
        raise AssertionError("build_hub must not be called")

    monkeypatch.setattr(repohub.config, "build_hub", boom)
    main = importlib.import_module(module).main
    with pytest.raises(SystemExit) as e:
        main(["--help"])
    assert e.value.code == 0 and prog in capsys.readouterr().out


def test_tui_version_and_unknown_flag(monkeypatch, capsys):
    import repohub.config
    from repohub import __version__
    from repohub.tui.app import main

    monkeypatch.setattr(repohub.config, "build_hub", lambda: pytest.fail("no hub"))
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and __version__ in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        main(["--bogus"])
    assert e.value.code == 2


def test_cli_version_matches_package_version(capsys):
    import pytest

    from repohub import __version__
    from repohub.cli import main

    with pytest.raises(SystemExit) as e:
        main(["--version"], hub_factory=lambda: (_ for _ in ()).throw(AssertionError("hub built")))
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"repohub {__version__}"
