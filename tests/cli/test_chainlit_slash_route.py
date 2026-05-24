"""Tier 1: ``reyn.chainlit_app.slash_route`` contract.

Pinned invariants:

1. ``is_slash`` truth table: ``/`` prefix → True, anything else → False.
   Empty / None / leading-space inputs → False (= predictable for paste
   edge cases).
2. ``QUICK_ACTIONS`` exposes a non-empty curated set; each entry's
   ``slash_text`` itself satisfies ``is_slash`` (= self-consistency,
   no malformed catalog entries).
3. ``action_name_for`` namespaces with ``slash_`` prefix and is stable
   per QuickAction (= callback registry key must match what the
   decorator + welcome button builder use).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from reyn.chainlit_app.slash_route import (
    _FALLBACK_ICON,
    QUICK_ACTIONS,
    QuickAction,
    action_name_for,
    build_command_dicts,
    icon_for_slash_name,
    is_slash,
)


@dataclass
class _FakeCommand:
    """Minimal stand-in for ``reyn.chat.slash.SlashCommand`` — just the
    fields ``build_command_dicts`` reads."""
    name: str
    summary: str
    hidden: bool = False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/help", True),
        ("/agents", True),
        ("/", True),  # bare slash — odd but matches CUI/TUI handling
        ("hello", False),
        ("hello /world", False),  # only leading / counts
        (" /help", False),  # leading whitespace → not slash (paste edge case)
        ("", False),
    ],
)
def test_is_slash_truth_table(text: str, expected: bool):
    """Tier 1: exact behavior on representative inputs."""
    assert is_slash(text) is expected


def test_is_slash_handles_none_safely():
    """Tier 1: None / empty → False (no exception, predictable for
    callers using ``message.content or ''`` shorthand)."""
    assert is_slash("") is False
    assert is_slash(None) is False  # type: ignore[arg-type]


def test_quick_actions_non_empty():
    """Tier 1: catalog ships at least one entry (= the welcome button
    row would otherwise render empty)."""
    assert len(QUICK_ACTIONS) >= 1


def test_quick_actions_self_consistent():
    """Tier 1: every catalog entry's ``slash_text`` is itself a valid
    slash command (= the dispatcher's first guard always passes when
    invoked from a button click)."""
    for qa in QUICK_ACTIONS:
        assert isinstance(qa, QuickAction)
        assert qa.name, "QuickAction.name must be non-empty"
        assert qa.label.startswith("/"), (
            f"QuickAction.label must look like a slash command: {qa.label!r}"
        )
        assert is_slash(qa.slash_text), (
            f"QuickAction.slash_text must satisfy is_slash: {qa.slash_text!r}"
        )


def test_quick_action_names_unique():
    """Tier 1: ``action_name_for`` returns a unique key per entry; a
    duplicate would silently shadow the prior ``@cl.action_callback``."""
    keys = [action_name_for(qa) for qa in QUICK_ACTIONS]
    assert len(keys) == len(set(keys))


def test_action_name_for_uses_slash_prefix():
    """Tier 1: callback key namespace is ``slash_<name>`` (= shared with
    the welcome button builder)."""
    qa = QuickAction(name="example", label="/example", slash_text="/example")
    assert action_name_for(qa) == "slash_example"


# ── build_command_dicts (slash typing palette) ──────────────────────────


def test_build_command_dicts_basic_shape():
    """Tier 1: each visible SlashCommand → CommandDict with required
    chainlit fields (id, description, icon)."""
    out = build_command_dicts([
        _FakeCommand(name="agents", summary="List all agents"),
    ])
    assert out == [{
        "id": "/agents",
        "description": "List all agents",
        "icon": "users",  # known mapping
    }]


def test_build_command_dicts_drops_hidden():
    """Tier 1: hidden commands (= matrix / donut / zen etc.) never
    surface in the typing palette."""
    out = build_command_dicts([
        _FakeCommand(name="agents", summary="visible"),
        _FakeCommand(name="matrix", summary="easter egg", hidden=True),
        _FakeCommand(name="zen", summary="hidden", hidden=True),
        _FakeCommand(name="skills", summary="visible too"),
    ])
    ids = [d["id"] for d in out]
    assert ids == ["/agents", "/skills"]


def test_build_command_dicts_sorts_by_id():
    """Tier 1: output sorted by id so the popup palette is stable
    across reloads (= input order doesn't matter)."""
    out = build_command_dicts([
        _FakeCommand(name="zebra", summary="z"),
        _FakeCommand(name="apple", summary="a"),
        _FakeCommand(name="mango", summary="m"),
    ])
    assert [d["id"] for d in out] == ["/apple", "/mango", "/zebra"]


def test_build_command_dicts_empty_name_skipped():
    """Tier 1: defensive — entries without a name don't crash, just drop."""
    out = build_command_dicts([
        _FakeCommand(name="", summary="no name"),
        _FakeCommand(name="ok", summary="fine"),
    ])
    assert [d["id"] for d in out] == ["/ok"]


def test_build_command_dicts_empty_input_returns_empty_list():
    """Tier 1: no commands → no palette entries (= safe to call unconditionally
    at chat-start time)."""
    assert build_command_dicts([]) == []


def test_icon_for_slash_name_known_mappings():
    """Tier 1: the curated lucide-icon map applies for the commands
    we picked it for."""
    assert icon_for_slash_name("agents") == "users"
    assert icon_for_slash_name("cost") == "dollar-sign"
    assert icon_for_slash_name("help") == "help-circle"


def test_icon_for_slash_name_falls_back_on_unknown():
    """Tier 1: unknown command name → fallback icon (= no crash,
    typing palette still gets an icon for every entry)."""
    assert icon_for_slash_name("not_in_table_at_all") == _FALLBACK_ICON
