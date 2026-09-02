"""Tier 2: proposal 0067 P4e (#3978) — session_api.run_prompt_async +
the settle-consumer branch in InterAgentMessaging.handle_agent_response.

Real ``AgentRegistry`` + real ``Session`` (no mocks — mirrors
``test_run_prompt_result_3978_p4d.py``'s construction pattern).

Pins:

  1. No live target session → typed refusal (``target_session_not_found``),
     never a spawn (ADR-0040 D5 precedent, same as run_prompt(attached)).
  2. A successful dispatch registers the chain DIRECTLY and returns a
     task_id immediately, WITHOUT waiting for the target's reply — the
     target's turn is never run in this test, proving the call doesn't
     block on it (architect's ruling: run_prompt(async) never drives the
     target inline).
  3. The registered chain carries kind="prompt", |waiting_on| == 1 (just
     the target), and the caller's own (agent, sid) as requester.
  4. When the peer's reply arrives (simulated via
     ``caller._handle_agent_response(...)``, the same entry point a real
     inbound ``agent_response`` turn dispatches through), the settle
     branch fires: the chain is gone (settled, not left pending), exactly
     ONE router turn runs (mirroring _handle_pipeline_result/
     _handle_hook_message's "append already done, run the turn" shape —
     NOT the legacy relay-continuation path), and task_settled dispatches
     with task_id=chain_id, kind="prompt".
  5. Falsify pair: a LEGACY (kind=None) chain, registered the old way
     (mirrors test_multi_agent_p7.py's manual register), still resolves
     through the ORIGINAL relay-continuation path when a reply arrives —
     the new kind-based branch does not accidentally catch it too.
  6. NOT a truncate-falsify test (nothing here is truncated below any
     floor) — run_prompt_async's REAL registration call writes a
     chain_register WAL event that reconstructs with the right
     kind/waiting_on/requester via pure WAL replay from applied_seq=0.
     CLAUDE.md's recovery-feature PR gate (truncate PAST a saved
     snapshot's floor, confirm survival) is satisfied by
     tests/core/test_agent_snapshot.py::
     test_truncate_falsify_requester_survives_wal_truncation, which
     covers ChainManager.register()'s own generic WAL-shape mechanism;
     this test is narrower and different — it pins THIS producer's
     actual call shape (did run_prompt_async pass register() the right
     kwargs?), not the reconstruction mechanism itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import run_prompt_async
from reyn.runtime.task_types import Requester
from tests._support.agent_session import make_session


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile: AgentProfile) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
        )

    reg = AgentRegistry(
        project_root=tmp_path, session_factory=_factory, state_log=state_log,
    )
    holder["reg"] = reg
    return reg


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


@pytest.mark.asyncio
async def test_no_live_target_session_refuses_without_spawning(tmp_path):
    """Tier 2: ADR-0040 D5 precedent — a target naming no LIVE session is
    refused, never loaded/spawned (same posture run_prompt(attached) and
    send_to_session take)."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    reg.get_or_load("alpha")
    # "beta" is NEVER loaded — no live session exists for it.

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="hi",
    )
    assert result["status"] == "error"
    assert result["kind"] == "target_session_not_found"
    assert "no live session" in result["error"]


@pytest.mark.asyncio
async def test_registers_immediately_and_returns_without_waiting_for_reply(tmp_path):
    """Tier 2: the call returns a task_id as soon as the chain is
    registered + dispatched — the target's turn is never run in this
    test, proving run_prompt(async) does not drive it inline (unlike
    collect="attached")."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")  # live, but its turn loop never runs here

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    assert result["status"] == "started"
    task_id = result["data"]["task_id"]
    assert isinstance(task_id, str) and task_id

    chain = caller.chains.get(task_id)
    assert chain is not None, "the chain must be registered synchronously, before this call returns"
    assert chain.kind == "prompt"
    assert chain.waiting_on == {"beta"}
    assert chain.requester == Requester(agent_name="alpha", session_id="main")


@pytest.mark.asyncio
async def test_settle_branch_fires_one_router_turn_and_task_settled_then_clears_the_chain(
    tmp_path, monkeypatch,
):
    """Tier 2: the core P4e settle-branch proof. When the peer's reply
    arrives, the chain settles (not the legacy relay-continuation path):
    exactly one router turn runs, task_settled dispatches with the right
    payload, and the chain is gone afterward."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    task_id = result["data"]["task_id"]

    router_calls: list[tuple[str, str]] = []

    async def _record_router_loop(user_text: str, chain_id: str) -> None:
        router_calls.append((user_text, chain_id))

    monkeypatch.setattr(caller, "_run_router_loop", _record_router_loop)

    settled_events: list[tuple[str, dict]] = []

    async def _record_dispatch(point: str, template_vars: dict) -> None:
        settled_events.append((point, dict(template_vars)))

    # InterAgentMessaging captured Session.dispatch_external_event as a
    # BOUND callback at construction time (see session.py's
    # _build_inter_agent_messaging) — patching caller.dispatch_external_event
    # after the fact would not reach it; the injected callback itself needs
    # patching.
    monkeypatch.setattr(
        caller._inter_agent_messaging, "_dispatch_task_settled", _record_dispatch,  # noqa: SLF001
    )

    # Simulate the peer's reply arriving — the SAME entry point a real
    # inbound agent_response turn dispatches through (mirrors
    # test_session_invariants.py's own _handle_agent_response call shape).
    await caller._handle_agent_response({
        "from_agent": "beta",
        "response": "here is the answer",
        "depth": 1,
        "chain_id": task_id,
    })

    assert not caller.chains.has(task_id), "the chain must be gone after settle"
    # exactly one router turn — a second (or zero) call fails the unpack.
    ((ran_text, ran_chain_id),) = router_calls
    assert ran_chain_id == task_id
    assert "here is the answer" in ran_text

    (settled,) = [
        v for point, v in settled_events if point == "task_settled"
    ]
    assert settled["task_id"] == task_id
    assert settled["kind"] == "prompt"
    assert settled["status"] == "ok"


@pytest.mark.asyncio
async def test_legacy_kind_none_chain_still_uses_the_relay_continuation_path(
    tmp_path, monkeypatch,
):
    """Tier 2c: falsify pair — a chain registered the OLD way (kind=None,
    the legacy multi-hop relay shape from test_multi_agent_p7.py) must
    NOT be caught by the new settle branch. Confirms the kind-based
    discriminator is exact, not "any pending chain now settles"."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    chain_id = "legacy-chain-001"
    await caller.chains.register(
        chain_id=chain_id,
        depth=1,
        original_text="task for beta",
        sender=None,
        waiting_on={"beta"},
        requester=Requester(agent_name="", session_id="main"),
        origin_depth=0,
        # kind intentionally omitted — defaults to None, the legacy shape.
    )
    assert caller.chains.get(chain_id).kind is None

    settled_events: list[tuple[str, dict]] = []

    async def _record_dispatch(point: str, template_vars: dict) -> None:
        settled_events.append((point, dict(template_vars)))

    monkeypatch.setattr(caller, "dispatch_external_event", _record_dispatch)

    # _resolve_pending_chain re-invokes the router on an empty waiting_on;
    # stub it to a no-op recorder so this test doesn't need a real LLM.
    router_calls: list[str] = []

    async def _record_run_router_loop(text: str, cid: str) -> None:
        router_calls.append(cid)

    monkeypatch.setattr(caller, "_run_router_loop", _record_run_router_loop)

    await caller._handle_agent_response({
        "from_agent": "beta",
        "response": "legacy reply",
        "depth": 1,
        "chain_id": chain_id,
    })

    # The legacy path resolves via the relay-continuation route (re-runs
    # the router), NOT the new settle branch — no task_settled fires for
    # a kind=None chain.
    assert not any(point == "task_settled" for point, _ in settled_events), (
        "a legacy (kind=None) chain must never fire task_settled — "
        "that would mark a non-task as a settled task"
    )
    assert router_calls == [chain_id], (
        "the legacy relay-continuation path must still re-invoke the router"
    )


def _wal_diagnostic_dump(
    *, task_id: str, all_events: "list[dict]", caller, request: "pytest.FixtureRequest",
) -> str:
    """part of #4986: assertion-failure diagnostics ONLY — never changes
    control flow, never sleeps, never retries (CLAUDE.md: a duration must
    never be reached for by a test, in either direction). Assembled lazily
    (an ``assert cond, msg`` message expression is only evaluated once
    ``cond`` is already falsy — zero cost on the green path).

    Surfaces exactly what lead-coder's own #4986 next-step named, so the
    NEXT real CI red (7/7 today, 0/45 locally reproduced per this
    session's own finding on that issue) answers its own reproduction
    question directly, without a throwaway diagnostic branch/PR:

    1. The WAL's own real ``(kind, chain_id)`` population at the moment
       of failure — what IS there, not just that the expected entry
       wasn't found.
    2. Whether ``task_id`` exists under a DIFFERENT ``kind`` (a shape
       mismatch) or not at all (genuinely absent) — these are different
       failure classes and the plain assertion message could not tell
       them apart.
    3. ``DurabilityWorker``'s own drain state right after ``flush()``
       returned — ``queue.qsize()`` / whether the drainer task itself
       reports ``done()`` — the SAME internals :meth:`SnapshotJournal.
       flush`'s own barrier depends on (see that method's docstring).
    4. The xdist worker id and (to the extent ``request.node.session.
       items`` exposes it — the list IS ordered per-worker, so the
       immediately-PRECEDING entry is a real read, not a guess) the test
       that ran immediately before this one in the SAME worker process —
       the #4986 issue's own real-CI reports name a shared worker (gw2)
       across unrelated failures as one live hypothesis."""
    import os

    kind_chain_pairs = [(e.get("kind"), e.get("chain_id")) for e in all_events]
    same_chain_id_events = [e for e in all_events if e.get("chain_id") == task_id]
    if same_chain_id_events:
        shape_note = (
            f"{len(same_chain_id_events)} WAL event(s) DO carry "
            f"chain_id={task_id!r}, under kind(s)="
            f"{sorted({str(e.get('kind')) for e in same_chain_id_events})!r} "
            f"— not simply missing, a SHAPE mismatch"
        )
    else:
        shape_note = (
            f"NO WAL event anywhere carries chain_id={task_id!r} — "
            f"genuinely absent, not a shape mismatch"
        )

    worker = os.environ.get("PYTEST_XDIST_WORKER", "(not under xdist)")
    prev_test = "(unknown — request.node not found in session.items)"
    try:
        items = request.node.session.items
        idx = items.index(request.node)
        prev_test = (
            items[idx - 1].nodeid if idx > 0
            else "(first test collected for this worker's own run)"
        )
    except Exception:  # noqa: BLE001 — diagnostic-only, must never mask the real assertion
        pass

    drain_state = "(no DurabilityWorker reached — StateLog has no worker)"
    try:
        dworker = caller._state_log._worker  # noqa: SLF001
        qsize = dworker._queue.qsize() if dworker._queue is not None else "(queue never created)"  # noqa: SLF001
        drainer_done = dworker._drainer.done() if dworker._drainer is not None else "(drainer never created)"  # noqa: SLF001
        drain_state = f"queue.qsize()={qsize}, drainer.done()={drainer_done}"
    except Exception:  # noqa: BLE001 — diagnostic-only, must never mask the real assertion
        pass

    return (
        "\n  -- #4986 diagnostics (assertion-failure only; no retry, no sleep) --\n"
        f"  WAL (kind, chain_id) pairs, replay order: {kind_chain_pairs}\n"
        f"  {shape_note}\n"
        f"  drain state right after flush(): {drain_state}\n"
        f"  xdist worker: {worker!r}, previous test in this worker: {prev_test!r}\n"
    )


@pytest.mark.asyncio
async def test_registered_chain_wal_event_reconstructs_with_the_right_shape(
    tmp_path, request,
):
    """Tier 2c: NOT a truncate-falsify test — this replays every WAL event
    from applied_seq=0 (nothing is truncated below any floor). The actual
    truncation-survives-WAL-truncation coverage for kind/waiting_on/
    requester lives in
    tests/core/test_agent_snapshot.py::test_truncate_falsify_requester_survives_wal_truncation
    (a synthetic chain_register event, generic to ChainManager.register()'s
    own mechanism). What THIS test pins is different and narrower:
    run_prompt_async's REAL registration call (not a synthetic event)
    actually writes a chain_register WAL entry with the RIGHT kwargs —
    kind="prompt", the correct waiting_on, the correct requester — i.e.
    did this producer's call to register() pass what it claims to, proven
    by reading it back via pure WAL replay rather than trusting the
    in-memory chain object alone.

    part of #4986: both assertions below carry a diagnostic dump on
    failure only (see :func:`_wal_diagnostic_dump`) — this test's own
    flakiness on CI (Python 3.12 only, 7/7 today, 0/45 reproduced
    locally per this session's own finding on that issue) has no known
    reproduction recipe yet; the next real red now answers its own
    question instead of needing a second investigation pass."""
    reg = _make_registry(tmp_path)
    _seed(tmp_path, "alpha")
    _seed(tmp_path, "beta")
    caller = reg.get_or_load("alpha")
    reg.get_or_load("beta")

    result = await run_prompt_async(
        reg, caller_agent="alpha", caller_sid="main",
        target_agent="beta", target_session="main",
        prompt="please help",
    )
    task_id = result["data"]["task_id"]
    await caller._journal.flush()  # #2259 PR-2b: drain async WAL writes

    # PURE WAL REPLAY: a brand-new AgentSnapshot at applied_seq=0, fed only
    # the raw WAL entries — no live in-memory snapshot involved at all.
    all_events = list(caller._state_log.iter_from(0))  # noqa: SLF001
    chain_events = [
        e for e in all_events
        if e.get("kind") == "chain_register" and e.get("chain_id") == task_id
    ]
    assert chain_events, (
        f"no chain_register WAL event found for {task_id!r}"
        + _wal_diagnostic_dump(
            task_id=task_id, all_events=all_events, caller=caller, request=request,
        )
    )

    replayed = AgentSnapshot.empty("alpha")
    replayed.apply_events(all_events)
    reconstructed = replayed.pending_chains.get(task_id)
    assert reconstructed is not None, (
        f"chain {task_id!r} must reconstruct from pure WAL replay"
        + _wal_diagnostic_dump(
            task_id=task_id, all_events=all_events, caller=caller, request=request,
        )
    )
    assert reconstructed["task_kind"] == "prompt"
    assert reconstructed["waiting_on"] == ["beta"]
    assert reconstructed["requester"] == {"agent_name": "alpha", "session_id": "main"}
