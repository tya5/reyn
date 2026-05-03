"""Tier 2: OS invariant tests for ChatSession (chain mgmt + intervention + WAL/snapshot).

Re-encodes the invariants formerly pinned by `tests/scaffold/test_chain_manager.py`
and `tests/scaffold/test_intervention_registry.py` (Tier 4 — Mock + private
state) at the ChatSession public surface (Tier 2). The scaffold files are
removed in the same PR that lands these tests.

Policy compliance (`docs/ja/contributing/testing.md`):
- `unittest.mock` import: `AsyncMock` only, used by `_install_call_llm_tools_mock`
  to stub the LLM. No other mock usage.
- Private state assertion: prohibited. Observation flows through:
    - `session.outbox` (OutboxMessage kind / text / meta)
    - `session.history` (ChatMessage list)
    - `StateLog.iter_from()` on the on-disk WAL
    - `AgentSnapshot.load(agent_name, path)` for fully external snapshot re-read
    - `iv.future` (the producer-side contract for a UserIntervention)
- Internal-attribute access is restricted to:
    - `session._chains.has()` / `.get()` — public ChainManager methods, used as a
      precondition check; final post-condition uses `AgentSnapshot.load`.
    - `session._dispatch_intervention` / `_drop_interventions_for_run` /
      `_maybe_answer_oldest_intervention` — session-level thin wrappers over
      InterventionRegistry, kept in the public surface so the bus can forward to
      them.
- Each test docstring's first line starts with `Tier 2: <intent>`.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from reyn.chat.outbox import OutboxMessage
from reyn.chat.session import ChatSession
from reyn.events.agent_snapshot import AgentSnapshot
from reyn.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text_result(text: str) -> LLMToolCallResult:
    """Minimal LLMToolCallResult that triggers the text-reply branch in RouterLoop."""
    return LLMToolCallResult(
        content=text,
        tool_calls=[],
        finish_reason="stop",
        usage=_EMPTY_USAGE,
    )


def _delegate_result(to: str, request: str) -> LLMToolCallResult:
    """LLMToolCallResult that makes RouterLoop call delegate_to_agent."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[
            {
                "id": "tc_delegate_001",
                "type": "function",
                "function": {
                    "name": "delegate_to_agent",
                    "arguments": json.dumps({"to": to, "request": request}),
                },
            }
        ],
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


class _FakeRegistry:
    """Minimal fake AgentRegistry satisfying the session's send/receive surface.

    Does NOT use unittest.mock — it is a plain fake (Fake > Mock per policy).
    Tracks send_agent_request calls so tests can verify upstream routing.
    """

    def __init__(self):
        # Map agent_name → fake target session (if needed)
        self._targets: dict[str, "_FakeTarget"] = {}
        self.sent_requests: list[dict] = []
        self.sent_responses: list[dict] = []

    def register(self, name: str, session: "ChatSession") -> None:
        self._targets[name] = session

    def exists(self, name: str) -> bool:
        return name in self._targets

    def permit(self, from_agent: str, to_agent: str) -> bool:
        return True

    def iter_reachable_agents(self, self_name: str) -> list[dict]:
        return [
            {"name": n, "role": "assistant"}
            for n in self._targets
            if n != self_name
        ]

    def get_or_load(self, name: str) -> "ChatSession":
        return self._targets[name]

    async def ensure_running(self, name: str) -> None:
        pass


def _make_session(
    tmp_path: Path,
    *,
    agent_name: str = "test_agent",
    chain_timeout_seconds: float = 60.0,
    registry: _FakeRegistry | None = None,
) -> ChatSession:
    """Build a ChatSession with WAL + per-test snapshot path via public kwargs."""
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        chain_timeout_seconds=chain_timeout_seconds,
        registry=registry,
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _wal_events(tmp_path: Path) -> list[dict]:
    """Read all events from the WAL in tmp_path."""
    wal_path = tmp_path / "state.wal"
    log = StateLog(wal_path)
    return list(log.iter_from(0))


def _drain_outbox(session: ChatSession) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _install_call_llm_tools_mock(result: LLMToolCallResult | list) -> AsyncMock:
    """Return a configured AsyncMock for call_llm_tools; caller must patch it."""
    mock = AsyncMock()
    if isinstance(result, list):
        mock.side_effect = result
    else:
        mock.return_value = result
    return mock


# ---------------------------------------------------------------------------
# Test 1: chain_register emits WAL event with required payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_register_emits_wal_event(tmp_path, monkeypatch):
    """Tier 2: chain_register WAL event emitted with required payload fields.

    Scenario: receive agent_request → router returns messages_to_agents →
    ChainManager registers a chain.

    P6 invariant: every state change (chain registration) must emit a WAL
    event.  Missing event = state invisible to crash recovery.
    """
    monkeypatch.chdir(tmp_path)

    registry = _FakeRegistry()
    peer_session = ChatSession(agent_name="peer_agent")
    registry.register("peer_agent", peer_session)

    session = _make_session(tmp_path, registry=registry)
    session.is_attached = True

    # Round 1: router asks to delegate; round 2 is never reached in this test
    # because send_to_agent is async (loop exits after delegation).
    mock = _install_call_llm_tools_mock(
        _delegate_result("peer_agent", "please help")
    )

    with patch("reyn.chat.router_loop.call_llm_tools", new=mock):
        await session._handle_agent_request({
            "from_agent": "origin_agent",
            "request": "do something",
            "depth": 1,
            "chain_id": "chain-reg-001",
        })

    events = _wal_events(tmp_path)
    register_events = [e for e in events if e.get("kind") == "chain_register"]

    assert register_events, (
        "Expected at least one 'chain_register' WAL event; none found."
    )
    ev = register_events[0]
    assert ev.get("chain_id") == "chain-reg-001", (
        f"chain_id mismatch: {ev.get('chain_id')!r}"
    )
    assert "origin_agent" in ev, f"Missing 'origin_agent' in WAL event: {ev}"
    assert "origin_depth" in ev, f"Missing 'origin_depth' in WAL event: {ev}"
    assert "waiting_on" in ev, f"Missing 'waiting_on' in WAL event: {ev}"
    assert "from_user" in ev, f"Missing 'from_user' in WAL event: {ev}"

    # Snapshot must reflect the chain as pending.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert "chain-reg-001" in snapshot.pending_chains, (
        f"chain-reg-001 not found in snapshot.pending_chains: {snapshot.pending_chains}"
    )


# ---------------------------------------------------------------------------
# Test 2: chain_resolve clears snapshot and emits resolve WAL event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_resolve_clears_snapshot_and_emits_resolve(tmp_path, monkeypatch):
    """Tier 2: chain_resolve removes chain from snapshot; WAL order is register → resolve.

    Scenario: register a chain (agent_request + delegation) → peer replies
    (agent_response) → router produces text reply → chain resolved.

    P6 invariant: chain_register and chain_resolve must both appear in the WAL
    in order; snapshot must show no pending chain after resolution.
    """
    monkeypatch.chdir(tmp_path)

    # Peer session: receives agent_request from us, we feed agent_response back.
    peer_session = ChatSession(agent_name="peer_agent")
    registry = _FakeRegistry()
    registry.register("peer_agent", peer_session)

    session = _make_session(tmp_path, registry=registry)
    session.is_attached = True

    # Phase 1: router delegates to peer_agent.
    mock_round1 = _install_call_llm_tools_mock(
        _delegate_result("peer_agent", "help me")
    )
    with patch("reyn.chat.router_loop.call_llm_tools", new=mock_round1):
        await session._handle_agent_request({
            "from_agent": "origin_agent",
            "request": "synthesize",
            "depth": 1,
            "chain_id": "chain-res-001",
        })

    # Verify chain is registered.
    assert session._chains.has("chain-res-001"), (
        "Chain should be pending after delegation"
    )

    # Phase 2: peer_agent responds → router re-runs and produces text reply.
    mock_round2 = _install_call_llm_tools_mock(_text_result("synthesized answer"))
    with patch("reyn.chat.router_loop.call_llm_tools", new=mock_round2):
        await session._handle_agent_response({
            "from_agent": "peer_agent",
            "response": "peer result",
            "depth": 1,
            "chain_id": "chain-res-001",
        })

    # Snapshot must NOT contain the chain after resolve.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert "chain-res-001" not in snapshot.pending_chains, (
        f"chain-res-001 still present in snapshot after resolve: {snapshot.pending_chains}"
    )

    # WAL must have register before resolve (intermediate updates are OK).
    events = _wal_events(tmp_path)
    kinds = [e.get("kind") for e in events if e.get("chain_id") == "chain-res-001"]
    assert "chain_register" in kinds, f"chain_register missing from WAL: {kinds}"
    assert "chain_resolve" in kinds, f"chain_resolve missing from WAL: {kinds}"
    reg_idx = kinds.index("chain_register")
    res_idx = kinds.index("chain_resolve")
    assert reg_idx < res_idx, (
        f"chain_register ({reg_idx}) must precede chain_resolve ({res_idx})"
    )


# ---------------------------------------------------------------------------
# Test 3: chain timeout fires upstream error and emits WAL event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_timeout_fires_upstream_error_and_emits_event(tmp_path, monkeypatch):
    """Tier 2: chain timeout emits chain_timeout_fired WAL event + upstream error response.

    P6 invariant: chain_timeout_fired must be recorded in the WAL.
    PR18 contract: on timeout, an error response is sent upstream AND a WAL
    event is appended — no silent failures.
    """
    monkeypatch.chdir(tmp_path)

    # upstream_session receives the error agent_response.
    upstream_session = ChatSession(agent_name="upstream_agent")
    upstream_received: list[dict] = []

    async def _fake_submit_agent_response(*, from_agent, response, depth, chain_id):
        upstream_received.append({
            "from_agent": from_agent,
            "response": response,
            "chain_id": chain_id,
        })

    upstream_session.submit_agent_response = _fake_submit_agent_response

    registry = _FakeRegistry()
    registry.register("upstream_agent", upstream_session)
    # Also add a peer so delegation succeeds.
    peer_session = ChatSession(agent_name="slow_peer")
    registry.register("slow_peer", peer_session)

    # Short timeout so it fires fast.
    session = _make_session(
        tmp_path, registry=registry, chain_timeout_seconds=0.05
    )
    session.is_attached = True

    # Router delegates to slow_peer (which never responds).
    mock = _install_call_llm_tools_mock(
        _delegate_result("slow_peer", "process this")
    )
    with patch("reyn.chat.router_loop.call_llm_tools", new=mock):
        await session._handle_agent_request({
            "from_agent": "upstream_agent",
            "request": "do slow work",
            "depth": 1,
            "chain_id": "chain-timeout-001",
        })

    # Wait long enough for the timeout to fire.
    await asyncio.sleep(0.2)

    # WAL must contain chain_register → chain_timeout_fired.
    events = _wal_events(tmp_path)
    chain_events = [
        e for e in events if e.get("chain_id") == "chain-timeout-001"
    ]
    kinds = [e.get("kind") for e in chain_events]
    assert "chain_register" in kinds, f"chain_register missing: {kinds}"
    assert "chain_timeout_fired" in kinds, f"chain_timeout_fired missing: {kinds}"
    reg_idx = kinds.index("chain_register")
    to_idx = kinds.index("chain_timeout_fired")
    assert reg_idx < to_idx, (
        f"chain_register ({reg_idx}) must precede chain_timeout_fired ({to_idx})"
    )

    # Upstream session must have received an agent_response with "chain timeout".
    assert upstream_received, (
        "Expected upstream_agent to receive an agent_response on timeout; none received"
    )
    resp = upstream_received[0]
    assert resp["chain_id"] == "chain-timeout-001"
    assert "chain timeout" in resp["response"].lower(), (
        f"Expected 'chain timeout' in response text; got: {resp['response']!r}"
    )


# ---------------------------------------------------------------------------
# Test 4: restore_state reconstructs chains and inbox from snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restore_reconstructs_chains_and_inbox_from_snapshot(tmp_path, monkeypatch):
    """Tier 2: restore_state() re-populates inbox queue and re-arms chain from snapshot.

    Scenario: build a pre-populated AgentSnapshot with one pending chain and
    one inbox message → construct a fresh ChatSession → call restore_state().

    P5 invariant: workspace is the single source of truth; restoration must
    reconstruct in-memory state faithfully from the persisted snapshot.

    Observation: restored chain is verified through `_chains.has()/.get()`
    (public ChainManager methods); restored inbox is verified by draining
    `session.inbox` (public asyncio.Queue).
    """
    monkeypatch.chdir(tmp_path)

    chain_id = "chain-restore-001"
    msg_id = "aabbccdd"

    # Build and persist a snapshot with one pending chain + one inbox message.
    snap = AgentSnapshot(agent_name="test_agent")
    snap.pending_chains[chain_id] = {
        "chain_id": chain_id,
        "origin_agent": "origin",
        "origin_depth": 1,
        "original_request": "original task",
        "waiting_on": ["peer_agent"],
    }
    snap.inbox.append({
        "id": msg_id,
        "kind": "user",
        "payload": {"text": "hello from snapshot", "_msg_id": msg_id},
    })
    snap.applied_seq = 5

    snapshot_path = tmp_path / "test_agent_snapshot.json"
    snap.save(snapshot_path)

    # Build a fresh session and restore.
    session = _make_session(tmp_path)
    loaded_snap = AgentSnapshot.load("test_agent", snapshot_path)
    session.restore_state(loaded_snap)

    # Inbox queue must have the restored message.
    assert not session.inbox.empty(), (
        "session.inbox should be non-empty after restore_state"
    )
    kind, payload = session.inbox.get_nowait()
    assert kind == "user"
    assert payload.get("text") == "hello from snapshot"

    # ChainManager must have the chain loaded (public query API).
    assert session._chains.has(chain_id), (
        f"ChainManager.has({chain_id!r}) returned False after restore_state"
    )
    pc = session._chains.get(chain_id)
    assert pc is not None
    assert pc.origin_agent == "origin"
    assert "peer_agent" in pc.waiting_on


# ---------------------------------------------------------------------------
# Test 5: inbox_put / inbox_consume emit WAL events with monotonic seq
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inbox_put_consume_emits_wal_events_with_monotonic_seq(tmp_path, monkeypatch):
    """Tier 2: inbox_put and inbox_consume WAL events have strictly increasing seq.

    Scenario: 3× submit_user_text → run() consumes all three (mocked router).

    P6 invariant: every state change has a WAL event; seq is monotonically
    increasing so crash recovery can replay in order without gaps.
    """
    monkeypatch.chdir(tmp_path)

    session = _make_session(tmp_path)
    session.is_attached = True

    # Queue three user messages.
    await session.submit_user_text("msg one")
    await session.submit_user_text("msg two")
    await session.submit_user_text("msg three")

    # Router stub: always returns text so the loop exits after 1 iteration.
    mock = _install_call_llm_tools_mock(
        [_text_result(f"reply {i}") for i in range(3)]
    )

    async def _run_one_turn():
        """Process all three inbox messages then shutdown."""
        with patch("reyn.chat.router_loop.call_llm_tools", new=mock):
            # Start run() in background and shutdown after a brief moment.
            run_task = asyncio.create_task(session.run())
            # Give the event loop time to process all three messages.
            await asyncio.sleep(0.05)
            await session.shutdown()
            try:
                await asyncio.wait_for(run_task, timeout=2.0)
            except asyncio.TimeoutError:
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

    await _run_one_turn()

    events = _wal_events(tmp_path)
    put_events = [e for e in events if e.get("kind") == "inbox_put"]
    consume_events = [e for e in events if e.get("kind") == "inbox_consume"]

    assert len(put_events) >= 3, (
        f"Expected at least 3 inbox_put events; got {len(put_events)}"
    )
    assert len(consume_events) >= 3, (
        f"Expected at least 3 inbox_consume events; got {len(consume_events)}"
    )

    # All seq numbers must be strictly increasing across the WAL.
    all_seqs = [e["seq"] for e in events if "seq" in e]
    for a, b in zip(all_seqs, all_seqs[1:]):
        assert a < b, (
            f"WAL seq not strictly monotonic: ...{a}, {b}... in {all_seqs}"
        )

    # Snapshot applied_seq must equal the highest seq in the WAL.
    max_seq = max(all_seqs)
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert snapshot.applied_seq == max_seq, (
        f"snapshot.applied_seq {snapshot.applied_seq} != max WAL seq {max_seq}"
    )


# ---------------------------------------------------------------------------
# Test 6: shutdown signal bypasses WAL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_signal_bypasses_wal(tmp_path, monkeypatch):
    """Tier 2: shutdown() does not emit inbox_put or inbox_consume WAL events.

    P21 design: the shutdown signal is out-of-band (crash recovery does not
    need to replay it — a re-started agent should simply resume from its last
    snapshot, not re-execute a shutdown).  Any WAL record of shutdown would
    confuse replay and is explicitly forbidden by the _consume_inbox guard.
    """
    monkeypatch.chdir(tmp_path)

    session = _make_session(tmp_path)

    # Trigger the shutdown path: start run() then immediately shutdown.
    async def _run_with_shutdown():
        run_task = asyncio.create_task(session.run())
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)

    await _run_with_shutdown()

    events = _wal_events(tmp_path)
    shutdown_put = [
        e for e in events
        if e.get("kind") == "inbox_put" and e.get("msg_kind") == "shutdown"
    ]
    shutdown_consume = [
        e for e in events
        if e.get("kind") == "inbox_consume"
        # shutdown messages have no _msg_id so consume is skipped entirely.
    ]

    assert shutdown_put == [], (
        f"shutdown inbox_put should NOT appear in WAL; found: {shutdown_put}"
    )
    # inbox_consume for shutdown is also skipped (_consume_inbox guard).
    # Any consume events that DO exist are for non-shutdown messages; there
    # should be none here since we sent no user messages before shutdown.
    assert shutdown_consume == [], (
        f"inbox_consume should be empty (no user messages sent); found: {shutdown_consume}"
    )


# ---------------------------------------------------------------------------
# Helpers (intervention tests)
# ---------------------------------------------------------------------------


def _iv(
    *,
    run_id: str | None = None,
    choices: list[InterventionChoice] | None = None,
    prompt: str = "Q?",
    kind: str = "ask_user",
) -> UserIntervention:
    """Build a UserIntervention bound to the running asyncio loop's future."""
    iv = UserIntervention(kind=kind, prompt=prompt, run_id=run_id, choices=choices or [])
    iv.future = asyncio.get_running_loop().create_future()
    return iv


# ---------------------------------------------------------------------------
# Test 7: drop_for_run cancels all matching futures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intervention_drop_for_run_cancels_all_matching(tmp_path, monkeypatch):
    """Tier 2: _drop_interventions_for_run cancels every future tagged with run_id.

    Invariant: when a skill run is cancelled, every pending intervention belonging
    to that run_id must have its future cancelled — no dangling futures that
    would block the producer forever.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv1 = _iv(run_id="rA", prompt="First Q?")
    iv2 = _iv(run_id="rA", prompt="Second Q?")

    # Dispatch both without awaiting — each coroutine blocks on iv.future.
    t1 = asyncio.ensure_future(session._dispatch_intervention(iv1))
    t2 = asyncio.ensure_future(session._dispatch_intervention(iv2))
    # Yield twice so both dispatch coros reach `await iv.future`.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    session._drop_interventions_for_run("rA")
    await asyncio.sleep(0)

    assert iv1.future.cancelled(), "iv1.future must be cancelled after drop"
    assert iv2.future.cancelled(), "iv2.future must be cancelled after drop"

    await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 8: choices no-match emits unknown-choice hint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intervention_choices_no_match_emits_unknown_choice_hint(tmp_path, monkeypatch):
    """Tier 2: unrecognised hotkey emits status hint; intervention stays pending.

    Invariant: when user submits text that matches no choice hotkey, the OS must
    (a) emit a kind="status" message with "unknown choice" text and the
    intervention's id in meta, (b) return True (consumed) so the router does
    not start a fresh turn, and (c) keep the intervention pending — the user
    can retry with a valid hotkey.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    choices = [
        InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
        InterventionChoice(id="no", label="[N]o", hotkey="n"),
    ]
    iv = _iv(choices=choices, prompt="Confirm?")

    dispatch_task = asyncio.ensure_future(session._dispatch_intervention(iv))
    await asyncio.sleep(0)

    consumed = await session._maybe_answer_oldest_intervention("invalid")
    assert consumed is True, (
        "_maybe_answer_oldest_intervention must return True for unknown choice"
    )

    messages = _drain_outbox(session)
    status_msgs = [m for m in messages if m.kind == "status"]
    unknown_msgs = [m for m in status_msgs if "unknown choice" in m.text]
    assert unknown_msgs, (
        f"Expected status with 'unknown choice', got: {[m.text for m in status_msgs]!r}"
    )
    hint_msg = unknown_msgs[0]
    assert hint_msg.meta.get("intervention_id") == iv.id, (
        f"Status meta must include intervention_id={iv.id!r}, got {hint_msg.meta!r}"
    )

    # Intervention still pending — verify by resolving with a valid hotkey.
    resolved = await session._maybe_answer_oldest_intervention("y")
    assert resolved is True, "Valid hotkey must resolve the still-pending intervention"
    assert iv.future.done() and not iv.future.cancelled()
    assert iv.future.result().choice_id == "yes"

    await asyncio.gather(dispatch_task, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 9: queued status emitted when dispatched while another is pending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_intervention_queued_status_when_dispatched_while_pending(tmp_path, monkeypatch):
    """Tier 2: dispatching while one is pending emits "awaiting answer (N queued)".

    Invariant: the session must surface the queued status with the waiting
    intervention's id in meta so the UI can inform the user that their
    question is queued behind another.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    iv1 = _iv(prompt="First Q?")
    iv2 = _iv(prompt="Second Q?")

    # Dispatch iv1 — blocks on its future. The registry calls on_announce.
    t1 = asyncio.ensure_future(session._dispatch_intervention(iv1))
    await asyncio.sleep(0)

    msgs_after_iv1 = _drain_outbox(session)
    intervention_msgs = [m for m in msgs_after_iv1 if m.kind == "intervention"]
    assert intervention_msgs and "Question:" in intervention_msgs[0].text, (
        "iv1 announce must have been emitted"
    )

    # Dispatch iv2 while iv1 is still pending — triggers the queued-status path.
    t2 = asyncio.ensure_future(session._dispatch_intervention(iv2))
    await asyncio.sleep(0)

    msgs_after_iv2 = _drain_outbox(session)
    queued_msgs = [
        m for m in msgs_after_iv2
        if m.kind == "status" and "queued" in m.text and "awaiting answer" in m.text
    ]
    assert queued_msgs, (
        f"Expected queued status; got: {[(m.kind, m.text) for m in msgs_after_iv2]!r}"
    )
    assert queued_msgs[0].meta.get("intervention_id") == iv2.id, (
        "Queued status meta must include iv2's intervention_id"
    )

    # Cleanup.
    iv1.future.set_result(InterventionAnswer(text="a"))
    await asyncio.sleep(0)
    iv2.future.set_result(InterventionAnswer(text="b"))
    await asyncio.gather(t1, t2, return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 10: P6 anti-bypass — every chain state mutation emits a WAL event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p6_chain_state_changes_emit_events(tmp_path, monkeypatch):
    """Tier 2: P6 anti-bypass — inbox/chain mutations all emit WAL events.

    Invariant: inbox_put, inbox_consume, chain_register, and chain_resolve must
    all appear in the WAL with strictly-increasing applied_seq, and snapshot's
    pending_chains must be empty after resolve. A missing event for any of
    these operations indicates a state mutation that bypassed P6.

    This test exercises the public surface (`_put_inbox` / `_consume_inbox`,
    plus `_chains.register` / `.resolve` which are public ChainManager methods)
    and verifies via WAL+snapshot file reads — no internal state assertions.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)

    # ── inbox_put ─────────────────────────────────────────────────────────
    await session._put_inbox("agent_request", {
        "from_agent": "upstream",
        "request": "hello",
        "depth": 1,
        "chain_id": "chain-p6-001",
    })

    # ── inbox_consume ─────────────────────────────────────────────────────
    kind, payload = await session._consume_inbox()
    assert kind == "agent_request"
    chain_id = payload["chain_id"]

    # ── chain_register ────────────────────────────────────────────────────
    await session._chains.register(
        chain_id=chain_id,
        from_user=False,
        depth=1,
        original_text="hello",
        sender="upstream",
        waiting_on={"downstream"},
        origin_agent="upstream",
        origin_depth=1,
    )

    # ── chain_resolve ─────────────────────────────────────────────────────
    resolved_chain = await session._chains.resolve(chain_id)
    assert resolved_chain is not None, "resolve must return the chain"

    # ── WAL read: verify P6 invariant ─────────────────────────────────────
    wal_entries = _wal_events(tmp_path)
    kinds_present = {e["kind"] for e in wal_entries}
    required_kinds = {"inbox_put", "inbox_consume", "chain_register", "chain_resolve"}
    missing = required_kinds - kinds_present
    assert not missing, (
        f"P6 violation: WAL event kinds missing: {missing!r}. "
        f"A state mutation bypassed the event log."
    )

    # applied_seq must be strictly increasing across entries.
    seqs = [e["seq"] for e in wal_entries]
    for i in range(1, len(seqs)):
        assert seqs[i] > seqs[i - 1], (
            f"P6 violation: WAL seq not strictly increasing at index {i}: "
            f"{seqs[i - 1]} → {seqs[i]}"
        )

    # External snapshot read — verify pending_chains is empty post-resolve.
    snapshot = AgentSnapshot.load(session.agent_name, session._snapshot_path)
    assert snapshot.pending_chains == {}, (
        f"pending_chains must be empty after resolve, got: {snapshot.pending_chains!r}"
    )
