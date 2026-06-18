"""Tier 2: OS invariant tests for Session (chain mgmt + intervention + WAL/snapshot).

Re-encodes the invariants formerly pinned by `tests/scaffold/test_chain_manager.py`
and `tests/scaffold/test_intervention_registry.py` (Tier 4 — Mock + private
state) at the Session public surface (Tier 2). The scaffold files are
removed in the same PR that lands these tests.

Policy compliance (`docs/deep-dives/contributing/testing.ja.md`):
- LLM is faked via a real async callable stub (Tier 2c policy).  No
  unittest.mock.AsyncMock / patch usage.
- Private state assertion: prohibited. Observation flows through:
    - `session.outbox` (OutboxMessage kind / text / meta)
    - `session.history` (ChatMessage list)
    - `StateLog.iter_from()` on the on-disk WAL
    - `AgentSnapshot.load(agent_name, path)` for fully external snapshot re-read
    - `iv.future` (the producer-side contract for a UserIntervention)
- Internal-attribute access is restricted to:
    - `session.chains.has()` / `.get()` — public ChainManager methods, used as a
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

import pytest
from _async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper

from reyn.chat.session import Session
from reyn.config import OnLimitConfig, SafetyConfig, TimeoutConfig
from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
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
    """LLMToolCallResult that makes RouterLoop call delegate_to_agent via invoke_action wrapper."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[
            {
                "id": "tc_delegate_001",
                "type": "function",
                "function": {
                    "name": "invoke_action",
                    "arguments": json.dumps({
                        "action_name": "multi_agent__delegate",
                        "args": {"to": to, "request": request},
                    }),
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

    def register(self, name: str, session: "Session") -> None:
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

    def get_or_load(self, name: str) -> "Session":
        return self._targets[name]

    async def ensure_running(self, name: str) -> None:
        pass


def _make_session(
    tmp_path: Path,
    *,
    agent_name: str = "test_agent",
    chain_timeout_seconds: float = 60.0,
    registry: _FakeRegistry | None = None,
    on_limit: OnLimitConfig | None = None,
) -> Session:
    """Build a Session with WAL + per-test snapshot path via public kwargs.

    issue #254 Phase 1: register a placeholder listener so the registry's
    ``enforce_listener_presence=True`` short-circuit does not fire — these
    tests exercise the chat-side intervention flow and resolve answers
    via ``deliver_answer`` themselves.

    Tests that want the legacy "abort immediately on limit hit" behaviour
    (= chain-timeout fires + emits chain_timeout_fired) pass
    ``on_limit=OnLimitConfig(mode="unattended")`` explicitly; otherwise
    the default ``interactive`` + ``ask_timeout=0`` is applied and the
    registered listener keeps the prompt awaiting forever.
    """
    safety_kwargs = {"timeout": TimeoutConfig(chain_seconds=chain_timeout_seconds)}
    if on_limit is not None:
        safety_kwargs["on_limit"] = on_limit
    safety = SafetyConfig(**safety_kwargs)
    session = Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        safety=safety,
        registry=registry,
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
        # #1657: these chain-invariant tests stub the LLM with the universal-
        # category tool-call shape (_delegate_result = invoke_action wrapper),
        # so pin the scheme to match the stub. They assert WAL/chain behaviour,
        # not the tool-use scheme; the default is now enumerate-all (which
        # interprets FLAT native tool_calls, not the wrapper) so an unpinned
        # session would not dispatch the stub's delegate → no chain_register.
        chat_tool_use_scheme="universal-category",
    )
    session.register_intervention_listener("test")
    return session


def _wal_events(tmp_path: Path) -> list[dict]:
    """Read all events from the WAL in tmp_path."""
    wal_path = tmp_path / "state.wal"
    log = StateLog(wal_path)
    return list(log.iter_from(0))


def _drain_outbox(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def _make_llm_stub(result: LLMToolCallResult | list):
    """Return a real async callable that mimics call_llm_tools.

    Replacing AsyncMock per testing policy (Tier 2c): use a real callable
    so that signature drift in call_llm_tools raises TypeError at the call
    site rather than silently succeeding.
    """
    if isinstance(result, list):
        results = list(result)
        call_count = [0]

        async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(results):
                return results[idx]
            return results[-1]

        return _stub
    else:
        async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
            return result

        return _stub


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
    peer_session = Session(agent_name="peer_agent")
    registry.register("peer_agent", peer_session)

    session = _make_session(tmp_path, registry=registry,
                            on_limit=OnLimitConfig(mode="unattended"))
    session.is_attached = True

    # Round 1: router asks to delegate; round 2 is never reached in this test
    # because send_to_agent is async (loop exits after delegation).
    stub = _make_llm_stub(_delegate_result("peer_agent", "please help"))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub)

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
    peer_session = Session(agent_name="peer_agent")
    registry = _FakeRegistry()
    registry.register("peer_agent", peer_session)

    session = _make_session(tmp_path, registry=registry,
                            on_limit=OnLimitConfig(mode="unattended"))
    session.is_attached = True

    # Phase 1: router delegates to peer_agent.
    stub_round1 = _make_llm_stub(_delegate_result("peer_agent", "help me"))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub_round1)
    await session._handle_agent_request({
        "from_agent": "origin_agent",
        "request": "synthesize",
        "depth": 1,
        "chain_id": "chain-res-001",
    })

    # Verify chain is registered.
    assert session.chains.has("chain-res-001"), (
        "Chain should be pending after delegation"
    )

    # Phase 2: peer_agent responds → router re-runs and produces text reply.
    stub_round2 = _make_llm_stub(_text_result("synthesized answer"))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub_round2)
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
    upstream_session = Session(agent_name="upstream_agent")
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
    peer_session = Session(agent_name="slow_peer")
    registry.register("slow_peer", peer_session)

    # Short timeout so it fires fast. Use unattended mode so the chain
    # timeout fires as an abort + emits chain_timeout_fired event, rather
    # than awaiting the new ``interactive`` default's user prompt that
    # nothing would resolve in this test (the registered placeholder
    # listener stays silent under ``ask_timeout=0``).
    session = _make_session(
        tmp_path,
        registry=registry,
        chain_timeout_seconds=0.05,
        on_limit=OnLimitConfig(mode="unattended"),
    )
    session.is_attached = True

    # Router delegates to slow_peer (which never responds).
    stub = _make_llm_stub(_delegate_result("slow_peer", "process this"))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub)
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
    one inbox message → construct a fresh Session → call restore_state().

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
    assert session.chains.has(chain_id), (
        f"ChainManager.has({chain_id!r}) returned False after restore_state"
    )
    pc = session.chains.get(chain_id)
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
    # Real async callable per Tier 2c policy (no unittest.mock.AsyncMock).
    stub = _make_llm_stub([_text_result(f"reply {i}") for i in range(3)])
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub)

    async def _run_one_turn():
        """Process all three inbox messages then shutdown."""
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

    # P6 invariant: each submitted message must produce a WAL event.
    # The exact count equals the number of messages submitted (3).
    assert put_events, f"inbox_put WAL events must be present; got {len(put_events)}"
    assert consume_events, f"inbox_consume WAL events must be present; got {len(consume_events)}"

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
    # Wait until both dispatches have registered (#1751: each fsyncs its WAL
    # append via to_thread, so a fixed sleep(0) no longer covers them).
    await wait_until(lambda: len(session.interventions.list_active()) >= 2)

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
    # Wait until the dispatch registered the pending iv (#1751: WAL append now
    # fsyncs via to_thread; sleep(0) would answer before the iv is pending).
    await wait_until(lambda: bool(session.interventions.list_active()))

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
    # Wait until iv1 is registered/announced (#1751: WAL append now fsyncs via
    # to_thread; sleep(0) would drain the outbox before the prompt is emitted).
    await wait_until(lambda: bool(session.interventions.list_active()))

    msgs_after_iv1 = _drain_outbox(session)
    intervention_msgs = [m for m in msgs_after_iv1 if m.kind == "intervention"]
    assert intervention_msgs and "Question:" in intervention_msgs[0].text, (
        "iv1 announce must have been emitted"
    )

    # Dispatch iv2 while iv1 is still pending — triggers the queued-status path.
    t2 = asyncio.ensure_future(session._dispatch_intervention(iv2))
    # Wait until iv2 is registered (both pending) so its queued-status announce
    # has been emitted (#1751: WAL append now fsyncs via to_thread).
    await wait_until(lambda: len(session.interventions.list_active()) >= 2)

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
    await session.chains.register(
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
    resolved_chain = await session.chains.resolve(chain_id)
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


# ---------------------------------------------------------------------------
# F6/F7 fix: empty router reply on agent_request → structured marker upstream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_request_empty_router_reply_sends_marker_upstream(
    tmp_path, monkeypatch
):
    """Tier 2: when RouterLoop produces an empty-stop response during an
    inbound agent_request, the upstream agent receives a NON-EMPTY reply
    (F6/F7 fix + ADR-0021 Option F).

    Pre-fix dogfood scenario 2 (multi-agent delegate batch 1): the
    specialist's RouterLoop returned with `agent_replies = []` (e.g. from
    max_iterations exhaustion or empty content), and `_handle_agent_request`
    forwarded `response=""` upstream. The upstream LLM interpreted the
    empty string as "in-progress" and re-delegated until the router cap
    fired (= F7 cascade). F6/F7 fix: synthesise a clear text marker.

    ADR-0021 Option F (2026-05-04): when finish_reason=stop and content is
    empty, RouterLoop itself emits a user-visible failure message (Option F
    path) rather than an empty put_outbox. The failure message is captured
    by _router_loop_agent_replies and forwarded upstream as the reply.
    The upstream therefore always receives a non-empty, human-readable
    failure description — the F6 invariant (no empty upstream reply) is
    preserved via a different mechanism.
    """
    monkeypatch.chdir(tmp_path)

    upstream_session = Session(agent_name="origin_agent")
    upstream_received: list[dict] = []

    async def _fake_submit_agent_response(*, from_agent, response, depth, chain_id):
        upstream_received.append({
            "from_agent": from_agent,
            "response": response,
            "chain_id": chain_id,
        })

    upstream_session.submit_agent_response = _fake_submit_agent_response

    registry = _FakeRegistry()
    registry.register("origin_agent", upstream_session)

    session = _make_session(
        tmp_path, agent_name="specialist", registry=registry
    )
    session.is_attached = True

    # LLM returns empty content (finish_reason="stop", content="").
    # With ADR-0021 Option F, RouterLoop detects this as empty-stop and
    # emits a failure message to the outbox (kind="agent", non-empty text).
    # Session's capture filter picks it up → agent_replies non-empty
    # → failure message forwarded upstream (not the no-reply marker).
    stub = _make_llm_stub(_text_result(""))
    monkeypatch.setattr("reyn.chat.router_loop.call_llm_tools", stub)

    await session._handle_agent_request({
        "from_agent": "origin_agent",
        "request": "what is the recipe?",
        "depth": 1,
        "chain_id": "chain-f6-001",
    })

    assert upstream_received, (
        "Expected origin_agent to receive an agent_response; none received"
    )
    resp = upstream_received[0]
    assert resp["chain_id"] == "chain-f6-001"
    # Core F6 invariant: upstream must NOT receive an empty response.
    assert resp["response"] != "", (
        "F6 regression: upstream received empty response; "
        "Option F should produce a non-empty failure message"
    )
    # Option F: the response must be a meaningful failure description,
    # not an empty or trivially short placeholder.
    assert resp["response"].strip(), (
        f"Upstream reply must be non-blank: {resp['response']!r}"
    )


@pytest.mark.asyncio
async def test_agent_request_router_cap_exhausted_sends_marker_upstream(
    tmp_path, monkeypatch
):
    """Tier 2: RouterCapExceeded during an agent_request handler also
    sends a structured marker upstream (not "") so the upstream chain
    doesn't stall on an ambiguous empty response (F6/F7 fix, exception
    path).

    Triggered by patching `_run_router_loop` to raise RouterCapExceeded
    directly, isolating the fallback path from RouterLoop's internals.
    """
    monkeypatch.chdir(tmp_path)

    upstream_session = Session(agent_name="origin_agent")
    upstream_received: list[dict] = []

    async def _fake_submit_agent_response(*, from_agent, response, depth, chain_id):
        upstream_received.append({
            "from_agent": from_agent,
            "response": response,
            "chain_id": chain_id,
        })

    upstream_session.submit_agent_response = _fake_submit_agent_response

    registry = _FakeRegistry()
    registry.register("origin_agent", upstream_session)

    session = _make_session(
        tmp_path, agent_name="specialist", registry=registry
    )
    session.is_attached = True

    # Force RouterCapExceeded from the handler.
    from reyn.chat.session import RouterCapExceeded

    async def _raise_cap(*args, **kwargs):
        raise RouterCapExceeded(count=3, cap=3, last_reason="loop")

    session._run_router_loop = _raise_cap  # type: ignore[assignment]

    await session._handle_agent_request({
        "from_agent": "origin_agent",
        "request": "anything",
        "depth": 1,
        "chain_id": "chain-f6-cap-001",
    })

    assert upstream_received, (
        "upstream must receive an agent_response on cap exhaustion"
    )
    resp = upstream_received[0]
    assert resp["response"] != ""
    assert "specialist" in resp["response"]
    assert "router retry budget exhausted" in resp["response"].lower(), (
        f"Expected cap-exhausted reason in marker; got: {resp['response']!r}"
    )


# B2-H2 fix: peer _no_reply_marker silently absorbed by LLM → deterministic surfacing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_no_reply_marker_surfaced_to_user_not_absorbed(
    tmp_path, monkeypatch
):
    """Tier 2: when a peer agent returns a `_no_reply_marker`-formatted
    response on a user-initiated chain, the receiving agent must surface the
    failure to the user explicitly via OS-level deterministic message — NOT
    silently absorb it into a polite close like "かしこまりました..." (B2-H2).

    This is the inverse safety: the F6 marker mechanism produces a clear
    failure signal from the OS, but B2-H2 dogfood found that weak LLMs
    read the marker as conversational reply and ignore the failure. We bypass
    the LLM for this specific case.

    Path exercised: `_handle_agent_response` → `chain_id ∉ self._chains`
    branch (user-initiated chain, PR11 path).
    """
    monkeypatch.chdir(tmp_path)
    from reyn.chat.session import _no_reply_marker

    session = _make_session(tmp_path, agent_name="default_agent")
    session.is_attached = True

    # Inject a no-reply marker as if a specialist peer sent it.
    marker = _no_reply_marker("specialist", "router completed without producing a text reply")

    # _run_router_loop must NOT be called — we verify by patching it to raise.
    router_called = []

    async def _should_not_call(*args, **kwargs):
        router_called.append(True)
        raise AssertionError("_run_router_loop should NOT be called for a no-reply marker")

    session._run_router_loop = _should_not_call  # type: ignore[assignment]

    await session._handle_agent_response({
        "from_agent": "specialist",
        "response": marker,
        "depth": 1,
        "chain_id": "chain-b2h2-user-001",
    })

    # Must NOT have called the router loop.
    assert not router_called, "B2-H2 regression: _run_router_loop was called for a marker"

    # Outbox must contain a kind=agent failure message.
    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert agent_msgs, (
        "B2-H2 regression: outbox has no 'agent' message; user was not notified of peer failure"
    )
    # The message must reference the failing peer.
    combined_text = " ".join(m.text for m in agent_msgs)
    assert "specialist" in combined_text, (
        f"B2-H2: expected peer name 'specialist' in user message; got: {combined_text!r}"
    )
    # meta must carry peer_failure=True.
    peer_failure_msgs = [m for m in agent_msgs if m.meta.get("peer_failure")]
    assert peer_failure_msgs, (
        f"B2-H2: no message with meta.peer_failure=True; msgs: {agent_msgs!r}"
    )

    # Chat event log must contain peer_reply_failed_surfaced event (P6 audit).
    chat_event_types = [e.type for e in session._chat_events.all()]
    assert "peer_reply_failed_surfaced" in chat_event_types, (
        f"B2-H2: expected 'peer_reply_failed_surfaced' chat event; got: {chat_event_types!r}"
    )


@pytest.mark.asyncio
async def test_peer_no_reply_marker_forwarded_upstream_in_pending_chain(
    tmp_path, monkeypatch
):
    """Tier 2: when a peer returns a `_no_reply_marker`-formatted response
    and the receiving agent has a *pending chain* (multi-hop relay path),
    the failure is forwarded upstream deterministically without consulting
    the LLM (B2-H2, `_resolve_pending_chain` path).

    Path exercised: `_handle_agent_response` → `chain_id ∈ self._chains`
    branch → `_resolve_pending_chain`.
    """
    monkeypatch.chdir(tmp_path)
    from reyn.chat.session import _no_reply_marker

    # Set up upstream origin agent to capture the forwarded response.
    origin_session = Session(agent_name="origin_agent")
    upstream_received: list[dict] = []

    async def _fake_submit_agent_response(*, from_agent, response, depth, chain_id):
        upstream_received.append({
            "from_agent": from_agent,
            "response": response,
            "chain_id": chain_id,
        })

    origin_session.submit_agent_response = _fake_submit_agent_response

    registry = _FakeRegistry()
    registry.register("origin_agent", origin_session)

    session = _make_session(
        tmp_path, agent_name="relay_agent", registry=registry
    )
    session.is_attached = True

    # Manually register a pending chain: relay_agent is waiting on "specialist"
    # for a request that came from "origin_agent".
    chain_id = "chain-b2h2-relay-001"
    await session.chains.register(
        chain_id=chain_id,
        from_user=False,
        depth=2,
        original_text="what is the recipe?",
        sender="origin_agent",
        waiting_on={"specialist"},
        origin_agent="origin_agent",
        origin_depth=1,
    )

    # Confirm the chain exists.
    assert session.chains.get(chain_id) is not None

    # _run_router_loop must NOT be called.
    router_called = []

    async def _should_not_call(*args, **kwargs):
        router_called.append(True)
        raise AssertionError("_run_router_loop should NOT be called for a marker in pending chain")

    session._run_router_loop = _should_not_call  # type: ignore[assignment]

    # Simulate specialist sending a no-reply marker.
    marker = _no_reply_marker("specialist", "router completed without producing a text reply")

    await session._handle_agent_response({
        "from_agent": "specialist",
        "response": marker,
        "depth": 2,
        "chain_id": chain_id,
    })

    # Must NOT have called the router.
    assert not router_called, "B2-H2 relay regression: _run_router_loop was called for a marker"

    # Chain must be resolved.
    assert session.chains.get(chain_id) is None, (
        "B2-H2 relay: chain should be resolved after marker detection, but it is still pending"
    )

    # Origin agent must have received a forwarded failure message.
    assert upstream_received, (
        "B2-H2 relay: origin_agent received no forwarded response"
    )
    fwd = upstream_received[0]
    assert fwd["chain_id"] == chain_id
    # Forwarded message should mention the failing peer name.
    assert "specialist" in fwd["response"], (
        f"B2-H2 relay: expected 'specialist' in forwarded failure; got: {fwd['response']!r}"
    )
    # Should NOT be a raw marker anymore (the OS translates it to a user-facing message).
    assert "could not produce a reply" not in fwd["response"], (
        f"B2-H2 relay: raw marker was forwarded verbatim; should be localized: {fwd['response']!r}"
    )

    # Chat event log must contain peer_reply_failed_surfaced event (P6 audit).
    chat_event_types = [e.type for e in session._chat_events.all()]
    assert "peer_reply_failed_surfaced" in chat_event_types, (
        f"B2-H2 relay: expected 'peer_reply_failed_surfaced' chat event; got: {chat_event_types!r}"
    )


# ---------------------------------------------------------------------------
# B4-H1 fix: _run_skill_awaitable narrator reply must reach RouterLoop replies
# ---------------------------------------------------------------------------


class _FakeAgent:
    """Real callable fake for Agent — returns a scripted RunResult without LLM."""

    def __init__(self, run_result):
        self._run_result = run_result

    async def run(self, skill, input_artifact, **kwargs):
        return self._run_result


# ---------------------------------------------------------------------------
# FP-0011: post-narrator-removal contract for _run_skill_awaitable
# ---------------------------------------------------------------------------
#
# The previous B4-H1 tests in this section asserted that _run_skill_awaitable
# pushed the narrator's reply into _router_loop_agent_replies and appended
# exactly one history entry. Both behaviours are gone post-FP-0011 — the
# router LLM narrates from the tool-result on its next turn instead. The new
# contract is exercised by Component E (test_post_invoke_skill_narration_*).


@pytest.mark.asyncio
async def test_run_skill_awaitable_returns_status_data_no_outbox(
    tmp_path, monkeypatch
):
    """Tier 2: _run_skill_awaitable (= plan-mode blocking path) returns {status, data} and does NOT push to outbox.

    FP-0011 invariant (replaces B4-H1) + FP-0012 scope clarification:
    _run_skill_awaitable is preserved as the plan-mode blocking call
    site (sequential step execution needs the result inline). Chat-mode
    invoke_skill now uses ``_spawn_skill_for_router`` instead — see
    ``test_spawn_skill_for_router_returns_spawn_ack`` for that contract.

    The blocking awaitable's only side effect on success is the
    skill_run_completed event + accumulate; MUST NOT push to outbox /
    history / _router_loop_agent_replies.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.session as session_mod
    from reyn.core.kernel.runtime import RunResult

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    def _fake_resolve(skill_name):
        return dummy_skill_dir, tmp_path

    def _fake_load_dsl_skill(path, *, skill_root):
        return object()

    monkeypatch.setattr(session_mod, "resolve_skill_path", _fake_resolve)
    monkeypatch.setattr(session_mod, "load_dsl_skill", _fake_load_dsl_skill)

    session = _make_session(tmp_path)
    session.is_attached = True

    fake_result = RunResult(
        data={"reply_text": "カレーレシピを生成しました"},
        status="finished",
    )

    def _fake_build_agent(**kwargs):
        return _FakeAgent(fake_result)

    session._build_agent = _fake_build_agent

    # Arm RouterLoop reply capture so we can confirm it stays empty.
    session._router_loop_agent_replies = []

    history_before = len(session.history)

    spec = {"skill": "direct_llm", "input": {"type": "llm_request", "data": {}}}
    ret = await session._skill_runner.run_skill_awaitable(
        spec, chain_id="chain-fp0011-001",
    )

    # Contract 1: return shape exposes status + data verbatim.
    assert ret == {
        "status": "finished",
        "data": {"reply_text": "カレーレシピを生成しました"},
    }, f"FP-0011 contract: returned dict mismatch; got {ret!r}"

    # Contract 2: no narration side effects on outbox / history /
    # _router_loop_agent_replies — the router LLM is the narrator.
    assert session.router_loop_agent_replies == [], (
        "FP-0011 contract: _run_skill_awaitable must not push to "
        "_router_loop_agent_replies; got "
        f"{session.router_loop_agent_replies!r}"
    )
    assert len(session.history) == history_before, (
        "FP-0011 contract: _run_skill_awaitable must not append to history; "
        f"history grew by {len(session.history) - history_before} entries"
    )


@pytest.mark.asyncio
async def test_spawn_skill_for_router_returns_spawn_ack(
    tmp_path, monkeypatch
):
    """Tier 2: FP-0012 — chat-mode router-side invoke_skill returns spawn ack.

    Contract: ``_spawn_skill_for_router`` returns the
    ``{status: "spawned", run_id, chain_id, skill, note}`` ack
    immediately (= no blocking on the actual skill task). The skill task
    is queued via ``running_skills``; completion arrives via the
    ``skill_completed`` inbox kind, NOT inline.

    Observation: read the return value (= what the LLM sees as
    tool_result) and ``running_skills`` (= the actual asyncio task
    bookkeeping). We don't await the skill — the test just verifies the
    spawn handshake.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.session as session_mod

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    def _fake_resolve(skill_name):
        return dummy_skill_dir, tmp_path

    def _fake_load_dsl_skill(path, *, skill_root):
        return object()

    monkeypatch.setattr(session_mod, "resolve_skill_path", _fake_resolve)
    monkeypatch.setattr(session_mod, "load_dsl_skill", _fake_load_dsl_skill)

    session = _make_session(tmp_path)
    session.is_attached = True

    # Fake _build_agent so the spawned task does not actually run a real
    # skill (= keeps the test lightweight; we cancel the task at the end).
    class _NeverEndAgent:
        async def run(self, *_a, **_kw):
            await asyncio.sleep(60)  # never reached — task is cancelled below

    session._build_agent = lambda **kw: _NeverEndAgent()

    spec = {"skill": "direct_llm", "input": {"type": "llm_request", "data": {}}}
    ret = await session._spawn_skill_for_router(spec, chain_id="chain-fp0012-001")

    # Spawn-ack contract.
    assert ret["status"] == "spawned", f"unexpected status: {ret!r}"
    assert ret["chain_id"] == "chain-fp0012-001"
    assert ret["skill"] == "direct_llm"
    assert "run_id" in ret and ret["run_id"]
    assert "note" in ret and "/tasks" in ret["note"]

    # The asyncio task must be tracked in running_skills under the
    # returned run_id so /tasks list / /skill discard can reach it.
    run_id = ret["run_id"]
    assert run_id in session.running_skills, (
        f"FP-0012: spawn must register run_id in running_skills; "
        f"got keys: {list(session.running_skills.keys())}"
    )

    # Cleanup: cancel the long-sleep task.
    task = session.running_skills[run_id]
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


@pytest.mark.asyncio
async def test_skill_completed_inbox_enqueued_on_finish(tmp_path, monkeypatch):
    """Tier 2: FP-0012 — _run_one_skill enqueues skill_completed inbox on finish.

    Contract: when a background-spawned skill finishes (terminal status),
    the ``skill_completed`` inbox kind is appended with the structured
    payload ``{run_id, skill, chain_id, status, data}`` so the chat
    ``run()`` loop can drive narration on its next iteration.

    Observation: read ``session.inbox`` (= public asyncio.Queue) directly
    after _run_one_skill returns — the message must be present with the
    expected shape. WAL append is exercised transparently by
    SnapshotJournal.append_inbox.
    """
    monkeypatch.chdir(tmp_path)

    import reyn.chat.services.skill_runner as skill_runner_mod
    from reyn.core.kernel.runtime import RunResult

    dummy_skill_dir = tmp_path / "dummy_skill"
    dummy_skill_dir.mkdir()

    # FP-0019 Wave 1b: _run_one_skill now lives in SkillRunner, so patch
    # resolve_skill_path / load_dsl_skill in the skill_runner module.
    monkeypatch.setattr(
        skill_runner_mod, "resolve_skill_path",
        lambda name: (dummy_skill_dir, tmp_path),
    )
    monkeypatch.setattr(
        skill_runner_mod, "load_dsl_skill",
        lambda path, *, skill_root: object(),
    )

    session = _make_session(tmp_path)
    session.is_attached = True

    fake_result = RunResult(
        data={"path": "reyn/project/foo/skill.md"},
        status="finished",
    )
    # Patch build_agent_fn on SkillRunner (FP-0019 Wave 1b).
    session._skill_runner._build_agent_fn = lambda run_id, skill_name, **kw: _FakeAgent(fake_result)

    run_id = "20260510T100000Z_direct_llm_aaaa"
    session.running_skills_started_at[run_id] = 0.0
    session.running_skills_chain[run_id] = "chain-fp0012-002"

    # Drain any prior messages so the assertion below sees only what we enqueue.
    while not session.inbox.empty():
        session.inbox.get_nowait()

    await session._skill_runner._run_one_skill(
        run_id, "direct_llm",
        {"type": "llm_request", "data": {}},
        chain_id="chain-fp0012-002",
    )

    # The inbox must contain exactly one skill_completed message.
    kind, payload = await asyncio.wait_for(session.inbox.get(), timeout=1.0)
    assert kind == "skill_completed", f"expected skill_completed, got {kind!r}"
    assert payload["run_id"] == run_id
    assert payload["skill"] == "direct_llm"
    assert payload["status"] == "finished"
    assert payload["chain_id"] == "chain-fp0012-002"
    assert payload["data"] == {"path": "reyn/project/foo/skill.md"}


# ---------------------------------------------------------------------------
# B17-S8-3 fix: router op context declares index_drop permission
# ---------------------------------------------------------------------------


def test_router_op_context_declares_canonical_file_write_paths(tmp_path, monkeypatch):
    """Tier 2: router op context declares file.write for the canonical OS paths.

    #571 collapse arc Phase 5: the bool-axis ``index_drop`` /
    ``mcp_install`` declarations on the router context are replaced
    with the equivalent ``file.write`` entries for the canonical
    mutation paths. The op handlers (= index_drop / mcp_install /
    mcp_drop_server / cron_register) now gate via
    ``require_file_write`` against these paths.

    Observation: the test calls _make_router_op_context() and reads
    the public ``file_write`` list on the PermissionDecl. No private
    internal state is asserted.
    """
    monkeypatch.chdir(tmp_path)

    session = _make_session(tmp_path)
    ctx = session._make_router_op_context()

    declared_paths = {
        entry["path"]
        for entry in ctx.permission_decl.file_write
        if isinstance(entry, dict) and entry.get("path")
    }
    for canonical in (".reyn/mcp.yaml", ".reyn/cron.yaml", ".reyn/index/sources.yaml"):
        assert canonical in declared_paths, (
            f"#571 Phase 5: router op context must declare file.write for "
            f"{canonical!r} so the corresponding op handler's "
            f"require_file_write gate can pass; declared={sorted(declared_paths)}"
        )
