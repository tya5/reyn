"""Tier 2: #3792 PR2 — the Session-level peek/commit primitives for mid-turn
``CLIENT_INPUT`` injection.

Covers, per architect's issue #3792 Test plan (the Session-level items; the
RouterLoop-level wire-position / strip-falsify witnesses live in
``tests/llm/test_3792_pr2_router_loop_injection.py``):

- **origin gate** — only ``TurnOrigin.CLIENT_INPUT`` is peek-eligible; all 9
  members enumerated (vacuity guard: fails loud if the enumeration is empty).
- **order** — peek looks PAST an ineligible head to the operator's own
  message (#5647 replaced #3792's original STOP rule; see that test's own
  docstring for why), while the ordinary turn-boundary ``_consume_inbox``
  drain still returns everything in arrival order.
- **carry-forward** — a peeked-but-never-committed item (the abnormal-exit
  case: cancel / cap / overflow / LLM exception happened before commit) is
  not lost — the next ``_consume_inbox`` call returns the SAME item.
- **the atomic commit unit** — history append + snapshot/WAL prune +
  ``turn_started`` promote-delta, all three.
- **truncate-falsify** (CLAUDE.md recovery-feature PR gate) — a committed
  injection's inbox entry does not resurrect after a WAL truncation below
  its source events.
(Pre-#5561 this list also had a "loop valve — commit does not reset
``_hook_driven_turns``" bullet with its own dedicated test; #5561 retired
that counter entirely, and the test with it — see
``test_commit_does_not_reset_hook_driven_turns``'s old location, git
history.)

Real ``Session``/``StateLog``/``SnapshotJournal`` throughout (the
``tests/interfaces/test_3300_p3_cancel_by_id.py`` convention) — no ``unittest.mock``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

AGENT = "pr2-injection-agent"


def _make_session(wal: Path, snapshot_path: Path) -> tuple[Session, StateLog]:
    state_log = StateLog(wal)
    session = make_session(agent_name=AGENT, state_log=state_log, snapshot_path=snapshot_path)
    return session, state_log


def _reconstruct(agent_name: str, snapshot_path: Path, state_log: StateLog) -> AgentSnapshot:
    snap = AgentSnapshot.load(agent_name, snapshot_path)
    events = list(state_log.iter_from(snap.applied_seq))
    snap.apply_events(events)
    return snap


# ---------------------------------------------------------------------------
# Origin gate — exhaustive, vacuity-guarded
# ---------------------------------------------------------------------------


_INELIGIBLE_KWARGS: dict[TurnOrigin, dict] = {
    TurnOrigin.AGENT_RESPONSE: {"from_agent": "a", "response": "r", "depth": 1, "chain_id": "c1"},
    TurnOrigin.PIPELINE_RESULT: {"run_id": "run1", "pipeline_name": "p", "status": "ok", "text": "t"},
    TurnOrigin.AGENT_STEP: {"seq": 1},
    TurnOrigin.CRON: {"job": "nightly"},
    TurnOrigin.PIPELINE_NUDGE: {"run_id": "run1"},
    # Proposal 0067 P5 (#3978): send_to_session / run_prompt(attached), a
    # peer session's text — see TurnOrigin.PEER_SESSION's own docstring.
    TurnOrigin.PEER_SESSION: {"text": "hi", "from_session": "a2a:peer"},
}


@pytest.mark.asyncio
async def test_only_mid_turn_injectable_origins_are_peek_eligible(tmp_path):
    """Tier 2: #5677 — exhaustive origin gate over ALL 10 ``TurnOrigin``
    members against :data:`~reyn.runtime.turn_origin.MID_TURN_INJECTABLE`
    (vacuity guard: asserts the enumeration itself is non-empty and matches
    the expected 10, so a future member silently added to the enum without
    a corresponding case here is caught, not silently vacuous).

    #5677 widened eligibility from ``CLIENT_INPUT`` alone to
    ``MID_TURN_INJECTABLE`` (``CLIENT_INPUT`` + ``AGENT_REQUEST`` +
    ``EXTERNAL_MESSAGE``); #5747 widened it once more (``HOOK`` — see
    ``MID_TURN_INJECTABLE``'s own per-member reasoning for all four).
    This test is the sibling architect's #5677 co-vet required for the
    new axis, in the SAME exhaustive-enumeration shape #3595's own
    site-census gate uses (one file, one gate, both directions —
    deny-side and accept-side together, not one alone): every OTHER
    member must still be excluded.

    Falsification (performed during review): reverting
    ``InboxArbiter.peek_mid_turn_injections``'s eligibility check from
    ``kind not in MID_TURN_INJECTABLE`` back to
    ``kind != TurnOrigin.CLIENT_INPUT`` makes the AGENT_REQUEST accept-side
    assertion below go RED (an empty list, not one entry); widening it to
    accept every kind unconditionally makes every deny-side assertion below
    go RED instead.
    """
    all_origins = list(TurnOrigin)
    eligible_kwargs = {
        TurnOrigin.CLIENT_INPUT: {"text": "hello"},
        TurnOrigin.AGENT_REQUEST: {"from_agent": "a", "request": "r", "depth": 1, "chain_id": "c1"},
        TurnOrigin.EXTERNAL_MESSAGE: {"text": "hi", "sender": "slack:U456"},
        # #5747: owner-requested feature, previously unimplemented. Only
        # the mcp_resource_updated POINT is actually eligible (a NARROWER
        # gate than this dict's own key-level vacuity check expresses —
        # see test_hook_injection_is_eligible_only_for_its_one_declared_
        # point below for that finer-grained accept/deny pair).
        TurnOrigin.HOOK: {"name": "on_idle", "text": "hi", "point": "mcp_resource_updated"},
    }
    # Vacuity guard: if TurnOrigin grew, shrank, or the enumeration came back
    # empty, this set-equality against the explicit expected membership
    # (below) fails LOUD — no separate count needed, a set-size mismatch is
    # already a set-inequality.
    assert set(_INELIGIBLE_KWARGS) | set(eligible_kwargs) == set(all_origins), (
        "TurnOrigin's membership changed — update _INELIGIBLE_KWARGS/"
        "eligible_kwargs above to cover the new/removed member before "
        "trusting this gate again"
    )

    # Deny side: every kind OUTSIDE MID_TURN_INJECTABLE stays ineligible.
    for origin, kwargs in _INELIGIBLE_KWARGS.items():
        session, state_log = _make_session(
            tmp_path / f"{origin.value}.wal", tmp_path / f"{origin.value}.json",
        )
        await session._put_inbox(origin, kwargs)
        result = await session._inbox_arbiter.peek_mid_turn_injections()
        assert result == [], f"origin {origin!r} must NOT be peek-eligible"
        await state_log.aclose()

    # Accept side: CLIENT_INPUT (via the real submit_user_text path) IS eligible.
    session, state_log = _make_session(
        tmp_path / "client_input.wal", tmp_path / "client_input.json",
    )
    await session.submit_user_text("hello")
    result = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = result
    assert only["payload"]["text"] == "hello"
    assert only["kind"] == TurnOrigin.CLIENT_INPUT
    await state_log.aclose()

    # Accept side: AGENT_REQUEST — #5677's own motivation — is ALSO eligible.
    session2, state_log2 = _make_session(
        tmp_path / "agent_request.wal", tmp_path / "agent_request.json",
    )
    await session2._put_inbox(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "peer-agent", "request": "corrected instruction", "depth": 1, "chain_id": "c1"},
    )
    result2 = await session2._inbox_arbiter.peek_mid_turn_injections()
    (only2,) = result2
    assert only2["payload"]["request"] == "corrected instruction"
    assert only2["kind"] == TurnOrigin.AGENT_REQUEST
    await state_log2.aclose()

    # Accept side: EXTERNAL_MESSAGE — owner ruling (2026-09-02), overriding
    # architect/lead-coder's own recommendation to exclude it — is ALSO
    # eligible.
    session3, state_log3 = _make_session(
        tmp_path / "external_message.wal", tmp_path / "external_message.json",
    )
    await session3._put_inbox(
        TurnOrigin.EXTERNAL_MESSAGE, {"text": "urgent update", "sender": "slack:U456"},
    )
    result3 = await session3._inbox_arbiter.peek_mid_turn_injections()
    (only3,) = result3
    assert only3["payload"]["text"] == "urgent update"
    assert only3["kind"] == TurnOrigin.EXTERNAL_MESSAGE
    await state_log3.aclose()

    # Accept side: HOOK — #5747, owner-requested feature, previously
    # unimplemented — is ALSO eligible, when its own point is
    # mcp_resource_updated (see test_hook_injection_is_eligible_only_
    # for_its_one_declared_point below for the finer-grained pair this
    # abbreviates).
    session4, state_log4 = _make_session(
        tmp_path / "hook.wal", tmp_path / "hook.json",
    )
    await session4._put_inbox(
        TurnOrigin.HOOK,
        {"name": "on_idle", "text": "check the queue", "point": "mcp_resource_updated"},
    )
    result4 = await session4._inbox_arbiter.peek_mid_turn_injections()
    (only4,) = result4
    assert only4["payload"]["text"] == "check the queue"
    assert only4["kind"] == TurnOrigin.HOOK
    await state_log4.aclose()


@pytest.mark.asyncio
async def test_hook_injection_is_eligible_only_for_its_one_declared_point(tmp_path):
    """Tier 2: #5747 — ``TurnOrigin.HOOK`` membership in
    ``MID_TURN_INJECTABLE`` is necessary but not sufficient: a hook push
    is ALSO required to have fired from ``mcp_resource_updated`` (the
    owner's own request — a broker/MCP inbox notification steering an
    in-flight turn). A hook wired to any OTHER point is looked past
    exactly like an ineligible-kind item — the design question of which
    other points should also qualify is an open discussion, deliberately
    not decided by this test (see ``InboxArbiter.peek_mid_turn_
    injections``'s own docstring).

    Falsification (performed during review): removing the ``payload.get(
    "point") not in _HOOK_INJECTABLE_POINTS`` guard from
    ``peek_mid_turn_injections`` (reverting to plain ``TurnOrigin``
    membership, #5750's own original shipped shape) makes the deny-side
    assertion below go RED — a hook from ANY point would be injected."""
    # Accept: mcp_resource_updated.
    session, state_log = _make_session(tmp_path / "accept.wal", tmp_path / "accept.json")
    await session._put_inbox(
        TurnOrigin.HOOK,
        {"name": "broker-inbox", "text": "peer message arrived", "point": "mcp_resource_updated"},
    )
    result = await session._inbox_arbiter.peek_mid_turn_injections()
    (only,) = result
    assert only["payload"]["text"] == "peer message arrived"
    await state_log.aclose()

    # Deny: any other point — file_changed here, the exact one architect's
    # own self-recursion concern named (a turn's own tool writes a file a
    # `file_changed` hook watches) — must NOT be injected.
    session2, state_log2 = _make_session(tmp_path / "deny.wal", tmp_path / "deny.json")
    await session2._put_inbox(
        TurnOrigin.HOOK,
        {"name": "fs-watch", "text": "a file changed", "point": "file_changed"},
    )
    result2 = await session2._inbox_arbiter.peek_mid_turn_injections()
    assert result2 == [], (
        "a hook from a point OTHER than mcp_resource_updated must not be "
        "peek-eligible — widening past this one point is an open design "
        "question, not something this code should do unasked"
    )
    await state_log2.aclose()

    # Deny: a HOOK payload missing "point" entirely (a hand-built/legacy
    # payload shape) must also be treated as ineligible, never as a
    # free pass — absence is not the same claim as "the right point".
    session3, state_log3 = _make_session(tmp_path / "deny2.wal", tmp_path / "deny2.json")
    await session3._put_inbox(TurnOrigin.HOOK, {"name": "no-point", "text": "no point field"})
    result3 = await session3._inbox_arbiter.peek_mid_turn_injections()
    assert result3 == [], "a HOOK payload with no 'point' field must not be peek-eligible"
    await state_log3.aclose()


# ---------------------------------------------------------------------------
# Order — injection looks past an ineligible head; consumption order does not
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_looks_past_an_ineligible_head_but_consume_order_is_unchanged(tmp_path):
    """Tier 2: #5647 replaced this test's original claim, deliberately.

    It used to assert that an ineligible head STOPS peek, and its own
    docstring named the falsifier: "making peek skip past an ineligible head
    ... makes this test go RED". #5647 makes peek do exactly that, because the
    STOP rule inverted its own purpose in practice — on reyn-self a
    ``broker_drain`` hook posts inbox items continuously, so the operator's
    prompt was nearly always sitting behind one and mid-turn injection, the
    feature that exists so a human can steer a running tool loop, never fired
    (owner-reported: the prompt sat in the TUI's sent-queue instead).

    What survives unchanged is the half that actually carried the invariant:
    **consumption order**. #3792's objection to skipping was that it would
    "silently reorder arrival". It does not — injection changes which message
    the model sees first WITHIN a turn; the turn-boundary drain still returns
    the ineligible head before CLIENT_INPUT, exactly as this test asserted
    before.

    #5677 (this update): the head item was made ``HOOK`` rather than
    ``AGENT_REQUEST`` — ``AGENT_REQUEST`` is itself peek-eligible now
    (#5677's own motivation), so it could no longer stand in for "an
    ineligible head" in this test.

    #5747 (this same update, repeated): ``HOOK`` is now ALSO
    peek-eligible (the owner-requested feature #5747 built), so it
    cannot stand in either — swapped again, to ``CRON`` (still
    genuinely ineligible; see ``TurnOrigin.CRON``'s own docstring). This
    is the SECOND time this test's own "ineligible head" example has had
    to move as ``MID_TURN_INJECTABLE`` widened — a reader maintaining
    this test next should expect a THIRD move is possible too.

    Strip-falsifier: restore the STOP (return ``[]`` on an ineligible head)
    and the peek assertion below goes red — which is the owner's symptom, not
    a cosmetic difference.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session._put_inbox(TurnOrigin.CRON, {"job": "nightly"})
    await session.submit_user_text("second, eligible")

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked, (
        "#5647: peek must look PAST the ineligible CRON head to find "
        "the operator's message — stopping here is the reported defect"
    )
    assert peeked[0]["payload"]["text"] == "second, eligible"

    # The item looked past is NOT consumed by the peek, and is still first.
    kind, payload = await session._inbox_arbiter.consume_inbox()
    assert kind == TurnOrigin.CRON, (
        "the FIRST item consume_inbox returns must still be the ineligible "
        "head — injection reorders what the MODEL sees, never what the turn "
        "boundary consumes"
    )
    assert payload["job"] == "nightly"

    kind2, payload2 = await session._inbox_arbiter.consume_inbox()
    assert kind2 == TurnOrigin.CLIENT_INPUT
    assert payload2["text"] == "second, eligible"

    await state_log.aclose()


@pytest.mark.asyncio
async def test_peek_collects_every_eligible_item_in_one_scan(tmp_path):
    """Tier 2: #5677 (owner ruling, verbatim: "溜まっているものは基本inject
    すべき… わざわざ分けるとコスト増えるだけ") — multiple queued eligible
    items are ALL returned by one ``peek_mid_turn_injections()`` call, not
    just the first, so a caller can splice them into the SAME completion
    round instead of paying for one round trip per item.

    An ineligible item (``CRON`` — #5747 made ``HOOK`` itself eligible
    too, so this test's own "ineligible, between" example moved to CRON,
    same swap as the sibling test above) BETWEEN the two eligible ones
    is looked past and stays in the buffer — collecting continues past
    it, the same "skip, don't stop" behavior #5647 established,
    generalized from "stop at the first eligible hit" to "keep going to
    the end of what's available".

    Strip-falsifier: reverting ``peek_mid_turn_injections`` to ``return``
    immediately after the FIRST eligible hit (the pre-#5677 shape) makes
    the length assertion below go red (1 item, not 2) and the AGENT_REQUEST
    assertion never runs.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")

    await session.submit_user_text("first, eligible")
    await session._put_inbox(TurnOrigin.CRON, {"job": "nightly"})  # ineligible, between
    await session._put_inbox(
        TurnOrigin.AGENT_REQUEST,
        {"from_agent": "peer", "request": "second, eligible", "depth": 1, "chain_id": "c1"},
    )

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    first, second = peeked  # exactly 2 — a 3rd or a missing one fails to unpack
    assert first["kind"] == TurnOrigin.CLIENT_INPUT
    assert first["payload"]["text"] == "first, eligible"
    assert second["kind"] == TurnOrigin.AGENT_REQUEST
    assert second["payload"]["request"] == "second, eligible"

    # The ineligible CRON item looked past remains, unconsumed, in arrival order.
    kind, payload = await session._inbox_arbiter.consume_inbox()
    assert kind == TurnOrigin.CLIENT_INPUT
    kind2, payload2 = await session._inbox_arbiter.consume_inbox()
    assert kind2 == TurnOrigin.CRON
    assert payload2["job"] == "nightly"
    kind3, payload3 = await session._inbox_arbiter.consume_inbox()
    assert kind3 == TurnOrigin.AGENT_REQUEST

    await state_log.aclose()


# ---------------------------------------------------------------------------
# Carry-forward — peeked-but-never-committed is not lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peek_without_commit_carries_forward_to_normal_consume(tmp_path):
    """Tier 2: #3792 — simulates the abnormal-exit case (cancel / cap /
    overflow / LLM exception happened between peek and commit): the peeked
    item is NEVER lost — the next ``_consume_inbox`` (the normal
    turn-boundary drain) returns the exact same item, processed as an
    ordinary new turn, exactly as if the peek had never happened.

    Falsification (performed during review): if ``_consume_inbox`` read
    ``self.inbox.get()`` unconditionally (ignoring
    ``self._inbox_arbiter.pending_inbox_item``), this test goes RED with an indefinite
    hang (the queue is empty — the item is stuck in the buffer forever) —
    demonstrated by using ``asyncio.wait_for`` with a short timeout instead
    of a bare await, so the failure mode is a clean test failure, not a
    stuck CI job.
    """
    import asyncio

    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    await session.submit_user_text("carry me")

    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked
    # Deliberately do NOT commit — simulates the abnormal exit.

    kind, payload = await asyncio.wait_for(session._inbox_arbiter.consume_inbox(), timeout=2.0)
    assert kind == TurnOrigin.CLIENT_INPUT
    assert payload["text"] == "carry me"
    assert payload["_msg_id"] == peeked[0]["msg_id"]

    await state_log.aclose()


# ---------------------------------------------------------------------------
# The atomic commit unit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_appends_history_prunes_snapshot_emits_turn_started(tmp_path):
    """Tier 2: #3792 — a successful commit does all three: (1) appends a
    ``role="user"`` entry to ``session.history`` (the restore SSoT), (2)
    prunes the item from ``journal.snapshot.inbox`` (the inbox SSoT), (3)
    emits a ``turn_started`` audit-event carrying the injected message's own
    chain_id — the SAME shape the ordinary turn-boundary promote uses.

    Falsification (performed during review): commenting out any ONE of the
    three statements in ``Session._commit_mid_turn_injection`` makes the
    corresponding assertion below go RED independently of the other two —
    confirming each is actually load-bearing, not incidentally satisfied by
    one of the others.
    """
    session, state_log = _make_session(tmp_path / "s.wal", tmp_path / "s.json")
    events = collect_events(session)

    msg_id = await session.submit_user_text("inject me", attribution=None)
    peeked = await session._inbox_arbiter.peek_mid_turn_injections()
    assert peeked
    assert peeked[0]["msg_id"] == msg_id

    before_history_len = len(session.history)
    assert any(m["id"] == msg_id for m in session.journal.snapshot.inbox)

    await session._commit_mid_turn_injection(msg_id)
    await settle(session)

    # (1) history
    assert len(session.history) == before_history_len + 1
    injected_entry = session.history[-1]
    assert injected_entry.role == "user"
    assert injected_entry.content == "inject me"

    # (2) snapshot prune
    assert not any(m["id"] == msg_id for m in session.journal.snapshot.inbox)

    # (3) turn_started promote
    turn_started_events = [e for e in events if e.type == "turn_started"]
    (only,) = turn_started_events
    assert only.data["kind"] == TurnOrigin.CLIENT_INPUT
    assert only.data["chain_id"] == peeked[0]["payload"]["chain_id"]

    await state_log.aclose()


# (#3792 added a test_commit_does_not_reset_hook_driven_turns here, pinning
# architect's point 4 — a mid-turn injection commit must not reset the
# loop-valve counter. #5561 retired the valve and the counter; there is
# nothing left for this test to pin, so it was deleted with them.)


# ---------------------------------------------------------------------------
# Truncate-falsify (CLAUDE.md recovery-feature PR gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_commit_truncate_falsify(tmp_path):
    """Tier 2: #3792 — CLAUDE.md recovery-feature PR gate. A committed
    injection's inbox entry (WAL ``inbox_consume`` tombstone + synchronous
    snapshot prune, ``SnapshotJournal.consume_inbox`` — the SAME mechanism
    ``cancel_queued`` uses, per ``tests/interfaces/test_3300_p3_cancel_by_id.py``'s
    identical shape) must NOT resurrect after a WAL truncation below its
    ``inbox_put``/``inbox_consume`` source events.

    Falsification: this reuses the exact mechanism
    ``test_3300_p3_cancel_by_id.py::test_cancel_snapshot_prune_survives_truncate``
    already falsify-verified for ``cancel_inbox`` — ``consume_inbox`` is the
    same shape (synchronous snapshot prune + WAL tombstone, mirrored per its
    own docstring), so this test is the #3792-specific instance of that
    already-proven mechanism, not a novel one.
    """
    wal = tmp_path / "state.wal"
    snapshot_path = tmp_path / "snapshot.json"
    session, state_log = _make_session(wal, snapshot_path)

    msg_id = await session.submit_user_text("inject me")
    keep_id = await session.submit_user_text("stays queued")
    await session._inbox_arbiter.peek_mid_turn_injections()
    await session._commit_mid_turn_injection(msg_id)
    await session.journal.flush()

    assert not any(m["id"] == msg_id for m in session.journal.snapshot.inbox)
    assert any(m["id"] == keep_id for m in session.journal.snapshot.inbox)

    pre_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert any(
        '"inbox_consume"' in ln and f'"msg_id": "{msg_id}"' in ln for ln in pre_truncate_lines
    ), "sanity: the inbox_consume source event must be durable pre-truncation"

    for i in range(150):
        await state_log.append(
            "inbox_put", n=i, target="filler-agent", msg_id=f"filler-{i}",
            msg_kind="user", payload={},
        )
    floor = state_log.current_seq - 5
    await state_log.truncate_below(floor)
    await state_log.flush()
    stats = state_log.last_truncate_stats
    assert stats["dropped"] >= 3, (
        f"the 3 real source events (2x inbox_put + 1x inbox_consume) must be "
        f"truncated below the floor; dropped={stats['dropped']}"
    )
    post_truncate_lines = [ln for ln in wal.read_text().splitlines() if ln.strip()]
    assert not any('"inbox_consume"' in ln for ln in post_truncate_lines), (
        "the inbox_consume source event must actually be gone from the WAL "
        "post-truncation (not just counted)"
    )

    await state_log.aclose()

    state_log2 = StateLog(wal)
    reconstructed = _reconstruct(AGENT, snapshot_path, state_log2)

    reconstructed_ids = {m["id"] for m in reconstructed.inbox}
    assert msg_id not in reconstructed_ids, (
        "the committed injection must NOT resurrect after WAL truncation "
        "below its own source events"
    )
    assert keep_id in reconstructed_ids, (
        "an uncommitted queued message must SURVIVE the same truncation — "
        "proving this is injection-commit correctness, not blanket data loss"
    )

    await state_log2.aclose()
