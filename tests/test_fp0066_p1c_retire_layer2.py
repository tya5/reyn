"""Tier 2: FP-0066 P1c — retire layer-2 (user-facing in-core RAG source
creation).

`docs/deep-dives/proposals/0066-retrieval-two-groups-two-axes.md` §9 retires
the safe-mode `index_update()` python entry point (`reyn.api.safe.
index_update`) and the CLI `reyn source` command group — the last two
user-facing surfaces onto the in-core index (layer 1's agent-facing tools
were already retired in P1b). §9 layer 3 (the OS-internal `index_update` op
and its `core/op_runtime` / `SqliteIndexBackend` substrate) is unaffected —
this module pins ONLY that the two retired user-facing surfaces are gone,
so an accidental re-introduction is caught.
"""
from __future__ import annotations

import importlib

import pytest


def test_safe_index_update_module_is_gone() -> None:
    """Tier 2: `reyn.api.safe.index_update` no longer exists as an
    importable module — the safe-mode entry point is retired clean-break."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("reyn.api.safe.index_update")


def test_safe_package_does_not_export_index_update() -> None:
    """Tier 2: `reyn.api.safe` (the safe-mode public surface) never listed
    `index_update` in `__all__` / its module docstring even before this
    retirement (it was a directly-imported submodule, not a re-export) —
    this pins that it stays absent as an attribute too."""
    import reyn.api.safe as safe_pkg

    assert not hasattr(safe_pkg, "index_update")
    assert "index_update" not in safe_pkg.__all__


def test_cli_has_no_source_subcommand() -> None:
    """Tier 2: the CLI parser no longer registers a `source` subcommand —
    `reyn source ...` is gone along with the safe-mode entry point it
    fronted the same in-core store for."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    subparsers_action = next(
        action
        for action in parser._actions
        if getattr(action, "dest", None) == "command"
    )
    assert "source" not in subparsers_action.choices


def test_cli_commands_module_list_excludes_source() -> None:
    """Tier 2: the command registry (`cli.commands.ALL`) — the single place
    new command modules get wired into the parser — no longer references
    the retired `source` module."""
    from reyn.interfaces.cli.commands import ALL

    module_names = {mod.__name__ for mod in ALL}
    assert "reyn.interfaces.cli.commands.source" not in module_names


def test_index_update_op_still_importable() -> None:
    """Tier 2: layer 3 (OS-internal) is explicitly KEPT — the `index_update`
    op handler this retired wrapper used to dispatch onto remains
    importable and usable by internal callers."""
    from reyn.core.op_runtime.index_update import handle

    assert callable(handle)
