"""Tier 2: InterventionWidget renders source-agent badge when provided.

Issue #261 — when the outbox meta carries ``source_agent`` (= the
``parent_delegate`` branch fired upstream), the widget surfaces it as
a ``[parent: <name>]`` line so the user can tell a prompt arrived via
delegation rather than from the locally-attached agent.

When ``source_agent=None`` (= default, non-delegated path), the badge
is omitted entirely — keeping the prior layout untouched for the
common case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.widgets import Label

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


@pytest.mark.asyncio
async def test_widget_omits_source_agent_label_by_default() -> None:
    """Tier 2: no ``source_agent`` → no parent badge in the widget DOM."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="Continue?", choices=None, iv_id="iv-1",
        )
        await pilot.pause()

        # No Label with class iv-source-agent should be present in the
        # mounted widget tree.
        labels = list(app.query("Label.iv-source-agent"))
        assert labels == [], (
            f"no source-agent labels expected when source_agent=None; "
            f"got {len(labels)}"
        )


@pytest.mark.asyncio
async def test_widget_renders_source_agent_label_when_provided() -> None:
    """Tier 2: source_agent="planner" → "[parent: planner]" label rendered."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="Continue?",
            choices=None,
            iv_id="iv-2",
            source_agent="planner",
        )
        await pilot.pause()

        labels = list(app.query("Label.iv-source-agent"))
        (source_label,) = labels  # exactly 1 source-agent label expected
        text = str(source_label.render())
        assert "parent: planner" in text, (
            f"label must read '[parent: planner]'; got {text!r}"
        )


@pytest.mark.asyncio
async def test_widget_source_agent_label_precedes_question() -> None:
    """Tier 2: badge renders BEFORE the question label (= header position).

    Order matters for the visual hierarchy: the user should read the
    delegation breadcrumb first, then the question itself — analogous
    to how email shows the "From:" header above the body.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.mount_intervention(
            question="Continue?",
            choices=None,
            iv_id="iv-3",
            source_agent="planner",
        )
        await pilot.pause()

        widget = app.query_one(f"#iv_{'iv-3'[:8]}")
        # Walk the widget's children in compose order. The
        # iv-source-agent label must appear before the iv-question label.
        labels = [
            c for c in widget.walk_children()
            if isinstance(c, Label)
        ]
        src_idx = next(
            (i for i, lab in enumerate(labels)
             if "iv-source-agent" in lab.classes),
            -1,
        )
        q_idx = next(
            (i for i, lab in enumerate(labels)
             if "iv-question" in lab.classes),
            -1,
        )
        assert src_idx >= 0 and q_idx >= 0, (
            f"both labels expected; src={src_idx} q={q_idx} "
            f"labels={[(i, list(l.classes)) for i, l in enumerate(labels)]!r}"
        )
        assert src_idx < q_idx, (
            f"source-agent label must precede question label in "
            f"compose order; src={src_idx} q={q_idx}"
        )
