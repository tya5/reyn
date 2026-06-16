"""Tier 2: SkillActivityRow surfaces in-phase detail signals.

Skill-execution UX audit (MED severity Finding F4): all tool ops inside
a skill were silent. ``ChatEventForwarder`` only forwarded
``phase_started`` / ``phase_completed`` / ``workflow_finished`` /
``workflow_aborted``. Long LLM calls or heavy Control IR batches
inside a phase produced no user-visible signal — the row spinner
just kept spinning on the phase name for 10–30 s with no indication
of whether the skill was making progress or stuck.

The fix wires three additional event handlers in the forwarder:

  • ``on_llm_called``          → ``"detail: llm: <model>"``
  • ``on_llm_response_received`` → ``"detail: "`` (= clear)
  • ``on_act_executed``        → ``"detail: act: N op(s)"``

The TUI's ``_handle_trace_for_skill_row`` recognises the ``"detail: "``
prefix and routes it to ``ConversationView.update_skill_detail`` which
calls ``SkillActivityRow.set_detail`` — appending a dim ``⤷ <detail>``
segment to the row. ``set_phase`` clears the detail on phase advance
(new phase = fresh context).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.forwarder import ChatEventForwarder
from reyn.chat.outbox import OutboxMessage
from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


# ── Forwarder emissions ──────────────────────────────────────────────────────


def _drain(queue: asyncio.Queue) -> list[OutboxMessage]:
    out: list[OutboxMessage] = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_forwarder_emits_llm_called_detail() -> None:
    """Tier 1: ``on_llm_called`` enqueues ``"detail: llm: <model>"``."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="r1")
    fwd.on_llm_called({"model": "opus-4-5", "phase": "p"})
    msgs = _drain(q)
    assert msgs[0].kind == "trace"
    assert msgs[0].text == "detail: llm: opus-4-5"
    assert msgs[0].meta.get("run_id") == "r1"


def test_forwarder_emits_clear_on_llm_response() -> None:
    """Tier 1: ``on_llm_response_received`` enqueues a blank detail.

    Empty payload after the prefix signals the TUI to clear the
    ``⤷ llm: …`` segment so the row doesn't lie about an in-flight
    call after the response has actually arrived.
    """
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="r1")
    fwd.on_llm_response_received({"model": "opus-4-5"})
    msgs = _drain(q)
    assert msgs[0].text == "detail: "


def test_forwarder_emits_act_executed_count() -> None:
    """Tier 1: ``on_act_executed`` enqueues an op-count summary."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="r1")
    fwd.on_act_executed({"op_count": 3, "phase": "p"})
    msgs = _drain(q)
    assert msgs[0].text == "detail: act: 3 ops"


def test_forwarder_act_executed_singular_one_op() -> None:
    """Tier 1: ``on_act_executed`` with op_count=1 uses singular ``op`` not ``ops``."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="r1")
    fwd.on_act_executed({"op_count": 1, "phase": "p"})
    msgs = _drain(q)
    assert msgs[0].text == "detail: act: 1 op"


def test_forwarder_act_executed_drops_zero_op_count() -> None:
    """Tier 1: ``op_count`` 0 or missing → no detail emitted (= noise filter)."""
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatEventForwarder("test_skill", q, run_id="r1")
    fwd.on_act_executed({"op_count": 0, "phase": "p"})
    fwd.on_act_executed({"phase": "p"})  # missing op_count
    assert _drain(q) == []


# ── SkillActivityRow set_detail ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_detail_appears_in_rendered_row() -> None:
    """Tier 2: ``set_detail(text)`` puts ``⤷ <text>`` into the rendered row."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row("r1", "my_skill")
        row.set_phase("plan", visit=1)
        row.set_detail("llm: opus-4-5")
        await pilot.pause()

        plain = row._build_running().plain
        assert "⤷ llm: opus-4-5" in plain, (
            f"detail must appear after elapsed; got {plain!r}"
        )


@pytest.mark.asyncio
async def test_set_phase_clears_detail() -> None:
    """Tier 2: advancing to a new phase clears any prior detail.

    The previous phase's ``llm: <model>`` / ``act: N op`` context is
    irrelevant once we transition. Without this, the row would carry a
    stale detail tag into the new phase.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row("r1", "my_skill")
        row.set_phase("plan", visit=1)
        row.set_detail("llm: opus-4-5")
        await pilot.pause()

        row.set_phase("review", visit=1)
        await pilot.pause()

        plain = row._build_running().plain
        assert "⤷" not in plain, (
            f"phase advance must clear detail; got {plain!r}"
        )


@pytest.mark.asyncio
async def test_set_detail_empty_string_hides_segment() -> None:
    """Tier 2: ``set_detail('')`` (the clear-detail signal) hides the segment."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.start_skill_row("r1", "my_skill")
        row.set_phase("plan", visit=1)
        row.set_detail("llm: opus")
        row.set_detail("")  # clear
        await pilot.pause()

        plain = row._build_running().plain
        assert "⤷" not in plain


# ── TUI dispatcher routes "detail: " to update_skill_detail ──────────────────


@pytest.mark.asyncio
async def test_trace_handler_routes_detail_prefix() -> None:
    """Tier 2: ``"detail: <text>"`` trace calls ``update_skill_detail``.

    Captures the call via direct attribute substitution (no
    ``unittest.mock`` per the testing policy) so the test pins the
    dispatch path independently of the SkillActivityRow render.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        captured: list[tuple[str, str]] = []

        def _fake(run_id: str, detail: str) -> None:
            captured.append((run_id, detail))

        conv.update_skill_detail = _fake  # type: ignore[method-assign]

        msg = OutboxMessage(
            kind="trace",
            text="detail: llm: opus-4-5",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured == [("r1", "llm: opus-4-5")]


@pytest.mark.asyncio
async def test_trace_handler_routes_empty_detail_clear() -> None:
    """Tier 2: ``"detail: "`` (empty payload) routes the clear signal through."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        captured: list[tuple[str, str]] = []

        def _fake(run_id: str, detail: str) -> None:
            captured.append((run_id, detail))

        conv.update_skill_detail = _fake  # type: ignore[method-assign]

        msg = OutboxMessage(
            kind="trace",
            text="detail: ",
            meta={"skill_name": "s", "run_id": "r1"},
        )
        app._handle_trace_for_skill_row(conv, msg)

        assert captured == [("r1", "")]
