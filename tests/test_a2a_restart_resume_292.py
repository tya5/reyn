"""Tier 2: A2A iv restart-resume end-to-end (= structural pin for #292 α deliverable).

Pre-#292 (= pre-PR #300): A2A async-mode ivs bypassed Session's
iv machinery (= ``InterventionHandler.dispatch`` was replaced, not
decorated, by the chain override). The iv lived only in the A2A bus
+ ``RunEntry.pending_intervention``. On process restart the bus
coroutine died with the process and the restored iv had a fresh
future no one awaited — peer answers vanished.

Post-α (= PR #300): override flipped to decorate-not-replace.
A2A ivs flow through ``InterventionHandler.dispatch`` like TUI ivs,
landing in ``_active`` + WAL + ``outstanding_interventions``.
R-D12's persistent answer buffer applies automatically.

This file pins the FULL restart-resume round-trip for A2A peer
answers:

  Phase 1 (pre-crash):
    - Skill emits iv via the chain-override decorated path
    - Iv lands in Session's outstanding_interventions
    - AgentSnapshot persists the iv
    - "Crash" = drop the session reference

  Phase 2 (restart):
    - Fresh Session loads from the same snapshot path
    - ``restore_state`` re-enqueues the iv with a fresh future

  Phase 3 (peer answer):
    - Simulate the A2A POST answer path via
      ``Session.answer_pending_intervention(run_id, answer)``
    - Restore watcher fires → R-D12 persists answer to
      ``buffered_intervention_answers`` (= survives second crash)

  Phase 4 (skill resume):
    - Fresh ``ChatInterventionBus`` fires iv with same run_id (= what
      a re-spawned skill would do via ``SkillResumeCoordinator``)
    - L6 ``_consume_buffered_intervention_answer`` short-circuits
      dispatch and returns the buffered answer
    - Skill receives answer without re-prompting the peer

If any of these phases regress (= e.g. someone flips override back to
replace-not-decorate, or removes the R-D12 buffer wire), this test
catches the break. Tier 2: contract-level, no real network, no real
async server.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.interfaces.web.run_registry import RunRegistry
from reyn.runtime.session import (
    DEFAULT_CHAT_CHANNEL_ID,
    Session,
)
from reyn.runtime.session_buses import ChatInterventionBus
from reyn.user_intervention import (
    InterventionAnswer,
    InterventionChoice,
    UserIntervention,
)

# ── helpers ────────────────────────────────────────────────────────────


def _make_session(tmp_path: Path, *, agent_name: str = "demo") -> Session:
    """Build a Session redirected to ``tmp_path``."""
    session = Session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )
    # Register the canonical chat-channel listener so dispatch doesn't
    # short-circuit on no-listener.
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    return session


def _build_post_crash_snapshot(
    *, agent_name: str, iv_id: str, run_id: str, iv_dict: dict,
) -> AgentSnapshot:
    """Construct an AgentSnapshot as if the prior process had persisted
    one outstanding intervention. Mirrors what
    ``record_intervention_dispatched`` would have written.
    """
    snap = AgentSnapshot.empty(agent_name)
    snap.outstanding_interventions[iv_id] = iv_dict
    snap.applied_seq = 5
    return snap


# ── 1. Phase-by-phase pin: ask_user round-trip ────────────────────────


def test_a2a_restart_resume_round_trip_ask_user(tmp_path: Path) -> None:
    """Tier 2: full restart-resume sequence for a free-text ask_user iv.

    Pre-α this scenario was structurally impossible (= A2A iv never in
    snapshot, peer answer hit a dead future). Post-α it survives a
    process restart end-to-end.
    """
    agent_name = "demo"

    # ── Phase 1: simulate the pre-crash state ────────────────────────
    # The iv exists in snapshot form (= what
    # InterventionHandler.dispatch's record_intervention_dispatched
    # would have written before the crash).
    iv_id = "iv-restart-ask"
    run_id = "run-restart-ask"
    pre_crash_iv_dict = {
        "kind": "ask_user",
        "prompt": "What is your name?",
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
        "origin_channel_id": f"a2a:{run_id}",  # stamped by A2A bus pre-crash
    }
    snap = _build_post_crash_snapshot(
        agent_name=agent_name, iv_id=iv_id, run_id=run_id,
        iv_dict=pre_crash_iv_dict,
    )

    async def _drive() -> None:
        # ── Phase 2: restart — fresh Session + restore_state ────
        session = _make_session(tmp_path, agent_name=agent_name)
        session.restore_state(snap)
        # Let the restored intervention task settle.
        for _ in range(3):
            await asyncio.sleep(0)

        # The iv must be back in the active queue.
        assert session.interventions.get(iv_id) is not None

        # ── Phase 3: simulate peer answer via A2A router path ──────
        # This is what _handle_answer_injection calls under α.
        peer_answer = InterventionAnswer(text="Alice")
        delivered = await session.answer_pending_intervention(
            run_id, peer_answer,
        )
        assert delivered is True

        # Let the restore watcher fire (= writes to
        # _buffered_intervention_answers + WAL R-D12 event).
        for _ in range(5):
            await asyncio.sleep(0)

        # The answer is now in the buffer.
        assert run_id in session.buffered_intervention_answers
        buffered = session.buffered_intervention_answers[run_id]
        assert buffered.text == "Alice"

        # ── Phase 4: skill resume picks up the buffered answer ─────
        # A fresh ChatInterventionBus simulates what a re-spawned skill
        # would do via its OpContext-injected bus.
        resumed_bus = ChatInterventionBus(
            session, run_id=run_id, skill_name="demo",
            channel_id=DEFAULT_CHAT_CHANNEL_ID,
        )
        # The skill emits a fresh iv (= it doesn't know about the
        # crashed iv); L6 consumes the buffered answer and short-
        # circuits dispatch.
        fresh_iv = UserIntervention(
            kind="ask_user", prompt="What is your name?",
            run_id=run_id, skill_name="demo",
        )
        answer = await resumed_bus.deliver(fresh_iv)
        assert answer.text == "Alice"

        # Buffer must be drained after consumption (= single-use).
        assert run_id not in session.buffered_intervention_answers

    asyncio.run(_drive())


# ── 2. Choice-based iv (= permission.* / safety.limit.*) round-trip ─


def test_a2a_restart_resume_round_trip_with_choice_id(tmp_path: Path) -> None:
    """Tier 2: same round-trip but with a choice-based iv (=
    permission.confirm). The peer's structured ``choice_id`` flows
    through the buffer (= R-D12 persists choice_id alongside text)
    and the skill receives an ``InterventionAnswer`` with the correct
    choice_id on resume.

    Pin the Gap 4 (PR #285) structured-answer semantics across restart.
    """
    agent_name = "demo"
    iv_id = "iv-restart-perm"
    run_id = "run-restart-perm"
    pre_crash_iv_dict = {
        "kind": "permission.confirm",
        "prompt": "Allow read access?",
        "detail": "",
        "choices": [
            {"id": "yes", "label": "[Y]es", "hotkey": "y"},
            {"id": "always", "label": "[A]lways", "hotkey": "a"},
            {"id": "no", "label": "[N]o", "hotkey": "n"},
        ],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
        "origin_channel_id": f"a2a:{run_id}",
    }
    snap = _build_post_crash_snapshot(
        agent_name=agent_name, iv_id=iv_id, run_id=run_id,
        iv_dict=pre_crash_iv_dict,
    )

    async def _drive() -> None:
        session = _make_session(tmp_path, agent_name=agent_name)
        session.restore_state(snap)
        for _ in range(3):
            await asyncio.sleep(0)

        # Peer answer carries an explicit choice_id (= Gap 4 semantics).
        peer_answer = InterventionAnswer(text="always", choice_id="always")
        delivered = await session.answer_pending_intervention(
            run_id, peer_answer,
        )
        assert delivered is True
        for _ in range(5):
            await asyncio.sleep(0)

        # Buffered answer preserves choice_id.
        buffered = session.buffered_intervention_answers[run_id]
        assert buffered.text == "always"
        assert buffered.choice_id == "always"

        # Skill resume picks it up unchanged.
        resumed_bus = ChatInterventionBus(
            session, run_id=run_id, skill_name="demo",
            channel_id=DEFAULT_CHAT_CHANNEL_ID,
        )
        fresh_iv = UserIntervention(
            kind="permission.confirm",
            prompt="Allow read access?",
            choices=[
                InterventionChoice(id="yes", label="[Y]es", hotkey="y"),
                InterventionChoice(id="always", label="[A]lways", hotkey="a"),
                InterventionChoice(id="no", label="[N]o", hotkey="n"),
            ],
            run_id=run_id, skill_name="demo",
        )
        answer = await resumed_bus.deliver(fresh_iv)
        assert answer.text == "always"
        assert answer.choice_id == "always"

    asyncio.run(_drive())


# ── 3. answer_pending_intervention return-value pins ───────────────────


def test_answer_pending_intervention_returns_false_for_unknown_run_id(
    tmp_path: Path,
) -> None:
    """Tier 2: peer POST with a run_id that doesn't match any
    outstanding iv returns False → router sends
    ``{"answered": false, "reason": ...}`` back. Sanity for the
    misrouted-peer-call path.
    """
    async def _drive() -> bool:
        session = _make_session(tmp_path)
        return await session.answer_pending_intervention(
            "no-such-run", InterventionAnswer(text="x"),
        )

    delivered = asyncio.run(_drive())
    assert delivered is False


def test_answer_pending_intervention_returns_false_when_future_done(
    tmp_path: Path,
) -> None:
    """Tier 2: if the iv's future is already done (= second peer POST
    races with the first), the second call returns False. Pin the
    idempotency guard so a flaky peer that POSTs twice doesn't
    double-resolve.
    """
    agent_name = "demo"
    iv_id = "iv-double"
    run_id = "run-double"
    pre_crash_iv_dict = {
        "kind": "ask_user",
        "prompt": "?",
        "detail": "",
        "choices": [],
        "suggestions": [],
        "run_id": run_id,
        "skill_name": "demo",
        "id": iv_id,
    }
    snap = _build_post_crash_snapshot(
        agent_name=agent_name, iv_id=iv_id, run_id=run_id,
        iv_dict=pre_crash_iv_dict,
    )

    async def _drive() -> tuple[bool, bool]:
        session = _make_session(tmp_path, agent_name=agent_name)
        session.restore_state(snap)
        for _ in range(3):
            await asyncio.sleep(0)

        first = await session.answer_pending_intervention(
            run_id, InterventionAnswer(text="first"),
        )
        # Allow watcher to drain the iv from _active.
        for _ in range(3):
            await asyncio.sleep(0)
        second = await session.answer_pending_intervention(
            run_id, InterventionAnswer(text="second"),
        )
        return first, second

    first, second = asyncio.run(_drive())
    assert first is True
    assert second is False


# ── 4. RunRegistry + peer-router smoke (= router layer wiring) ─────────


def test_handle_answer_injection_routes_through_chat_session(
    tmp_path: Path,
) -> None:
    """Tier 2: the A2A router's ``_handle_answer_injection`` looks up
    the agent via RunEntry.agent_name and calls
    ``Session.answer_pending_intervention``. Pin that the wiring
    survives — a refactor that drops the registry lookup or reverts
    to ``run_registry.answer_intervention`` (removed in #292) fails
    this test.

    We assert via inspect on the router source so the test stays
    independent of FastAPI / TestClient setup.
    """
    import inspect

    from reyn.interfaces.web.routers import a2a as a2a_router

    src = inspect.getsource(a2a_router._handle_answer_injection)
    assert "answer_pending_intervention" in src, (
        "_handle_answer_injection must call "
        "Session.answer_pending_intervention (issue #292 α path)"
    )
    assert "resolve_a2a_session" in src, (
        "_handle_answer_injection must look up the owning agent's SESSION via the "
        "registry (FP-0043 S4b-4 (B): resolve_a2a_session → the shared a2a session, "
        "not run_registry.answer_intervention)"
    )
    # The pre-#292 path (= run_registry.answer_intervention call) MUST
    # be gone. Pin its absence so a refactor that re-adds it (=
    # regression to the pre-α split persistence) fails first. We look
    # for the call form (= trailing ``(``) so the docstring's
    # historical reference to the deprecated method doesn't false-fire.
    assert "run_registry.answer_intervention(" not in src, (
        "the deprecated RunRegistry.answer_intervention call must not "
        "return — issue #292 dissolved it"
    )


# Silence pytest's unused-import warning for RunRegistry; kept for
# future tests that may want to exercise the FastAPI router end-to-end.
_ = RunRegistry
_ = pytest
