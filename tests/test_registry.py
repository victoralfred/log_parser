from pathlib import Path

from logscope.core.registry import Registry

FIXTURES = Path(__file__).parent / "fixtures"


def test_loads_builtins_and_fixtures():
    reg = Registry(plugin_dirs=[FIXTURES])
    reg.load()
    by_name = {s.name: s for s in reg.statuses()}

    # built-ins present
    assert by_name["agent-files"].ok
    assert by_name["journald"].ok
    assert by_name["journald"].panels  # contributes the lifecycle panel

    # toy fixture loaded and usable
    assert by_name["toy"].ok
    toy = reg.get("toy")
    sources = toy.discover(None)
    records = list(toy.scan(sources[0], None))
    assert len(records) == 3

    # broken fixture reported, not fatal
    assert not by_name["broken_plugin"].ok
    assert "deliberately broken" in by_name["broken_plugin"].error


def test_broken_plugin_does_not_block_others():
    reg = Registry(plugin_dirs=[FIXTURES])
    reg.load()
    ok_names = {s.name for s in reg.statuses() if s.ok}
    assert {"agent-files", "journald", "toy"} <= ok_names
