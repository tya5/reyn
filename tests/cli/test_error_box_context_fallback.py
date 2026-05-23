"""Tier 2: ErrorBox context_lines fallback + ConversationView short-key derivation.

Wave-13 T1-1 (Topic A #1 + #2).

A#1: When ``details`` is empty but ``context_lines`` are provided, the
expand region renders the context lines (chain_id / skill / run_id /
dimension) instead of repeating the header message verbatim. When
``details`` is non-empty, ``context_lines`` are silently ignored
(details wins).

A#2: ``ConversationView.render_message`` with kind="error" derives
``skill_name`` and ``run_id_short`` from ``meta["skill"]`` /
``meta["run_id"]`` when the short keys are absent. This restores the
``[skill#abcd]`` header prefix and re-enables the Ctrl+B trace hint for
direct router-emitted errors.

Public surfaces asserted:
  - ErrorBox compose: ``Static.eb-details`` rendered text (via ``.plain``)
  - ErrorBox._skill_name / ._run_id_short  (init-time value, not private
    runtime state — these are set in ``__init__`` and read by tests that
    build via the normal ctor, not bypassing super().__init__)
  - ConversationView.mount_error ← called by render_message
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    from reyn.chat.tui.app import ReynTUIApp
    return ReynTUIApp(
        registry=None, agent_name="t", model="m", budget_tracker=None
    )


def _rendered_details_text(box) -> str:
    """Return the plain text of the eb-details Static inside ``box``.

    Uses Static.render() which returns the Rich Content / Text object;
    that carries a ``.plain`` attribute with the unstyled text content.
    """
    from textual.widgets import Static
    try:
        static = box.query_one(".eb-details", Static)
    except Exception:
        return ""
    try:
        rendered = static.render()
        return str(getattr(rendered, "plain", rendered))
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# A#1 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_lines_rendered_when_details_empty() -> None:
    """Tier 2: context_lines appear in expand region when details is empty."""
    from reyn.chat.tui.widgets.error_box import ErrorBox

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        box = ErrorBox(
            message="router error",
            details="",
            context_lines=["chain_id=abc123", "skill=foo"],
        )
        app.query_one("#conversation").mount(box)
        await pilot.pause()

        text = _rendered_details_text(box)
        assert "chain_id=abc123" in text, (
            f"Expected 'chain_id=abc123' in expand text, got: {text!r}"
        )
        assert "skill=foo" in text, (
            f"Expected 'skill=foo' in expand text, got: {text!r}"
        )


@pytest.mark.asyncio
async def test_details_wins_over_context_lines() -> None:
    """Tier 2: when details non-empty, context_lines are ignored (details wins)."""
    from reyn.chat.tui.widgets.error_box import ErrorBox

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        box = ErrorBox(
            message="router error",
            details="the real detail text",
            context_lines=["x=should-not-appear"],
        )
        app.query_one("#conversation").mount(box)
        await pilot.pause()

        text = _rendered_details_text(box)
        assert "the real detail text" in text, (
            f"Expected details text in expand region, got: {text!r}"
        )
        assert "x=should-not-appear" not in text, (
            f"context_lines should be suppressed when details non-empty, got: {text!r}"
        )


# ---------------------------------------------------------------------------
# A#2 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_message_derives_skill_and_run_id_short() -> None:
    """Tier 2: render_message derives skill_name/run_id_short from meta.skill/meta.run_id."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.error_box import ErrorBox

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        msg = OutboxMessage(
            kind="error",
            text="classify timeout",
            meta={
                "skill": "foo",
                "run_id": "TS_foo_abcd",
                # no skill_name, no run_id_short — direct router emission
            },
        )
        conv.render_message(msg)
        await pilot.pause()

        boxes = list(conv.query(ErrorBox))
        assert boxes, "ErrorBox should have been mounted"
        box = boxes[-1]
        # The header prefix [foo#abcd] must appear in the rendered header
        header = box._header_text()
        assert "[foo#abcd]" in header, (
            f"Expected '[foo#abcd]' prefix in header, got: {header!r}"
        )
        # Internal derivation values (set in __init__) confirm correctness.
        assert box._skill_name == "foo", (
            f"Expected _skill_name='foo', got: {box._skill_name!r}"
        )
        assert box._run_id_short == "abcd", (
            f"Expected _run_id_short='abcd', got: {box._run_id_short!r}"
        )


@pytest.mark.asyncio
async def test_render_message_does_not_double_derive_when_short_keys_present() -> None:
    """Tier 2: render_message uses skill_name/run_id_short as-is when already set."""
    from reyn.chat.outbox import OutboxMessage
    from reyn.chat.tui.widgets import ConversationView
    from reyn.chat.tui.widgets.error_box import ErrorBox

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        msg = OutboxMessage(
            kind="error",
            text="skill error",
            meta={
                # forwarder.py already set both short keys
                "skill_name": "code_review",
                "run_id_short": "ef01",
                # full keys also present (should NOT override already-set shorts)
                "skill": "wrong_skill",
                "run_id": "TS_something_wrongwrong",
            },
        )
        conv.render_message(msg)
        await pilot.pause()

        boxes = list(conv.query(ErrorBox))
        assert boxes, "ErrorBox should have been mounted"
        box = boxes[-1]
        assert box._skill_name == "code_review", (
            f"skill_name should come from meta.skill_name, got: {box._skill_name!r}"
        )
        assert box._run_id_short == "ef01", (
            f"run_id_short should come from meta.run_id_short, got: {box._run_id_short!r}"
        )
