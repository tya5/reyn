"""Tier 2: #4995/#5267 — ``RouterHistoryBuffer``'s incremental elide cache
is published only by the session's CURRENT turn owner, never by a stale
one whose background work outlived its own cancellation.

Root cause (#5267, filed while starting #4995's candidate ① — moving
``build_history()`` off the TUI's own event loop onto a worker thread via
``asyncio.to_thread``): ``Session.cancel_inflight()`` hard-cancels the
turn's owning sub-task with ``Task.cancel()``, which "injects
``CancelledError`` at whatever await point the task is currently suspended
on" (that method's own docstring). Today ``build_history()`` is
synchronous, so a turn can never be suspended INSIDE it — cancel can only
land before or after. Dispatching it to a worker thread creates exactly
such a suspension point, but ``Task.cancel()`` only cancels the AWAIT — it
cannot, and does not, stop the underlying thread-pool worker already
executing. So a cancelled turn's ``build_history()`` call can keep running
in the background while the SAME session immediately starts its next
turn, whose OWN ``build_history()`` call can race the orphaned one over
the SAME instance's ``_cached_elide_*`` fields.

"Last write wins" is not a safe fallback here: ``_incremental_elide_total``
READS ``_cached_elide_total`` and ADDS to it (an incremental cache, not an
idempotent overwrite) — an interleaved stale write is not merely outdated,
it can corrupt the NEXT legitimate call's own incremental computation.

Fix (architect ruling via #5267, reusing #5217's own "build privately,
publish in one step" shape — no new mechanism): the caller captures
``asyncio.current_task()`` as ``expected_owner`` BEFORE dispatching to a
worker thread; ``RouterHistoryBuffer`` computes everything into local
variables and, at the very end, commits to ``_cached_elide_*`` only if
``expected_owner`` still matches ``current_turn_owner_fn()`` — Session's
LIVE ``_turn_owner_task``, read fresh (a bare attribute read/write is
atomic under the GIL; no lock needed for the comparison itself). No new
ownership register was invented — this reuses the EXISTING one
(``Session._turn_owner_task``), per lead-coder's own correction during
review (a first draft that invented an ``itertools.count()``-based
attempt counter was rejected for exactly this reason).

Real ``RouterHistoryBuffer`` + real ``ChatMessage`` — no mocks. The
"stale write" scenario is driven directly (calling ``build_history`` with
an explicit, now-mismatched ``expected_owner``) rather than via real
threads/cancellation — the unit under test is the ownership CHECK itself,
not the thread-scheduling race that motivates it (which #4995's own
future PR, wiring this into ``RouterLoopDriver`` via ``asyncio.to_thread``,
is responsible for exercising end-to-end).

No strip-falsify test lives in this file (review correction, #5275): an
earlier draft reconstructed the OLD unconditional-commit
``_incremental_elide_total`` BY HAND inside a test — that only proves a
hand-copied re-implementation of the old bug reproduces the old bug, a
tautology that stays green even if the REAL ownership check were removed
from production (CLAUDE.md's test-review Q3: nobody would miss it). The
actual strip-falsify — disabling the real ``still_owner`` check in
``router_history_buffer.py`` and confirming
``test_a_stale_owners_publish_is_rejected`` above goes RED — was done by
hand against the real source and is recorded in #5275's own PR body, not
duplicated here as a second, parallel implementation.
"""
from __future__ import annotations

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.router_history_buffer import RouterHistoryBuffer

_MODEL = "gpt-3.5-turbo"


def _turns(n: int, *, start: int = 1) -> list[ChatMessage]:
    return [
        ChatMessage(role="user", content=f"turn {i} " + ("x" * 40), seq=i)
        for i in range(start, start + n)
    ]


def _make_buf(history: list, *, current_turn_owner_fn=None) -> RouterHistoryBuffer:
    return RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: _MODEL,
        events=EventLog(),
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,  # #4552 PR-3
        non_interactive=True,
        current_turn_owner_fn=current_turn_owner_fn,
    )


def _counting_wrapper(monkeypatch) -> dict:
    """Installs the SAME counting technique #4403's own test file uses —
    returns a dict whose ``"n"`` key counts real
    ``estimate_tokens_for_any_turn`` calls from the moment this is called."""
    import reyn.services.compaction.engine as engine_module

    real_fn = engine_module.estimate_tokens_for_any_turn
    call_count = {"n": 0}

    def _counting(wt, model, *, use_chars4):
        call_count["n"] += 1
        return real_fn(wt, model, use_chars4=use_chars4)

    monkeypatch.setattr(engine_module, "estimate_tokens_for_any_turn", _counting)
    return call_count


def test_a_stale_owners_publish_is_rejected(monkeypatch) -> None:
    """Tier 2: acceptance — the exact #5267 witness. A call whose
    ``expected_owner`` no longer matches the CURRENT owner must not
    publish to the shared cache — proven by showing the cache still
    reflects the LEGITIMATE owner's own publish (via the #4403 counting
    technique: a subsequent legitimate call over UNCHANGED history costs
    ZERO new token estimates, which would not hold if the stale call had
    reverted the cache).

    The stale call's own ``turns``/``wire_turns`` are captured from a REAL
    50-turn snapshot taken BEFORE history grew to 60 — simulating "turn A
    already finished its own private computation off-thread; only the
    commit races turn B". Reading live (60-turn) history inside a second
    ``build_history()`` call would compute the SAME correct values turn B
    already published, making a broken ownership check indistinguishable
    from a working one — this is why the stale payload must come from the
    OLDER snapshot, matching what actually races in production."""
    history = _turns(50)
    owner_box = ["owner-A"]
    buf = _make_buf(history, current_turn_owner_fn=lambda: owner_box[0])

    # Turn A (currently the owner) builds and publishes normally.
    buf.build_history(expected_owner="owner-A")

    # Turn A's own private computation, from THIS (50-turn) snapshot —
    # captured now, "finished" but not yet committed, mirroring a
    # to_thread call that has returned its result but not yet reached the
    # ownership check at the end of _incremental_elide_total.
    turns_a, _watermark_a = buf._elide_candidate_turns(list(history))
    wire_turns_a = [buf._serialise_turn(m) for m in turns_a]

    # Simulate cancel + immediate retry: a NEW turn takes ownership,
    # extends history, and publishes legitimately BEFORE turn A's stale
    # computation above gets a chance to commit.
    owner_box[0] = "owner-B"
    history.extend(_turns(10, start=51))
    buf.build_history(expected_owner="owner-B")  # turn B: legitimate, publishes 60 turns

    # Turn A's orphaned computation finally "commits" — its own
    # expected_owner ("owner-A") no longer matches the current owner
    # ("owner-B"), so this must be rejected.
    stale_total = buf._incremental_elide_total(
        turns_a, wire_turns_a, use_chars4=True, expected_owner="owner-A",
    )
    assert isinstance(stale_total, int), (
        "a rejected-publish call must still return its own correct result "
        "to its own caller — only the SHARED cache write is skipped"
    )

    # Witness: turn B's own legitimate publish must still be what the
    # cache reflects. A further legitimate call (owner-B) over the SAME
    # (unchanged since turn B's publish) 60-turn history must cost ZERO
    # new token estimates — if turn A's stale commit HAD published
    # (reverting turn_count back to 50), this call would treat the 10
    # turns turn B already accounted for as "new" again.
    call_count = _counting_wrapper(monkeypatch)
    buf.build_history(expected_owner="owner-B")
    assert call_count["n"] == 0, (
        f"expected 0 new-turn estimates (cache already reflects the current "
        f"60-turn history), got {call_count['n']} — the stale call's commit "
        "was not actually rejected"
    )


def test_no_owner_configured_always_publishes_unchanged(monkeypatch) -> None:
    """Tier 2: non-regression — every pre-#5267 caller (no
    ``current_turn_owner_fn`` configured, no ``expected_owner`` passed)
    keeps publishing unconditionally, byte-identical to before this
    feature existed."""
    history = _turns(50)
    buf = _make_buf(history)  # no current_turn_owner_fn

    buf.build_history()
    history.extend(_turns(30, start=51))

    call_count = _counting_wrapper(monkeypatch)
    buf.build_history()
    assert call_count["n"] == 30, (
        f"expected exactly 30 new-turn estimates (incremental, unaffected "
        f"by the ownership feature when unconfigured), got {call_count['n']}"
    )


def test_the_legitimate_owners_own_publish_still_works(monkeypatch) -> None:
    """Tier 2: falsification contrast — when ``expected_owner`` DOES match
    the current owner, the cache publishes exactly as before (the feature
    only ever SUBTRACTS a publish, never adds a new inconsistency for the
    matching case)."""
    history = _turns(50)
    buf = _make_buf(history, current_turn_owner_fn=lambda: "the-one-owner")

    buf.build_history(expected_owner="the-one-owner")
    history.extend(_turns(30, start=51))

    call_count = _counting_wrapper(monkeypatch)
    buf.build_history(expected_owner="the-one-owner")
    assert call_count["n"] == 30, (
        f"a matching-owner call must still publish incrementally, got "
        f"{call_count['n']} new estimates instead of 30"
    )
