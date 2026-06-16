"""Tier 2: ``phase_completed`` traces update the SkillActivityRow.

Skill-execution UX audit (HIGH severity Finding F1):
``ChatEventForwarder.on_phase_completed`` enqueues ``"<phase> → <next>
(confidence=X)"`` trace messages, but the TUI's
``_handle_trace_for_skill_row`` only matched ``"phase started: "`` and
``"skill done: "`` prefixes — the transition text was silently dropped.

Between ``phase A completed`` and ``phase B started``, the outgoing
phase's LLM call can take 10–30 s. During that window the row showed
the OLD phase name (or a stale phase from earlier), making the skill
look stuck. The fix routes the transition into ``update_skill_phase``
with the parsed ``"<phase> → <next>"`` label so the user sees the
handoff is in flight.

These tests pin the parsing + dispatch contract by instrumenting
``conv.update_skill_phase`` via direct attribute substitution.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _instrument_update(conv: ConversationView):
    """Capture calls to ``update_skill_phase`` (run_id, phase, visit)."""
    captured: list[tuple[str, str, int]] = []

    def _fake(run_id: str, phase: str, visit: int = 1) -> None:
        captured.append((run_id, phase, visit))

    conv.update_skill_phase = _fake  # type: ignore[method-assign]
    return captured


# ── transition trace updates the row ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_phase_completed_trace_shows_transition_label() -> None:
    """Tier 2: ``"<phase> → <next>"`` updates the row with the transition string.

    The row should land on ``"plan → review"`` so the user sees the
    handoff is happening rather than staring at ``"plan"`` for 30 s.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        captured = _instrument_update(conv)

        msg = OutboxMessage(
            kind="trace",
            text="plan → review",
            meta={"skill_name": "code_review", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured, "no update_skill_phase call captured"
        run_id, phase, _visit = captured[-1]
        assert run_id == "r1"
        assert phase == "plan → review", (
            f"transition label wrong: got {phase!r}"
        )


@pytest.mark.asyncio
async def test_phase_completed_strips_confidence_suffix() -> None:
    """Tier 2: ``"  (confidence=0.9)"`` suffix is stripped from the row label.

    The confidence value is visible in the right-panel events tab and
    is high-detail; the conv-pane row should stay terse.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        captured = _instrument_update(conv)

        msg = OutboxMessage(
            kind="trace",
            text="plan → review  (confidence=0.9)",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured
        _, phase, _ = captured[-1]
        assert phase == "plan → review", (
            f"confidence suffix not stripped: got {phase!r}"
        )


# ── visit count preserved across transition ──────────────────────────────────


@pytest.mark.asyncio
async def test_phase_completed_keeps_existing_visit_count() -> None:
    """Tier 2: the transition reuses the just-finished phase's visit count.

    The next ``phase started`` will increment to the new phase's visit
    via the existing ``+1`` logic. Bumping during the transition would
    desync the badge from the user's mental model ("how many times has
    the skill cycled through phase X?").
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        captured = _instrument_update(conv)

        # Prime exec state with a visit count
        app._skill_exec["r1"] = {"phase_visits": 3, "skill_name": "s"}

        msg = OutboxMessage(
            kind="trace",
            text="plan → review",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured
        _, _, visit = captured[-1]
        assert visit == 3, (
            f"transition must reuse current visit count; got {visit}"
        )


# ── phase_started still works (regression guard) ─────────────────────────────


@pytest.mark.asyncio
async def test_phase_started_still_increments_visit() -> None:
    """Tier 2: the existing ``phase started:`` branch is unchanged.

    Defence against the new ``" → "`` branch accidentally catching
    ``phase started`` lines if the parsing order ever shifts.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        captured = _instrument_update(conv)

        app._skill_exec["r1"] = {"phase_visits": 2, "skill_name": "s"}

        msg = OutboxMessage(
            kind="trace",
            text="phase started: review",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured
        _, phase, visit = captured[-1]
        assert phase == "review"
        assert visit == 3, "phase_started must continue to bump visit by 1"


# ── skill done: pattern is NOT treated as a transition ──────────────────────


@pytest.mark.asyncio
async def test_skill_done_branch_does_not_run_transition_path() -> None:
    """Tier 2: ``"skill done: …"`` text is handled by the dedicated branch.

    The new transition branch keys on ``" → "`` substring; a
    pathological skill-done text containing that substring would
    otherwise route to the wrong handler. The explicit
    ``not text.startswith("skill done: ")`` guard prevents that.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        captured = _instrument_update(conv)

        # Pin: skill_done text shouldn't accidentally trigger update_skill_phase
        msg = OutboxMessage(
            kind="trace",
            text="skill done: finished",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)
        # No transition call should have happened — done is handled by
        # finish_skill_row, which we didn't instrument here.
        assert captured == [], (
            f"skill done leaked into the transition branch: {captured}"
        )
