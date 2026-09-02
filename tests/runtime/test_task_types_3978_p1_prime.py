"""Tier 2: proposal 0067 P1' — keep the task open while its issuer delegates (#3978).

Two witnesses architect specified for this stage:
  ① dispatch_kind="async" no longer closes the task — MessageBus.request
     must NOT return early with just the "delegated" ack while a delegation
     is outstanding, even with an empty inbox (the exact gap #3978's
     Verification notes table names: "its docstring promises three
     conditions; the implementation checks inbox.empty() only"). Exercised
     via bus.request()'s own public timeout behavior, not the private
     _is_quiescent staticmethod (testing.md Tier 4: no private-state
     assertion — test_tier_audit.py itself catches a direct call).
  ② the requester's real answer settles current_task, not a "delegated" ack
     — handle_agent_response's set/clear logic must survive multi-round
     delegation AND exceptions (lead-coder review: "誰が消すか" must be
     guaranteed by finally, not the happy path).

Plus lead-coder's two required failure-path decisions:
  - exception path clears current_task (finally, not except-only)
  - crash/rewind: current_task does not survive either recovery path.

Real Session + real InterAgentMessaging instances throughout (the
InterAgentMessaging harness below is the same "real instance + plain stub
callbacks" pattern test_a2a_handler_invariants.py already established — no
mocks, no MagicMock/AsyncMock).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.runtime.message_bus import MessageBus
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.services.inter_agent_messaging import InterAgentMessaging
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.runtime.session import Session
from reyn.runtime.task_types import CurrentTask
from tests._support.agent_session import make_session

# ---------------------------------------------------------------------------
# ① MessageBus.request respects current_task — real Session, public surface
# ---------------------------------------------------------------------------


def _session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


@pytest.mark.asyncio
async def test_request_keeps_polling_while_current_task_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: THE load-bearing witness ① — bus.request() must NOT return
    early with just the 'delegated' ack while current_task is set (mirrors
    router_loop.py's async-dispatch block, which sets this right before
    ending the turn to let a delegation run). Before this PR,
    MessageBus._is_quiescent checked inbox.empty() only, so request()
    would have returned instantly (elapsed ~= 0) with the ack as if it
    were the final answer. Same monkeypatch-the-turn-handler pattern
    test_transport_ref.py's own MessageBus tests use (real Session, real
    MessageBus, no LLM required)."""
    from reyn.runtime.transport import McpRef

    session = _session(tmp_path)

    async def _fake_handle_inbox_text(self: Session, text: str, *, chain_id: str) -> None:
        await self._put_outbox(OutboxMessage(kind="agent", text="delegated, standby"))
        self.current_task = CurrentTask()  # mirrors router_loop.py's async-dispatch SET

    monkeypatch.setattr(Session, "_handle_inbox_text", _fake_handle_inbox_text)

    bus = MessageBus()
    start = asyncio.get_event_loop().time()
    replies = await bus.request(
        session, kind="user", payload={"text": "hello"},
        reply_to=McpRef(request_id="test-p1prime"), timeout=0.1,
    )
    elapsed = asyncio.get_event_loop().time() - start

    # The turn DID run and its ack WAS collected...
    assert any(r.text == "delegated, standby" for r in replies)
    # ...but request() did not return the instant the ack landed — it kept
    # polling for the full timeout window because current_task was never
    # cleared (nothing in this test clears it — the real clear point is
    # handle_agent_response, covered by ② below).
    assert elapsed >= 0.1


# ---------------------------------------------------------------------------
# ② handle_agent_response's set/clear semantics — real InterAgentMessaging
# ---------------------------------------------------------------------------


def _build_handler(
    tmp_path: Path,
    *,
    agent_name: str = "specialist",
    router_actions: "list | None" = None,
    dispatch_task_settled: "Callable[[str, dict], Any] | None" = None,
) -> "tuple[InterAgentMessaging, dict[str, Any], ChainManager]":
    """Real InterAgentMessaging wired with plain stub callbacks — same
    pattern test_a2a_handler_invariants.py's own `_build_handler` uses.
    `state["current_task"]` mirrors Session.current_task via the same
    get/set-callable injection Session itself uses in production. Also
    returns the real `ChainManager` (#5654 fix-forward's own tests need
    it to `.register()`/`.mark_cancel_requested()` a real task-shaped
    chain before driving `handle_agent_response`) — existing callers
    ignore the third element."""
    state_log = StateLog(tmp_path / "state.wal")
    event_store = EventStore(tmp_path / "events")
    event_log = EventLog(subscribers=[event_store])
    journal = SnapshotJournal(
        agent_name=agent_name, snapshot_path=tmp_path / "snap.json", state_log=state_log,
    )
    chain_manager = ChainManager(
        journal=journal, events=event_log, chain_timeout_seconds=0.0, max_hop_depth=3,
    )

    state: dict[str, Any] = {"current_task": None, "outbox": [], "history": []}
    _router_call_count = [0]
    _actions = router_actions or []

    async def _run_router_loop(text: str, chain_id: str) -> None:
        idx = _router_call_count[0]
        _router_call_count[0] += 1
        if idx < len(_actions):
            await _actions[idx](text, chain_id, state)

    async def _put_outbox(msg: OutboxMessage) -> None:
        state["outbox"].append(msg)

    def _append_history(
        role: str, text: str, ts: str, meta: dict, spillability=None,
    ) -> None:
        state["history"].append({"role": role, "text": text, "ts": ts, "meta": meta})

    async def _handle_chat_limit_checkpoint(**kwargs):  # type: ignore[no-untyped-def]
        from reyn.runtime.limits.limit_handler import LimitDecision
        return LimitDecision(allow_continue=True, extension=0.0, reason="test-allow")

    async def _send_request_callback(to, from_agent, request, depth, chain_id) -> None:
        pass

    async def _send_response_callback(
        to, from_agent, response, depth, chain_id, responder_sid=None, to_sid=None,
    ) -> None:
        pass

    async def _on_chain_timeout_fire(chain_id: str) -> None:
        pass

    handler = InterAgentMessaging(
        event_log=event_log,
        chain_manager=chain_manager,
        agent_name=agent_name,
        max_hop_depth=3,
        safety_extensions={},
        output_language="en",
        append_history=_append_history,
        put_outbox=_put_outbox,
        handle_chat_limit_checkpoint=_handle_chat_limit_checkpoint,
        run_router_loop=_run_router_loop,
        reset_router_turn_counter=lambda: None,
        send_request_callback=_send_request_callback,
        send_response_callback=_send_response_callback,
        on_chain_timeout_fire=_on_chain_timeout_fire,
        emit_router_cap_exhausted_fn=lambda exc, *, chain_id, **_kw: asyncio.sleep(0),
        get_router_loop_delegations=lambda: None,
        set_router_loop_delegations=lambda v: None,
        get_router_loop_agent_replies=lambda: None,
        set_router_loop_agent_replies=lambda v: None,
        get_current_task=lambda: state["current_task"],
        set_current_task=lambda v: state.update({"current_task": v}),
        dispatch_task_settled=dispatch_task_settled,
    )
    return handler, state, chain_manager


@pytest.mark.asyncio
async def test_normal_completion_clears_current_task(tmp_path: Path) -> None:
    """Tier 2: witness ② core case — the router loop produces a real final
    answer (pushes to outbox, no further delegation) → current_task clears.
    This is the state MessageBus._is_quiescent needs to see True again so
    the requester's poll loop picks up the real answer, not the ack."""
    async def _final_answer(text, chain_id, state):
        state["outbox"].append(OutboxMessage(kind="agent", text="the real answer", meta={}))

    handler, state, _chains = _build_handler(tmp_path, router_actions=[_final_answer])
    state["current_task"] = CurrentTask()  # simulates router_loop.py's prior SET

    await handler.handle_agent_response({
        "from_agent": "peer", "response": "peer's reply", "depth": 1,
        "chain_id": "chain-not-registered",
    })

    assert state["current_task"] is None
    assert any(m.text == "the real answer" for m in state["outbox"])


@pytest.mark.asyncio
async def test_redelegation_during_settle_is_not_wiped(tmp_path: Path) -> None:
    """Tier 2: THE multi-round-chain correctness witness — if processing
    this response ITSELF triggers a new delegation (the router stub sets a
    NEW current_task, mirroring router_loop.py's async-dispatch block firing
    again), the finally clause must NOT wipe it out. An unconditional
    `finally: current_task = None` would break exactly this case."""
    fresh_task = CurrentTask()

    async def _redelegates(text, chain_id, state):
        state["current_task"] = fresh_task  # mirrors host.mark_task_pending()

    handler, state, _chains = _build_handler(tmp_path, router_actions=[_redelegates])
    state["current_task"] = CurrentTask()  # the task being settled

    await handler.handle_agent_response({
        "from_agent": "peer", "response": "peer's reply", "depth": 1,
        "chain_id": "chain-not-registered",
    })

    assert state["current_task"] is fresh_task


@pytest.mark.asyncio
async def test_exception_during_settle_clears_current_task(tmp_path: Path) -> None:
    """Tier 2: lead-coder's required failure-path decision ① — an exception
    raised while processing the settling response must still clear
    current_task (finally, not except-only). Without this, a router error
    on the settle turn leaves current_task marked outstanding forever —
    exactly the 'delegated and can never report quiescent again' failure
    mode lead-coder named as worse than the bug P1' exists to close."""
    async def _boom(text, chain_id, state):
        raise ValueError("simulated router failure")

    handler, state, _chains = _build_handler(tmp_path, router_actions=[_boom])
    state["current_task"] = CurrentTask()

    await handler.handle_agent_response({
        "from_agent": "peer", "response": "peer's reply", "depth": 1,
        "chain_id": "chain-not-registered",
    })

    assert state["current_task"] is None
    assert any("router failed" in m.text for m in state["outbox"])


@pytest.mark.asyncio
async def test_no_reply_marker_path_also_clears_current_task(tmp_path: Path) -> None:
    """Tier 2: the B2-H2 no-reply-marker early-return branch is INSIDE the
    try/finally too — a peer failure surfaced this way must also settle
    current_task, not just the try/except's own two branches."""
    from reyn.runtime.services.inter_agent_messaging import _no_reply_marker

    handler, state, _chains = _build_handler(tmp_path)
    state["current_task"] = CurrentTask()

    await handler.handle_agent_response({
        "from_agent": "peer",
        "response": _no_reply_marker("peer", "peer crashed"),
        "depth": 1,
        "chain_id": "chain-not-registered",
    })

    assert state["current_task"] is None


# ---------------------------------------------------------------------------
# Failure-path decision ② — current_task does not survive crash OR rewind
# ---------------------------------------------------------------------------


def test_crash_then_fresh_session_restore_has_no_current_task(tmp_path: Path) -> None:
    """Tier 2: the PROCESS-CRASH path — current_task is deliberately NOT part
    of AgentSnapshot (same 'volatile; None after crash' framing ADR-0040
    gives reply_to), so a fresh process's fresh Session() defaults it to
    None, and restore_state (which never touches current_task) leaves it
    there. A crashed session mid-delegation never resumes 'stuck'."""
    agent_name = "alpha"
    log = StateLog(tmp_path / "wal")
    old_session = make_session(
        agent_name=agent_name, state_log=log, snapshot_path=tmp_path / "snap.json",
    )
    old_session.current_task = CurrentTask()  # mid-delegation when "crash" happens

    # A crash never persists current_task (not in AgentSnapshot) — simulate
    # the recovery snapshot as it would genuinely be reconstructed, carrying
    # no current_task field at all.
    snapshot = AgentSnapshot.empty(agent_name)

    # The real recovery path constructs a BRAND NEW Session in the new
    # process, then calls restore_state on it — not the old, crashed object.
    new_session = make_session(
        agent_name=agent_name, state_log=log, snapshot_path=tmp_path / "snap2.json",
    )
    new_session.restore_state(snapshot)

    assert new_session.current_task is None


@pytest.mark.asyncio
async def test_rewind_clears_current_task_on_the_same_live_session(tmp_path: Path) -> None:
    """Tier 2: THE falsify-verified failure-path decision ② — a REWIND (not
    a crash) keeps the SAME Session object alive; without an explicit clear
    in reset_for_rewind, a mid-delegation current_task would outlive the
    rewind and MessageBus._is_quiescent would report non-quiescent forever
    for a delegation the rewound timeline no longer contains. This is the
    'delegated and never returns' failure lead-coder named as worse than
    P1's own bug — falsify-verified by temporarily removing the clear line
    from reset_for_rewind, confirming this test goes RED, then restoring."""
    log = StateLog(tmp_path / "wal")
    session = make_session(
        agent_name="alpha", state_log=log, snapshot_path=tmp_path / "snap.json",
    )
    session.current_task = CurrentTask()  # mid-delegation before the rewind

    await session.reset_for_rewind()
    session.restore_state(AgentSnapshot.empty("alpha"))

    assert session.current_task is None


# ---------------------------------------------------------------------------
# #5654 fix-forward (architect §1.6, lead-coder review on #5661): a
# prompt-kind task_settled must report the REAL outcome, not a fixed "ok"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_settled_reports_cancelled_when_the_operator_cancelled_it(
    tmp_path: Path,
) -> None:
    """Tier 2: an operator-cancelled prompt task's task_settled must NOT
    say "ok" — before this fix it always did, regardless of
    `cancel_requested_at`, the same silent lie
    task_verbs.py's own `_CANCEL_TASK_DESCRIPTION` disclaims for the
    OPPOSITE direction ("cancelled while the task keeps running").
    `mark_cancel_requested` is the REAL production method `/tasks
    cancel`'s own `cancel_task` op calls (task_verbs.py) — no stand-in."""
    settled: "list[dict]" = []

    async def _dispatch_task_settled(kind: str, payload: dict) -> None:
        settled.append(payload)

    handler, state, chains = _build_handler(
        tmp_path, dispatch_task_settled=_dispatch_task_settled,
    )
    from reyn.runtime.task_types import Requester
    await chains.register(
        chain_id="task-1", depth=1, original_text="please help",
        sender="specialist", waiting_on={"peer"},
        requester=Requester(agent_name="specialist", session_id="main"),
        origin_depth=1, kind="prompt", cancel=lambda: None,
    )
    await chains.mark_cancel_requested("task-1")

    await handler.handle_agent_response({
        "from_agent": "peer", "response": "peer's reply", "depth": 1,
        "chain_id": "task-1",
    })

    (payload,) = settled
    assert payload["status"] == "cancelled", (
        f"an operator-cancelled task must settle as cancelled, not "
        f"{payload['status']!r}"
    )


@pytest.mark.asyncio
async def test_task_settled_reports_ok_when_never_cancelled(tmp_path: Path) -> None:
    """Tier 2: the accept-side sibling — an ordinary, never-cancelled
    prompt task's task_settled still reports "ok" (this fix must not flip
    the default)."""
    settled: "list[dict]" = []

    async def _dispatch_task_settled(kind: str, payload: dict) -> None:
        settled.append(payload)

    handler, state, chains = _build_handler(
        tmp_path, dispatch_task_settled=_dispatch_task_settled,
    )
    from reyn.runtime.task_types import Requester
    await chains.register(
        chain_id="task-2", depth=1, original_text="please help",
        sender="specialist", waiting_on={"peer"},
        requester=Requester(agent_name="specialist", session_id="main"),
        origin_depth=1, kind="prompt", cancel=lambda: None,
    )

    await handler.handle_agent_response({
        "from_agent": "peer", "response": "peer's reply", "depth": 1,
        "chain_id": "task-2",
    })

    (payload,) = settled
    assert payload["status"] == "ok"
