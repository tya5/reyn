"""Tier 2: /compact slash — handler paths (no-engine error, raises, nothing-to-compact, success).

`compact_cmd` has four distinct paths based on whether the compaction engine
is wired, whether it raises, and the `summarized_turns` value in its result.

#5708 (owner real-machine incident): before this, EVERY `summarized_turns
<= 0` result rendered as one of two hedged messages regardless of WHY
nothing was summarised — `force_compact_now` had no way to tell the caller
"already_running" apart from "genuinely nothing eligible" apart from "an
attempt ran but did not advance" apart from "an internal invariant was
violated", so `/compact` could not either (its own comment, verbatim,
named the defect: "this function has no way to tell them apart"). The 4
tests below (`test_compact_*_outcome_*`) pin that each of the `compaction_
outcome` values `Session._compact_now_for_op` now threads through from
`CompactionController.ForceCompactResult` produces its OWN, non-hedged
wording — the acceptance-item-① witness."""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.compact import compact_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.
    """
    return slash_ctx(session, recorder=session._outbox)


class _FakeSession:
    def __init__(self, *, compact_now=None) -> None:
        if compact_now is not None:
            self._compact_now_for_op = compact_now
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")


@pytest.mark.asyncio
async def test_compact_no_engine_sends_error() -> None:
    """Tier 2: /compact with no _compact_now_for_op wired replies an error."""
    session = _FakeSession()  # no compact_now attr
    await compact_cmd(_ctx(session), "")
    assert session.error_text(), "expected error reply when engine absent"
    assert not session.reply_text(), "expected no system reply when engine absent"


@pytest.mark.asyncio
async def test_compact_engine_raises_sends_error_with_message() -> None:
    """Tier 2: /compact when the engine raises surfaces the exception text, not a crash."""
    async def _raising(**_kw):
        raise RuntimeError("disk full")

    session = _FakeSession(compact_now=_raising)
    await compact_cmd(_ctx(session), "")
    err = session.error_text()
    assert err, "expected an error reply"
    assert "disk full" in err


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_no_free_window() -> None:
    """Tier 2: summarized_turns=0 without free_window_after → 'Nothing to compact' reply."""
    async def _nothing(**_kw):
        return {"summarized_turns": 0}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "nothing" in text.lower() or "already fits" in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_nothing_to_compact_with_free_window_includes_token_count() -> None:
    """Tier 2: #5579 accept ②' (deny side) — summarized_turns=0 with a
    GENUINELY free window (free_window_after > 0) still surfaces that token
    count in the reply. The deny-side counterweight to accept ①: the fix
    must not become "always warn regardless of free_window_after".

    #5888 updated what the SENTENCE around that number says. It used to
    read "recent history already fits the window", which is a claim about
    the model's context WINDOW made from `free_window_after` — a
    TRIGGER-relative number (see compact.py's own module docstring). The
    number still travels here, unchanged and orthogonal, exactly as #5579
    established; what it no longer does is supply a window claim it never
    measured."""
    async def _nothing(**_kw):
        return {"summarized_turns": 0, "free_window_after": 45000}

    session = _FakeSession(compact_now=_nothing)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "45000" in text, f"expected free token count in reply; got: {text!r}"
    assert "eligible" in text.lower(), (
        f"reply must name WHY nothing folded, not just that nothing did; got: {text!r}"
    )


@pytest.mark.asyncio
async def test_compact_nothing_summarized_never_claims_the_window_has_room() -> None:
    """Tier 2: #5579 accept ①', re-pinned for #5888 — the owner's own
    observed contradiction (summarized_turns=0 AND free_window_after=0 in
    the SAME reply, reading "already fits the window. Free window: ~0
    tokens.") must not happen.

    #5579 fixed it by suppressing the "already fits" phrase when
    `free_window_after == 0`; #5888 removes the phrase from every branch
    instead, because the claim was never this code path's to make from
    that number at all. This test therefore now pins the STRONGER
    property — no reply asserts the window fits/has room, whatever
    `free_window_after` says — and pins it on BOTH sides (0 and a
    genuinely-free 45000) so it cannot pass merely because one input
    happens to take a branch that never said it.

    Q4 note: with the phrase gone this assertion could be green over
    nothing, so each case also asserts the reply is a real, non-empty
    explanation — the thing that WOULD regress if a future edit
    reintroduced a window claim to fill the silence."""
    for free_after in (0, 45000):
        async def _stuck(**_kw):
            return {"summarized_turns": 0, "free_window_after": free_after}

        session = _FakeSession(compact_now=_stuck)
        await compact_cmd(_ctx(session), "")
        text = session.reply_text()
        assert text, f"expected a non-empty reply, not silence (free_after={free_after})"
        lowered = text.lower()
        assert "already fits" not in lowered and "fits the window" not in lowered, (
            "reply must never claim the window fits from a trigger-relative "
            f"number; got (free_after={free_after}): {text!r}"
        )
        assert "eligible" in lowered, (
            f"reply must still say WHY nothing folded; got: {text!r}"
        )
        assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_success_mentions_summarized_turns() -> None:
    """Tier 2: successful compaction (summarized_turns>0) surfaces the turn count."""
    async def _success(**_kw):
        return {
            "summarized_turns": 3,
            "compressed_tokens": 1200,
            "bridge_tokens": 180,
        }

    session = _FakeSession(compact_now=_success)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "3" in text, "turn count not in reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_success_singular_turn_word() -> None:
    """Tier 2: exactly 1 summarized turn uses singular 'turn' not 'turns'."""
    async def _one(**_kw):
        return {"summarized_turns": 1, "compressed_tokens": 400, "bridge_tokens": 60}

    session = _FakeSession(compact_now=_one)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "1" in text, f"count not in reply; got: {text!r}"
    assert "turn" in text, f"singular 'turn' not in reply; got: {text!r}"
    assert "turns" not in text


@pytest.mark.asyncio
async def test_compact_success_plural_turns_word() -> None:
    """Tier 2: multiple summarized turns uses plural 'turns'."""
    async def _many(**_kw):
        return {"summarized_turns": 5, "compressed_tokens": 2000, "bridge_tokens": 300}

    session = _FakeSession(compact_now=_many)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "turns" in text, f"plural not used; got: {text!r}"


# --- #5708: outcome-specific wording (the acceptance-① witness) -----------


@pytest.mark.asyncio
async def test_compact_outcome_already_running_names_the_cause() -> None:
    """Tier 2: `already_running` gets its own wording — "try again", not
    "nothing to compact" (a DIFFERENT actionable next step)."""
    async def _running(**_kw):
        return {"summarized_turns": 0, "compaction_outcome": "already_running"}

    session = _FakeSession(compact_now=_running)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "already running" in text.lower(), f"got: {text!r}"
    assert "already fits" not in text.lower()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_outcome_invariant_violated_is_an_error_not_a_hedge() -> None:
    """Tier 2: `compaction_input_gap_invariant_violated` is a genuine
    internal anomaly (compaction_controller.py's own comment: "a defensive
    invariant, not a routine outcome") — must surface as an error, not a
    quiet "nothing to compact"."""
    async def _violated(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "compaction_input_gap_invariant_violated",
        }

    session = _FakeSession(compact_now=_violated)
    await compact_cmd(_ctx(session), "")
    assert session.error_text(), "expected an error reply for an invariant violation"
    assert "invariant" in session.error_text().lower()
    assert not session.reply_text()


@pytest.mark.asyncio
async def test_compact_outcome_forced_sync_with_candidates_but_no_progress() -> None:
    """Tier 2: acceptance ② — `forced_sync` WITH candidates selected but
    `summarized_turns == 0` (the watermark never advanced) must read
    DIFFERENTLY from `forced_sync_no_turns`/`candidate_count == 0` below —
    an attempt genuinely ran and did not complete, not "nothing eligible"."""
    async def _stalled(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "forced_sync",
            "compaction_candidate_count": 7,
        }

    session = _FakeSession(compact_now=_stalled)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "7" in text, f"candidate count not surfaced; got: {text!r}"
    assert "did not complete" in text.lower() or "attempt" in text.lower(), (
        f"got: {text!r}"
    )
    assert "already fits" not in text.lower(), (
        "must not be conflated with the genuinely-nothing-eligible case"
    )
    assert "nothing eligible" not in text.lower(), (
        "must not be conflated with the genuinely-nothing-eligible case"
    )
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_outcome_forced_sync_failed_says_so_plainly() -> None:
    """Tier 2: acceptance ④ — `compaction_failed=True` (an exception the
    swallow in `force_compact_now` caught) must state the fact PLAINLY, as
    an error, not hedge with "may indicate a compaction failure" the way
    the unconfirmed-stall case does. (423 here is an arbitrary test value,
    not a claim about any specific real-machine incident.)"""
    async def _failed(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "forced_sync",
            "compaction_candidate_count": 423,
            "compaction_failed": True,
        }

    session = _FakeSession(compact_now=_failed)
    await compact_cmd(_ctx(session), "")
    err = session.error_text()
    assert err, f"expected an error reply for a confirmed failure, got reply: {session.reply_text()!r}"
    assert "423" in err, f"candidate count not surfaced; got: {err!r}"
    assert "failed" in err.lower()
    assert "may indicate" not in err.lower(), (
        "a CONFIRMED failure must not be hedged the same way the "
        "unconfirmed-stall case is"
    )
    assert not session.reply_text()


@pytest.mark.asyncio
async def test_compact_outcome_forced_sync_no_turns_still_says_nothing_eligible() -> None:
    """Tier 2: acceptance ② deny side — `forced_sync_no_turns` (nothing was
    ever ELIGIBLE) must stay distinct from the stalled-attempt case above
    (same `summarized_turns == 0`, different `compaction_outcome`).

    #5888 sharpened which fact this branch is allowed to state. It used to
    say "recent history already fits the window" (a window claim, from a
    trigger-relative number); it now says nothing is eligible to fold —
    which is precisely, and only, what `forced_sync_no_turns` means. The
    companion case (things WERE eligible, head/tail protected all of
    them) gets its own separate sentence — see
    `test_compact_all_eligible_protected_is_not_reported_as_nothing_
    eligible`."""
    async def _no_turns(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "forced_sync_no_turns",
            "eligible_count": 0,
            "free_window_after": 12000,
        }

    session = _FakeSession(compact_now=_no_turns)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    assert "eligible" in text.lower(), f"got: {text!r}"
    assert "did not complete" not in text.lower()
    assert "protected" not in text.lower(), (
        "nothing was eligible here — the all-protected sentence belongs to "
        f"the OTHER case; got: {text!r}"
    )
    assert not session.error_text()


# ── #5888: the reply names what it measured; "nothing eligible" is reserved ──


@pytest.mark.asyncio
async def test_compact_reply_names_the_four_quantities_it_measured() -> None:
    """Tier 2: #5888 accept ① — the reply carries window used/size, middle
    room/used, what head/tail protected, and the seq already folded
    through. The pre-#5888 reply had ONE number (`free_window_after`, the
    room left in the MIDDLE after head/tail/system-prompt budgets) and
    rendered it as "the window is still full (~0 tokens free)" — a claim
    about the model's context window, while the status bar read ctx 75%.

    strip witness: dropping the `_measured_line(result)` concatenation
    from compact.py's replies removes every one of these substrings and
    this test goes red (verified directly during this fix)."""
    async def _measured(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "forced_sync",
            "compaction_candidate_count": 0,
            "eligible_count": 12,
            "window_tokens": 200_000,
            "window_used_tokens": 150_000,
            "middle_room_tokens": 40_000,
            "middle_used_tokens": 8_000,
            "protected_head_tokens": 30_000,
            "protected_tail_tokens": 90_000,
            "protected_head_turns": 2,
            "protected_tail_turns": 9,
            "covers_through_seq": 71,
        }

    session = _FakeSession(compact_now=_measured)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()

    assert "150000/200000" in text, f"window used/size missing; got: {text!r}"
    assert "(75%)" in text, f"window percentage missing; got: {text!r}"
    assert "middle room 40000, used 8000" in text, f"middle room/used missing; got: {text!r}"
    assert "head 30000 + tail 90000 tokens (11 turns)" in text, (
        f"protected head/tail missing; got: {text!r}"
    )
    assert "already folded through seq 71" in text, f"covered seq missing; got: {text!r}"
    # The sentence the owner actually saw must not be reachable at all.
    assert "window is still full" not in text.lower(), (
        f"the trigger-relative number must never be spoken as the window; got: {text!r}"
    )


@pytest.mark.asyncio
async def test_compact_all_eligible_protected_is_not_reported_as_nothing_eligible() -> None:
    """Tier 2: #5888 accept ③ — eligible entries that head/tail protected
    in full get their OWN sentence, never "nothing eligible to fold".

    This is the owner's actual case: a history whose head+tail hold most
    of the tokens leaves `candidate_count == 0`, and the pre-#5888 branch
    reported that as "There was nothing eligible to fold" — telling the
    operator their history had no foldable content while it was full of
    it. The distinguishing fact is `eligible_count`, which is why it is
    now returned (see `ForceCompactResult`).

    strip witness: branching on `free_window_after` instead of
    `eligible_count` (the pre-#5888 shape) sends this case back into the
    nothing-eligible sentence and this test goes red."""
    async def _all_protected(**_kw):
        return {
            "summarized_turns": 0,
            "compaction_outcome": "forced_sync",
            "compaction_candidate_count": 0,
            "eligible_count": 7,
            "free_window_after": 0,
        }

    session = _FakeSession(compact_now=_all_protected)
    await compact_cmd(_ctx(session), "")
    text = session.reply_text()
    lowered = text.lower()

    assert "7 eligible" in lowered, f"expected the eligible COUNT named; got: {text!r}"
    assert "protected" in lowered, (
        f"expected the head/tail keep-window named as the cause; got: {text!r}"
    )
    assert "nothing eligible" not in lowered, (
        "eligible entries existed — this must not read as 'nothing eligible'; "
        f"got: {text!r}"
    )
    assert not session.error_text()


@pytest.mark.asyncio
async def test_compact_asks_for_operator_selection_not_the_reactive_shortfall() -> None:
    """Tier 2: #5888 accept ② (handler half) — `/compact` calls the session
    wrapper with `selection="operator"`, so the whole unprotected middle
    is folded rather than only the reactive ladder's shortfall.

    The controller half (what `selection="operator"` actually selects, and
    that `"shortfall"` still selects only the shortfall — #5719's own
    non-regression) is pinned in
    `tests/runtime/test_5888_operator_selection.py` against the REAL
    `CompactionController`; this test pins only that the handler asks for
    it, which is the half that lives here.

    strip witness: dropping the keyword (back to a bare `compact_now()`)
    makes `seen` read `"shortfall"` and this test goes red."""
    seen: list[str] = []

    async def _record(**kw):
        seen.append(kw.get("selection", "shortfall"))
        return {"summarized_turns": 0, "eligible_count": 0}

    session = _FakeSession(compact_now=_record)
    await compact_cmd(_ctx(session), "")

    assert seen == ["operator"], (
        "/compact is an explicit shrink request, not a fit-check — it must "
        f"ask for operator selection; got: {seen!r}"
    )
