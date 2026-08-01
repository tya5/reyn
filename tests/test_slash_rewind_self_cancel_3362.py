"""Tier 2: ``/rewind <seq>`` must not cancel the very turn that issued it.

``AgentRegistry.checkout`` is a stop-world primitive: step 2 calls
``cancel_inflight()`` on *every* loaded session. When the caller is a slash
handler running inside one of those sessions' own turns, that all-cancel used
to include the caller — ``_turn_owner_task.cancel()`` armed ``_must_cancel`` on
the running task, and ``CancelledError`` landed at checkout's first real
suspension (the reset-record's durability await). The observable damage:

  * the ``rewind`` WAL record is written (it is enqueued before that await) but
    step 5 (``_materialize_rewind``) never runs — the WAL claims the world was
    reset to the target while every live session keeps running the pre-rewind
    lineage, and no snapshot is persisted at the reset seq;
  * the user gets **neither** the ``⏪ checked out to seq N`` reply **nor** an
    error, because ``CancelledError`` is a ``BaseException`` and ``rewind_cmd``
    catches ``Exception``.

``await_quiescent`` already carried the matching re-entrancy guard for exactly
this call shape; ``cancel_inflight`` did not. These tests drive the real slash
seam through a real ``Session`` run loop on a real ``StateLog``.

★ **#3595 S5 changed the caller shape, and this file records it rather than
quietly following it.** A slash command no longer runs inside the issuing
session's turn task: the dispatch is client-side
(``reyn.interfaces.slash.dispatch``) and the handler runs on the CLIENT's task,
so ``checkout``'s all-cancel can no longer reach its own caller through this
route. The self-cancel REPRO is therefore gone from the slash path — the guard
stays (``await_quiescent`` carried the same one for an independent call shape,
and nothing here argues for removing a guard because one of its callers moved).
These tests now drive ``/rewind`` the way an operator reaches it — the shared
client layer over a real ``InProcessTransport`` on this same real registry —
which is why the divergence gate below, explicitly kept "for the failure class
rather than for this instance", is the one that carries forward unchanged in
meaning.

``test_a_persisted_rewind_record_implies_the_reset_was_materialised`` is the
**divergence-falsify** gate — the permanent one, kept for the failure class
rather than for this instance. It compares the WAL's claim against what the
runtime substrate actually did, and it is the DUAL of the recovery-PR
truncate-falsify rule (which asks whether derived state survives the loss of
its source WAL-events): truncating cannot reproduce an over-claim, so a repair
to a reconstruction path needs this direction falsified too. See its docstring.

Destructive-path safety: every path — project root, WAL, agent dirs, workspace
— is under ``tmp_path``, and the test ``chdir``s there, so no real state can be
rewound.

The only substituted thing is the provider boundary itself
(``router_loop.call_llm_tools``, replaced by a real ``async def`` returning a
real ``LLMToolCallResult`` — the idiom ``test_fp0036_live_runner.py`` already
uses). Everything the fix touches — ``Session``, its run loop, ``AgentRegistry``,
``StateLog``, the slash registry — is a real instance.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from tests._support.agent_session import make_session

_WAIT_S = 20.0
_REPLY = "acknowledged"


async def _scripted_provider(**_kwargs) -> LLMToolCallResult:
    """The provider boundary: a real async callable returning a real result."""
    return LLMToolCallResult(
        content=_REPLY,
        tool_calls=[],
        finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def _install_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _scripted_provider,
    )


def _build_registry(tmp_path: Path) -> AgentRegistry:
    """A real registry + real WAL + real Session factory, entirely under tmp_path."""
    agents_dir = tmp_path / ".reyn" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    cell: "list[AgentRegistry]" = []

    def factory(profile: AgentProfile) -> Session:
        agent_dir = agents_dir / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        session = make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
            registry=cell[0] if cell else None,
            workspace_base_dir=tmp_path / "ws",
            workspace_state_dir=agent_dir / "state",
        )
        session.load_history()
        return session

    registry = AgentRegistry(
        project_root=tmp_path, session_factory=factory, state_log=state_log,
    )
    cell.append(registry)
    AgentProfile.new("default", role="test agent").save(registry._dir / "default")
    return registry


async def _await_reply(registry: AgentRegistry, *, contains: str) -> "str | None":
    """Drain ``repl_outbox`` until a message whose text contains ``contains``.

    Returns the matching text, or ``None`` if the wait budget expires — which is
    what a self-cancelled rewind produces (silence, no error frame either).
    """
    async def _drain() -> str:
        while True:
            msg = await registry.repl_outbox.get()
            text = str(getattr(msg, "text", "") or "")
            if contains in text:
                return text

    try:
        return await asyncio.wait_for(_drain(), timeout=_WAIT_S)
    except asyncio.TimeoutError:
        return None


async def _run_turns(registry: AgentRegistry, session, count: int) -> None:
    """Drive ``count`` router turns, each settled to a WAL generation boundary.

    The reply frame reaches the client BEFORE the turn's last WAL append lands,
    so waiting on the reply alone would read ``list_rewind_points()`` mid-turn.
    Waiting for the boundary itself is the settle condition the rewind needs.
    """
    for i in range(count):
        before = len(registry.list_rewind_points())
        await session.submit_user_text(f"turn {i}")
        assert await _await_reply(registry, contains=_REPLY) is not None, (
            f"turn {i} never produced a reply"
        )
        for _ in range(int(_WAIT_S / 0.05)):
            if len(registry.list_rewind_points()) > before:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"turn {i} never reached a WAL generation boundary")


async def _run_rewind(registry: AgentRegistry, target: int) -> bool:
    """Issue ``/rewind <target>`` the way an operator does after #3595 S5.

    The REAL client transport over this REAL registry, through the shared
    client-side layer — the production path, not a handler called directly.
    Replies land on ``registry.repl_outbox``, which ``_await_reply`` drains, so
    the observation point is unchanged.
    """
    transport = InProcessTransport(
        registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )
    return await maybe_dispatch_slash(transport, f"/rewind {target}")


def _snapshot_applied_seq(snapshot_path: Path) -> "int | None":
    """The runtime substrate's own cut — the half a WAL read cannot see."""
    if not snapshot_path.exists():
        return None
    return json.loads(snapshot_path.read_text(encoding="utf-8")).get("applied_seq")


def _wal_seqs(tmp_path: Path) -> "list[tuple[int, str]]":
    wal = tmp_path / ".reyn" / "state" / "wal.jsonl"
    return [
        (json.loads(line)["seq"], json.loads(line).get("kind", ""))
        for line in wal.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.asyncio
async def test_rewind_confirms_to_the_operator(tmp_path, monkeypatch):
    """Tier 2: ``/rewind <seq>`` returns its confirmation to the operator.

    Originally RED without the ``cancel_inflight`` self-cancel guard: the turn
    that issued the command cancelled itself inside ``checkout``, so NOTHING
    reached the client — not the ``⏪ checked out`` reply and not an error
    frame. #3595 S5 moved the caller off the turn task (see the module
    docstring), so this now holds the plainer contract the symptom named: a
    rewind an operator asks for is either confirmed or reported, never silent.
    """
    monkeypatch.chdir(tmp_path)
    _install_provider(monkeypatch)
    registry = _build_registry(tmp_path)
    await registry.attach("default")
    session = registry.get_session("default")
    await _run_turns(registry, session, 3)

    points = registry.list_rewind_points()
    assert points, "the completed turns must have produced rewind points"
    target = points[0]["seq"]

    assert await _run_rewind(registry, target), "/rewind was not run as a command"
    reply = await _await_reply(registry, contains="⏪")

    assert reply is not None, (
        "/rewind produced neither a confirmation nor an error — the turn "
        "cancelled itself inside checkout"
    )
    assert str(target) in reply


@pytest.mark.asyncio
async def test_a_persisted_rewind_record_implies_the_reset_was_materialised(
    tmp_path, monkeypatch,
):
    """Tier 2: DIVERGENCE-falsify — the WAL may not claim a rewind that never ran.

    The permanent gate for the failure class this defect belongs to. Note it is
    **not** a truncate-falsify, and the recovery-PR gate's truncate rule cannot
    substitute for it: truncate-falsify asks whether derived state survives the
    LOSS of its source WAL-events; this defect is the exact dual — the WAL
    ASSERTS something the runtime never did. Removing WAL-events cannot
    reproduce an over-claim, so the two gates falsify opposite directions and a
    repair to a reconstruction path needs this one.

    The two altitudes it compares, which must never disagree:

      * **the WAL's claim** — a ``rewind`` reset-record at seq ``R``;
      * **what the runtime actually did** — the self-contained snapshot
        ``checkout`` step 5 persists at ``applied_seq = R`` (so ``restore_all``
        replays only ``> R``).

    Asserting on the WAL record alone would be VACUOUS here: a self-cancelled
    checkout still lands it, because the write is enqueued (``put_nowait``)
    before the await the cancel arrives at. The snapshot is the discriminator —
    without the guard it stays behind at a pre-rewind seq while the record says
    the world moved. The before/after comparison is what makes it a divergence
    assertion rather than a coincidence: the snapshot must be observed to
    ADVANCE to ``R``, not merely to be found sitting there.
    """
    monkeypatch.chdir(tmp_path)
    _install_provider(monkeypatch)
    registry = _build_registry(tmp_path)
    await registry.attach("default")
    session = registry.get_session("default")
    await _run_turns(registry, session, 3)

    snapshot_path = tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json"
    applied_before = _snapshot_applied_seq(snapshot_path)

    target = registry.list_rewind_points()[0]["seq"]
    assert await _run_rewind(registry, target), "/rewind was not run as a command"
    await _await_reply(registry, contains="⏪")

    reset_seq, reset_kind = _wal_seqs(tmp_path)[-1]
    assert reset_kind == "rewind", (
        "the WAL must carry the claim this gate falsifies against — no reset "
        "record means the test never reached the divergence it exists to catch"
    )
    assert applied_before is not None and applied_before < reset_seq, (
        "the snapshot must start BEHIND the reset record, or advancing to it "
        "would prove nothing"
    )

    applied_after = _snapshot_applied_seq(snapshot_path)
    assert applied_after == reset_seq, (
        f"WAL/runtime divergence: the WAL claims a rewind at seq {reset_seq}, "
        f"but the runtime substrate is still at applied_seq {applied_after} — "
        "_materialize_rewind never ran, so every live session keeps the "
        "pre-rewind lineage the record says was abandoned"
    )


@pytest.mark.asyncio
async def test_the_session_keeps_serving_turns_after_a_rewind(tmp_path, monkeypatch):
    """Tier 2: the run loop survives the rewind and consumes the NEXT submission.

    The reported symptom class: an inbox_put with no matching inbox_consume,
    forever, because the driver task died on an escaping cancel.

    ★Honest scope: this one is **not discriminating for the self-cancel
    defect** — measured, not assumed. Stripping the guard leaves it GREEN,
    because ``run_one_iteration`` swallows a cancel it recognises as
    self-initiated, so the driver survives even when checkout is destroyed.
    It stays as the liveness half of the rewind contract (a put after a rewind
    must still reach a consume), which nothing else pins; it does not claim to
    witness the guard.
    """
    monkeypatch.chdir(tmp_path)
    _install_provider(monkeypatch)
    registry = _build_registry(tmp_path)
    await registry.attach("default")
    session = registry.get_session("default")
    await _run_turns(registry, session, 3)

    target = registry.list_rewind_points()[0]["seq"]
    assert await _run_rewind(registry, target), "/rewind was not run as a command"
    await _await_reply(registry, contains="⏪")

    before = len([1 for _, kind in _wal_seqs(tmp_path) if kind == "inbox_consume"])
    await session.submit_user_text("after the rewind")
    assert await _await_reply(registry, contains=_REPLY) is not None, (
        "the session stopped answering after a rewind"
    )
    for _ in range(int(_WAIT_S / 0.05)):
        after = len([1 for _, kind in _wal_seqs(tmp_path) if kind == "inbox_consume"])
        if after > before:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("the post-rewind submission was never consumed")
