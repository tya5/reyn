"""Tier 2: ``slash_command_completions`` — the inline CUI's ``/`` autocomplete.

Typing ``/`` in the inline input opens a completions menu fed by this helper:
non-hidden commands whose name starts with the typed prefix, each with its
one-line summary. Hidden easter-egg commands stay dispatchable by name but never
surface in the menu. Pure registry query, asserted on the public surface.
"""
from __future__ import annotations

from reyn.interfaces.slash import SlashCommand, slash_command_completions


async def _noop(session, args) -> None:  # pragma: no cover - fixture handler
    return None


_FIXTURE = [
    SlashCommand(name="model", summary="Override the model class", handler=_noop),
    SlashCommand(name="memory", summary="Memory ops", handler=_noop),
    SlashCommand(name="agents", summary="List agents", handler=_noop),
    SlashCommand(name="zen", summary="easter egg", handler=_noop, hidden=True),
]


def test_prefix_filters_and_carries_the_summary() -> None:
    """Tier 2: only commands whose name starts with the prefix are returned, each
    paired with its summary."""
    out = slash_command_completions("m", commands=_FIXTURE)
    names = [n for n, _ in out]
    assert "model" in names and "memory" in names
    assert "agents" not in names                       # prefix filtered out
    assert dict(out)["model"] == "Override the model class"


def test_hidden_commands_never_surface() -> None:
    """Tier 2: hidden commands are excluded even with an empty prefix (they stay
    dispatchable by name, just not offered in the menu)."""
    names = [n for n, _ in slash_command_completions("", commands=_FIXTURE)]
    assert "zen" not in names
    assert "model" in names and "agents" in names


def test_no_prefix_match_yields_nothing() -> None:
    """Tier 2: a prefix matching no command returns an empty list (menu closes)."""
    assert slash_command_completions("zzz", commands=_FIXTURE) == []


def test_live_registry_smoke() -> None:
    """Tier 2: against the live REGISTRY, ``/mo`` offers ``model`` and the hidden
    easter eggs (donut / matrix / zen) never appear."""
    assert "model" in [n for n, _ in slash_command_completions("mo")]
    allnames = [n for n, _ in slash_command_completions("")]
    assert not ({"donut", "matrix", "zen"} & set(allnames))
