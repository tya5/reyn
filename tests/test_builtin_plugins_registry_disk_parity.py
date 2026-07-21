"""Tier 2: OS invariant -- BUILTIN_PLUGINS <-> disk parity gate (#3202
symptom 3), sibling of ``tests/test_builtin_registry_disk_parity.py`` for
``src/reyn/builtin/plugins/``.

**Motivation.** ``BUILTIN_PLUGINS`` (``src/reyn/builtin/registry.py``) is
the ONLY registry ``reyn.builtin.discovery.list_builtin_plugins`` (and
therefore the ``plugin_management__list`` tool) enumerates -- there is NO
directory auto-scan under ``src/reyn/builtin/plugins/`` (mirrors the
BUILTIN_SKILLS/BUILTIN_PIPELINES discipline the sibling test's docstring
documents, and #3196's "a directory on disk must never self-advertise a
capability" rule). A plugin directory that ships on disk but never gets a
``BUILTIN_PLUGINS`` entry is therefore PERMANENTLY unreachable via
``plugin_management__list`` -- the exact "new builtin plugin ships
undiscoverable" shape #3202 symptom 3 exists to close. This gate is the
completeness check the architect's firm design calls for.

Both directions are checked, each with its own vacuity guard (per
CLAUDE.md's checklist -- a gate that can pass with zero real members proves
nothing): registry -> disk (a stale entry naming a directory that doesn't
exist) and disk -> registry (a plugin dir shipped but never registered,
the #3163-shaped hole).

No fakes: imports the REAL ``reyn.builtin.registry`` module and walks the
REAL ``src/reyn/builtin/plugins/`` directory on disk.
"""
from __future__ import annotations

from pathlib import Path

import reyn.builtin.registry as registry_module
from reyn.builtin.registry import BUILTIN_PLUGINS

_BUILTIN_DIR = Path(registry_module.__file__).parent
_PLUGINS_DIR = _BUILTIN_DIR / "plugins"


def _plugin_dirs_on_disk() -> "set[str]":
    """Every immediate subdirectory of ``builtin/plugins/`` that carries a
    ``.reyn-plugin/plugin.json`` manifest (a real plugin, not e.g. an
    ``__init__.py``-only package marker dir)."""
    return {
        p.name
        for p in _PLUGINS_DIR.iterdir()
        if p.is_dir() and (p / ".reyn-plugin" / "plugin.json").is_file()
    }


def test_every_builtin_plugins_entry_exists_on_disk() -> None:
    """Tier 2: OS invariant -- registry -> disk direction. Every
    BUILTIN_PLUGINS entry must resolve to a real plugin directory (catches a
    stale/typo'd name pointing at nothing)."""
    assert len(BUILTIN_PLUGINS) >= 1, (
        "vacuity guard: BUILTIN_PLUGINS is empty -- this gate would pass "
        "trivially with nothing to check"
    )
    for name in BUILTIN_PLUGINS:
        plugin_dir = _PLUGINS_DIR / name
        manifest_path = plugin_dir / ".reyn-plugin" / "plugin.json"
        assert manifest_path.is_file(), (
            f"BUILTIN_PLUGINS[{name!r}] has no matching "
            f"src/reyn/builtin/plugins/{name}/.reyn-plugin/plugin.json on disk"
        )


def test_every_plugin_dir_on_disk_is_registered_in_builtin_plugins() -> None:
    """Tier 2: OS invariant -- disk -> registry direction (#3202 symptom 3's
    own bug shape: a plugin directory shipped on disk but never added to
    BUILTIN_PLUGINS is permanently invisible to plugin_management__list)."""
    disk_names = _plugin_dirs_on_disk()
    assert len(disk_names) >= 1, (
        "vacuity guard: no plugin manifest found under builtin/plugins/ -- "
        "this gate would pass trivially with nothing to check"
    )
    unregistered = disk_names - set(BUILTIN_PLUGINS)
    assert not unregistered, (
        "plugin directory present on disk but missing a BUILTIN_PLUGINS "
        f"entry -- permanently undiscoverable via plugin_management__list "
        f"(#3202 symptom 3): {sorted(unregistered)}"
    )
