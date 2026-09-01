"""Tier 2: #5618 — the shrink-progress gate rises during a REAL retry-ladder
recovery, the path that never lifted it before.

#5588 gated the TUI's progress row on ``Session.is_compacting``, which
forwarded only to ``CompactionController.is_compacting``. That flag is set
exclusively inside ``force_compact_now``; the overflow retry ladder calls the
compaction engine directly, so during a genuine recovery the flag stayed False
and the row was structurally invisible — the one moment a user most needs it
(owner, real machine, mid-recovery: "tui では進捗わからない"). ``is_compacting``
is now the OR of that flag and the loop driver's own recovery-episode state.

Everything here drives the REAL ladder: a real ``Session``, a real
``RouterLoopDriver``, a real ``MediaStore``, and
``_run_with_shrink_and_byte_reduction`` — the same entry the production turn
uses. The only fake is ``_ContentDrivenLoop``, a stand-in for ``RouterLoop``
that raises a 413-shaped error while a given payload is still present; it is
the established harness in
``tests/runtime/test_5296_pr2_byte_reduction_same_turn_retry.py``, reproduced
here rather than imported so this file stays self-contained.

**Why the assertions sample from inside the loop.** ``is_compacting`` is a
state with an exit, and the claim is about its value WHILE recovery runs — by
the time the drive returns, the episode is over by design. ``_ContentDrivenLoop
.run`` is called by the ladder itself, so recording the gate there reads it at
genuinely mid-flight moments on the real call path. This is a probe, not a
second implementation: it stores what the public API returned, and asserts
nothing on its own.

Nothing here writes a duration. The ladder terminates on its own content
predicate, and every assertion is over recorded values, never over how long
anything took.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.services.compaction.engine import UnrecoveredError
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    """The real, WAL-durable write path — a bare ``session.history.append``
    leaves the durable store empty, and the compaction candidate read is from
    the durable store (#4472)."""
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _has_content(history: "list[dict]", needle: str) -> bool:
    return any(needle in str(m.get("content", "")) for m in history)


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class _ProbingLoop:
    """A fake ``RouterLoop`` that raises a 413-shaped error while
    ``should_fail(history, user_text)`` holds, and records the shrink-progress
    gate on EVERY call.

    The recording is the point: ``run`` is invoked once before the ladder is
    entered and again from inside it, so ``samples`` ends up holding the gate's
    value at real mid-recovery moments as well as outside one.
    """

    def __init__(self, session, should_fail) -> None:
        self._session = session
        self._should_fail = should_fail
        self.calls: "list[list[dict]]" = []
        self.samples: "list[dict]" = []

    def _sample(self) -> None:
        raw = self._session.compaction_progress_raw()
        self.samples.append({
            "is_compacting": raw["is_compacting"],
            "call_count": raw["upstream_recovery_call_count"],
            "raw_middle_remaining": raw["raw_middle_remaining"],
            # The discriminator architect asked for: the controller's own flag,
            # read at the SAME instant. It stays False on this path, so a True
            # gate above can only have come from the new recovery-episode
            # state. This is the one internal read here, and it is the whole
            # point of the pairing — without it, a "fix" that made the
            # controller flag rise on the ladder path would pass unnoticed.
            "controller": self._session._compaction_controller.is_compacting,
        })

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        self.calls.append(history)
        self._sample()
        if self._should_fail(history, user_text):
            raise _FakeStatusError("request too large", status_code=413)
        return None


def _make_spill_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real Session with a real MediaStore — the default ``make_session``
    passes ``media_store=None``, and the spill mechanism needs a real one to
    have any effect at all."""
    monkeypatch.chdir(tmp_path)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
        max_shrink_iterations=1,
        recovery_policy="next_turn",
        spill_granularity="turn",
    )
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _drive_a_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up a single oversized tool result and run the real ladder over it.

    One huge tool result is the cheapest history that genuinely overflows and
    genuinely recovers: the spill retires it and the retry succeeds, so the
    drive returns normally instead of terminating unrecovered.
    """
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")

    loop = _ProbingLoop(session, lambda history, _user_text: _has_content(history, huge))
    result = asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "continue please", chain_id="c1",
        )
    )
    return session, loop, result


# ── accept ─────────────────────────────────────────────────────────────────


def test_the_gate_rises_during_a_real_retry_ladder_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5618's acceptance criterion. During a recovery that runs the
    ladder — never touching ``force_compact_now`` — the progress gate reads
    True at least once. Before #5618 every sample here read False, which is
    exactly why the row never appeared on a real overflow.

    Paired with the controller's own flag sampled at the same instant: it is
    False throughout, so this is a genuine new signal and not the old one
    happening to fire. Without that pairing the test would still pass if
    someone "fixed" this by making the controller flag rise on the ladder
    path, which is the design architect ruled out (the ladder is not a
    controller-driven compaction)."""
    _session, loop, _result = _drive_a_recovery(tmp_path, monkeypatch)

    assert loop.samples, "the fake loop was never called — the ladder never ran"
    lifted = [s for s in loop.samples if s["is_compacting"]]
    assert lifted, (
        f"the shrink-progress gate never rose during a real ladder recovery — "
        f"this is #5618 itself. samples={loop.samples!r}"
    )
    assert all(not s["controller"] for s in loop.samples), (
        f"the compaction CONTROLLER's flag rose on the ladder path; the gate "
        f"above would then prove nothing new: {loop.samples!r}"
    )


def test_the_measured_figures_are_reported_while_their_episode_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the accept side of #5618's episode join. The figures the row
    displays are cached from #5592's own events and returned ONLY when the
    episode they were stamped with is the one running — so this pins that a
    real, in-flight episode does return them. Without it, a join that always
    reported ``None`` would satisfy every deny test in this file while showing
    the user a permanently empty row."""
    _session, loop, _result = _drive_a_recovery(tmp_path, monkeypatch)

    measured = [
        s for s in loop.samples
        if s["call_count"] is not None or s["raw_middle_remaining"] is not None
    ]
    assert measured, (
        f"no sample carried a measured figure while its own episode was "
        f"running — the join is hiding live values, not stale ones: "
        f"{loop.samples!r}"
    )
    assert all(s["is_compacting"] for s in measured), (
        f"a figure was reported for an episode the gate says is not running: "
        f"{measured!r}"
    )


# ── deny ───────────────────────────────────────────────────────────────────


def test_the_gate_falls_again_once_the_recovery_has_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: deny ①. The episode has an exit, and it runs: after a recovery
    that SUCCEEDED, the gate reads False again. Strip the inner scope's
    ``finally`` and this is the test that goes red — the depth never returns
    to 0 and the row stays lit for the rest of the session."""
    session, _loop, _result = _drive_a_recovery(tmp_path, monkeypatch)

    assert session.is_compacting is False
    assert session.compaction_progress_raw()["is_compacting"] is False


def test_a_finished_episodes_figures_are_not_reported_afterwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the freshness rule, witnessed on the real path. Figures WERE
    genuinely measured and cached during the drive (the accept test above
    reads them mid-flight), yet once that episode has ended they read
    ``None`` — unknown, which is what they honestly are about any later
    moment.

    This is the same mechanism that keeps the previous episode's numbers out
    of the NEXT one: the join compares the stamp against the current episode
    number, and a stamp from episode N matches neither "no episode" nor
    episode N+1. Nothing is ever cleared, so there is no window in which a
    real figure has been erased and its replacement has not yet arrived."""
    session, loop, _result = _drive_a_recovery(tmp_path, monkeypatch)

    measured = [
        s for s in loop.samples
        if s["call_count"] is not None or s["raw_middle_remaining"] is not None
    ]
    assert measured, (
        "nothing was measured during the drive, so this test would pass "
        "vacuously — it must witness real figures going stale, not absent "
        "ones staying absent"
    )

    raw = session.compaction_progress_raw()
    assert raw["upstream_recovery_call_count"] is None, raw
    assert raw["raw_middle_remaining"] is None, raw
    assert raw["raw_middle_total"] is None, raw


def test_the_gate_falls_again_when_the_recovery_terminates_unrecovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: deny ②. The failure exit, which is the one a bare
    ``flag = False`` after the call would miss: the ladder raises
    ``UnrecoveredError`` straight through both episode scopes, and the
    ``finally`` in each is what still brings the depth back to 0.

    The overflow here is the new message itself — nothing in history to spill
    or fold, so both reduction axes are dry on the first attempt and the
    wrapper re-raises rather than looping."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "hello")

    loop = _ProbingLoop(session, lambda _history, _user_text: True)
    with pytest.raises(UnrecoveredError):
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "X" * 200_000, chain_id="c-fail",
            )
        )

    assert session.is_compacting is False
    assert session.compaction_progress_raw()["is_compacting"] is False


def test_an_ordinary_turn_never_lifts_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: deny ③. A turn that never overflows never enters the ladder, so
    the gate is False at every sample — the row must not appear on ordinary
    traffic. This is the criterion that stops "fix #5618 by reporting True
    more often" from passing."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "hello")

    loop = _ProbingLoop(session, lambda _history, _user_text: False)
    asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            loop, "just a normal question", chain_id="c-ok",
        )
    )

    assert loop.samples, "the fake loop was never called"
    assert all(not s["is_compacting"] for s in loop.samples), loop.samples
    assert session.is_compacting is False


# ── episode identity ───────────────────────────────────────────────────────


def test_a_previous_recoverys_figures_are_never_shown_during_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the join's premise, stated as what the user would actually see.
    Two separate recoveries on the same session must not share an identity, or
    the first one's figures would render as the second one's progress — a row
    that appears to be making progress it has not made.

    Asserted through the public read rather than on the episode number itself:
    the number is an implementation detail of the join, while "recovery 2 never
    displays recovery 1's numbers" is the property that matters and the one
    that stays true if the join is ever implemented differently.

    The second recovery is driven by an oversized NEW MESSAGE rather than by
    history. After the first recovery spilled the oversized tool result that
    same history no longer overflows, so repeating the first scenario would
    silently not recover at all and this test would assert over an empty list
    — green having had nothing to bite on."""
    session = _make_spill_session(tmp_path, monkeypatch)

    _push(session, "user", "look something up")
    huge = "Y" * 50_000
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")
    _push(session, "assistant", "ok, done")
    first_loop = _ProbingLoop(
        session, lambda history, _user_text: _has_content(history, huge),
    )
    asyncio.run(
        session._loop_driver._run_with_shrink_and_byte_reduction(
            first_loop, "continue please", chain_id="c-a",
        )
    )

    measured_first = [
        (s["call_count"], s["raw_middle_remaining"])
        for s in first_loop.samples
        if s["call_count"] is not None or s["raw_middle_remaining"] is not None
    ]
    assert measured_first, (
        "the first recovery measured nothing, so there are no stale figures "
        "for the second one to wrongly display — this test would pass "
        "vacuously"
    )

    second_loop = _ProbingLoop(session, lambda _history, _user_text: True)
    with pytest.raises(UnrecoveredError):
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                second_loop, "X" * 200_000, chain_id="c-b",
            )
        )

    assert second_loop.samples, "the second drive never called the loop"
    leaked = [
        s for s in second_loop.samples
        if (s["call_count"], s["raw_middle_remaining"]) in measured_first
        and (s["call_count"] is not None or s["raw_middle_remaining"] is not None)
    ]
    assert not leaked, (
        f"the second recovery displayed the first one's figures "
        f"({leaked!r} also seen in {measured_first!r}) — the join let a "
        f"finished episode's numbers render as this one's progress"
    )


def test_the_figures_do_not_blank_out_between_the_ladders_own_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: once a figure has been reported during a recovery, no later
    sample in that SAME recovery reports unknown while the gate is still up.

    **What this does and does not witness.** Samples are taken where the ladder
    calls the loop, which is a handful of points across the whole recovery. At
    that resolution this pins the ordinary case (a figure, once shown, keeps
    being shown) but it does NOT catch every way the join could blank a live
    figure — in particular it did not catch the two-identity defect described
    below, which was found by instrumenting the episode number directly during
    development and is NOT covered by any test here. Disclosed rather than
    implied: the production consumer polls on its own ~1s heartbeat and so
    samples far more finely than this test can.

    The defect, kept on the record because the fix it produced is load-bearing:
    the ladder opens an episode scope, and the wrapper opens a SECOND one from
    its `except` clause AFTER the first has already unwound, so a naive
    "0->1 allocates a number" rule handed one uninterrupted recovery two
    identities (measured during development: 1, 1, 2 across a single drive).
    Because figures are joined against the episode they were stamped in,
    everything measured before the second identity was allocated would be
    judged stale at that instant, and the row would go empty in the middle of
    the recovery the user is watching. ``_recovery_episode_scope``'s
    ``continues_previous`` is what prevents it."""
    _session, loop, _result = _drive_a_recovery(tmp_path, monkeypatch)

    live = [s for s in loop.samples if s["is_compacting"]]
    assert live, "the gate never rose, so this test would pass vacuously"

    seen_a_figure = False
    for sample in live:
        has_figure = (
            sample["call_count"] is not None
            or sample["raw_middle_remaining"] is not None
        )
        if has_figure:
            seen_a_figure = True
            continue
        assert not seen_a_figure, (
            f"a figure was reported earlier in this recovery and then went "
            f"back to unknown while the gate was still up: {live!r}"
        )
    assert seen_a_figure, (
        "no figure was ever reported during the recovery, so the blanking "
        "this test is about could not have been observed"
    )
