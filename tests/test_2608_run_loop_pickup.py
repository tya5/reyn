"""Tests for #2608 — closing the run-loop-pickup coverage gap.

A live-dogfood + investigation of the external-event->hooks arc (#2608)
confirmed BY REPRODUCTION that a background-fired external-event hook push
(``wake=True``) wakes an IDLE ``session.run()`` loop and runs a
hook-attributed turn. But the existing H1/H4 unit tests
(``tests/test_2608_h1_mcp_resource_updated_hook.py``,
``tests/test_2608_h4_fs_watcher.py``) call the producer/dispatch path
directly and only assert the templated push LANDS in ``session.inbox`` —
they never start ``session.run()``, so they cannot observe the run-loop
actually picking the push up off the inbox and executing a turn. That is
the "unit-green != live-works" hole this file closes.

This test starts a REAL ``Session.run()`` as a background task, lets it go
idle, fires a REAL ``dispatch_external_event`` call (the exact public entry
point the FsWatcher drain task / MCP resources/updated bridge / cron+webhook
ingress all use — see ``Session.dispatch_external_event``'s docstring) from a
separate background task, and asserts a turn ACTUALLY RAN: a ``turn_started``
event with ``kind == "hook"`` was emitted and the hook-attributed
router-loop turn completed (``turn_settled``), via the session's public
event log / inbox surface — never private state.

Policy (docs/deep-dives/contributing/testing.md): real instances only — no
``unittest.mock``/``MagicMock``/``AsyncMock``/``patch``. The ONLY faked
boundary is the LLM call itself (``reyn.runtime.router_loop.call_llm_tools``),
replaced with a real async stub function — the same established idiom
``tests/test_1800_wake_drain.py`` uses to drive ``session.run()`` end-to-end
without a live model.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _text_result(text: str = "ok") -> LLMToolCallResult:
    """Real LLMToolCallResult that makes RouterLoop emit a text reply."""
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _make_llm_stub_fn(result: LLMToolCallResult):  # type: ignore[no-untyped-def]
    """A real async callable mimicking ``call_llm_tools`` — the LLM boundary is
    the only thing allowed to be faked (policy); the run-loop / dispatch /
    hook machinery below it all run for real."""

    async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
        return result

    return _stub


async def _wait_for(predicate, *, attempts: int = 200, delay: float = 0.02) -> None:
    """Poll ``predicate()`` until True or give up — the hook fire and the
    run-loop's pickup of it both happen asynchronously off separate tasks."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


def _make_session(tmp_path: Path, *, hooks_config: list) -> Session:
    return make_session(
        agent_name="test-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


@pytest.mark.asyncio
async def test_dispatch_external_event_wakes_idle_run_loop_and_runs_hook_turn(
    tmp_path, monkeypatch,
):
    """Tier 2: THE run-loop-pickup proof the H1/H4 unit tests stop one step
    short of. A REAL ``Session.run()`` idles on its inbox; a background-fired
    ``dispatch_external_event`` (the exact call FsWatcher / the MCP bridge /
    cron+webhook ingress all make) must not just land a message in the
    inbox — it must WAKE the run-loop and RUN a hook-attributed turn:
    ``turn_started`` with ``kind == "hook"`` is emitted, and the turn
    completes (``turn_settled``) via the REAL router loop (LLM boundary
    faked only)."""
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _make_llm_stub_fn(_text_result("hook turn ran")),
    )

    hooks_config = [
        {
            "on": "mcp_resource_updated",
            "template_push": {
                "message": "[{{ server }}] {{ uri }} updated",
                "wake": True,
            },
        },
    ]
    session = _make_session(tmp_path, hooks_config=hooks_config)

    run_task = asyncio.create_task(session.run())
    try:
        # Let the loop reach its idle blocking-get with nothing to do yet.
        await asyncio.sleep(0.1)
        assert session.inbox.empty()
        hook_turns_before = [
            e for e in session._chat_events.all()
            if e.type == "turn_started" and e.data.get("kind") == "hook"
        ]
        assert hook_turns_before == []

        # From a SEPARATE background task — the same shape a real external
        # producer (FsWatcher's drain task, the MCP receive-loop task) uses:
        # it never runs on the session's own run-loop task.
        async def _fire() -> None:
            await session.dispatch_external_event(
                "mcp_resource_updated",
                {"server": "srv", "uri": "resource://counter"},
            )

        fire_task = asyncio.create_task(_fire())
        await fire_task

        # The load-bearing assertion: a turn ACTUALLY RAN off the run-loop
        # picking the push up — not just "the message sits in the inbox".
        await _wait_for(
            lambda: any(
                e.type == "turn_started" and e.data.get("kind") == "hook"
                for e in session._chat_events.all()
            )
        )
        (hook_turn_started,) = [
            e for e in session._chat_events.all()
            if e.type == "turn_started" and e.data.get("kind") == "hook"
        ]
        assert hook_turn_started.data.get("kind") == "hook"

        # And the turn actually completed (the router loop ran to settlement,
        # not just got dispatched and stalled).
        await _wait_for(
            lambda: any(
                e.type == "turn_settled" and e.data.get("kind") == "hook"
                for e in session._chat_events.all()
            )
        )

        # The templated hook push was consumed as the turn's trigger — the
        # inbox drained it, it did not just sit there waiting to be read.
        assert session.inbox.empty()
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
