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

import pytest

from reyn.chainlit_app.slash_route import (
    QUICK_ACTIONS,
    QuickAction,
    action_name_for,
    is_slash,
)


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
