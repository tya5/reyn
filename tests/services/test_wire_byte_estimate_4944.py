"""Tier 1: #4944① — the byte-axis counterpart of the existing token
estimators (``estimate_tokens_for_turn`` / ``_estimate_tokens_list``),
measuring the SAME wire boundary (``_serialise_turn``'s output,
router_history_buffer.py — #2957 PR-B's "CANONICAL quantity" ruling) on the
byte axis instead of the token axis. Exists because an HTTP 413 (a
request-BODY-BYTE limit, #4885/#4944) says nothing about tokens — nothing
in this repo could answer "how many bytes will this turn put on the wire"
before this.

Policy compliance:
- No unittest.mock / MagicMock / AsyncMock / patch — pure functions, real
  inputs only.
- No private-state assertions.
- No len(result) == N byte-count pinning beyond what each test's own
  constructed input determines (never an opaque literal).
- Each docstring opens with ``Tier 1: ...``.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.services.token_multiplier_learner import TokenMultiplierLearner
from reyn.services.compaction.engine import (
    ChatSummary,
    ComputedBudgets,
    _estimate_bytes_list,
    estimate_turn_bytes,
    estimate_wire_bytes,
    retry_loop,
)


def test_estimate_turn_bytes_matches_the_turns_own_wire_json_size() -> None:
    """Tier 1: estimate_turn_bytes returns exactly the UTF-8 byte length of
    ``json.dumps(turn, ensure_ascii=False)`` — not an approximation, the
    real measurement the function's own docstring promises. Derived from
    the turn's own content, never a hardcoded literal (no pin)."""
    turn = {"role": "user", "content": "hello world", "seq": 1}
    expected = len(json.dumps(turn, ensure_ascii=False).encode("utf-8"))
    assert estimate_turn_bytes(turn) == expected


def test_estimate_turn_bytes_grows_with_content_length() -> None:
    """Tier 1: a longer content string produces a strictly larger byte
    estimate — the function is actually reading content size, not a fixed
    per-turn constant (the failure mode #4944 diagnosed for the existing
    per-image TOKEN estimate, which the byte axis must not repeat)."""
    small = {"role": "user", "content": "x", "seq": 1}
    large = {"role": "user", "content": "x" * 10_000, "seq": 1}
    assert estimate_turn_bytes(large) > estimate_turn_bytes(small)


def test_estimate_turn_bytes_counts_non_ascii_as_utf8_bytes_not_chars() -> None:
    """Tier 1: a non-ASCII character can be multiple UTF-8 bytes — the byte
    estimate must reflect ENCODED size, not character count (this is
    exactly the axis a token/char estimator would get wrong for the byte
    limit this function exists to measure)."""
    ascii_turn = {"role": "user", "content": "a", "seq": 1}
    multibyte_turn = {"role": "user", "content": "あ", "seq": 1}  # 3 UTF-8 bytes
    assert estimate_turn_bytes(multibyte_turn) > estimate_turn_bytes(ascii_turn)
    # Exact expected delta: the JSON encodes "あ" as 3 raw UTF-8 bytes with
    # ensure_ascii=False (vs 1 for "a") — derived from the real encoding,
    # not assumed.
    expected_delta = (
        len(json.dumps({"role": "user", "content": "あ", "seq": 1}, ensure_ascii=False).encode("utf-8"))
        - len(json.dumps({"role": "user", "content": "a", "seq": 1}, ensure_ascii=False).encode("utf-8"))
    )
    assert estimate_turn_bytes(multibyte_turn) - estimate_turn_bytes(ascii_turn) == expected_delta


def test_estimate_bytes_list_sums_each_turns_estimate() -> None:
    """Tier 1: _estimate_bytes_list is exactly the sum of estimate_turn_bytes
    over each turn — not an implementation-transcribed identity (checked
    against turns whose individual sizes were computed independently
    above, not by re-deriving the same expression)."""
    turns = [
        {"role": "user", "content": "a", "seq": 1},
        {"role": "user", "content": "bb", "seq": 2},
        {"role": "user", "content": "ccc", "seq": 3},
    ]
    individually_summed = sum(estimate_turn_bytes(t) for t in turns)
    assert _estimate_bytes_list(turns) == individually_summed


def test_estimate_bytes_list_empty_is_zero() -> None:
    """Tier 1: an empty turn list has zero wire bytes — the additive
    identity, not a fixed overhead constant."""
    assert _estimate_bytes_list([]) == 0


def test_estimate_wire_bytes_is_history_plus_sp_bytes() -> None:
    """Tier 1: #4944① — estimate_wire_bytes sums SP + head + summary + tail
    + new_msg wire bytes, mirroring retry_loop's own token-axis ``estimate``
    (SP + head + summary + tail + new_msg tokens, engine.py's success path)
    component-for-component on the byte axis."""
    SP = "system prompt text"
    head = [{"role": "user", "content": "h", "seq": 1}]
    summary = {"topic_arc": "stub", "covers_through_seq": 1}
    tail = [{"role": "user", "content": "t", "seq": 2}]
    new_msg = {"role": "user", "content": "q", "seq": 3}

    expected = (
        len(SP.encode("utf-8"))
        + _estimate_bytes_list(head)
        + len(json.dumps(summary, ensure_ascii=False).encode("utf-8"))
        + _estimate_bytes_list(tail)
        + estimate_turn_bytes(new_msg)
    )
    actual = estimate_wire_bytes(
        SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg,
    )
    assert actual == expected


def test_estimate_wire_bytes_none_summary_contributes_zero() -> None:
    """Tier 1: summary=None (no compaction has happened yet) contributes 0
    bytes, not a serialised "null" literal's bytes — mirrors retry_loop's
    own token-axis handling of summary=None (``json.dumps(summary) if
    summary else ""`` in the success-path estimate)."""
    SP = "sp"
    head: list[dict] = []
    tail: list[dict] = []
    new_msg = {"role": "user", "content": "q", "seq": 1}

    with_none = estimate_wire_bytes(SP=SP, head=head, summary=None, tail=tail, new_msg=new_msg)
    without_summary_component = len(SP.encode("utf-8")) + estimate_turn_bytes(new_msg)
    assert with_none == without_summary_component


def test_estimate_wire_bytes_grows_when_a_large_image_data_url_is_in_tail() -> None:
    """Tier 1: #4944's own motivating scenario — a materialised image
    (a large inline ``data:`` URL, the wire form ``_serialise_turn``
    produces for a path-ref image part per its own docstring) inflates the
    BYTE estimate by roughly its base64 size, even though a token
    estimator would price every image at the SAME fixed cost regardless of
    size (``_IMAGE_FIXED_TOKEN_COST``, engine.py — the exact blind spot
    #4944 diagnosed). This is the byte axis actually seeing what the token
    axis cannot."""
    small_image_tail = [{
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        "seq": 1,
    }]
    large_image_tail = [{
        "role": "user",
        "content": [{
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + ("A" * 1_000_000)},
        }],
        "seq": 1,
    }]
    small = estimate_wire_bytes(
        SP="sp", head=[], summary=None, tail=small_image_tail,
        new_msg={"role": "user", "content": "q", "seq": 2},
    )
    large = estimate_wire_bytes(
        SP="sp", head=[], summary=None, tail=large_image_tail,
        new_msg={"role": "user", "content": "q", "seq": 2},
    )
    assert large - small > 900_000, (
        f"a ~1MB larger data URL should inflate the byte estimate by "
        f"roughly that much; got a delta of only {large - small}"
    )


# ---------------------------------------------------------------------------
# #4944①: the primitive has a real day-1 consumer — retry_loop's success
# path emits ``compaction_wire_bytes_measured`` (lead-coder's condition on
# this PR: a measurement primitive with zero callers is indistinguishable,
# from the outside, from a declared-but-unwired one — #4941's own
# declaration≠guarantee lesson). This is "measure and report", not yet
# "measure and decide" (#4944②/③ wire a decision on top, not here).
# ---------------------------------------------------------------------------


class _MinimalCompactionEngine:
    """Smallest real collaborator retry_loop needs — the budgets/_events/
    _T_comp_SP surface every test in this file reads, plus a real (if
    trivial) ``compact()`` so a byte-limit-exhaustion fixture's own
    head/tail-floor shrink (#5531 PR-2: both Phase 1 and Phase 2 can now
    genuinely reach 0 as the reservation ladder halves the ceiling) has
    something real to fold into, rather than raising ``AttributeError``.

    #5531 PR-2 finding: this class used to have NO ``compact()`` at all
    (docstring claimed "no compact() call is exercised here, raw_middle
    stays empty") — false even pre-PR-2: once head/tail's derived
    minimums reach 0, `raw_middle` DOES receive content, and `compact()`
    WAS being called, raising AttributeError, silently absorbed by
    retry_loop's own "every compact()-call exception recovers by
    default" handling (#3783 stage 3) as a single-turn-floor "defer".
    That produced a genuinely fragile 2-iteration oscillation (a real
    413 from main_call, then an AttributeError from the missing
    compact(), alternating) whose OUTCOME happened to depend on whether
    ``max_iterations`` landed on an odd or even phase of that cycle — a
    real ``compact()`` removes the oscillation and the fragility with
    it, independent of any iteration-count coincidence."""

    def __init__(self) -> None:
        self.budgets = ComputedBudgets(
            main_pool=10_000, head_budget=1_000, body_budget=500,
            tail_budget=1_500, new_msg_budget=1_000,
            B_M=8_000, main_M_room=7_000, effective_trigger=7_000,
            section_caps={"topic_arc": 50, "decisions": 200, "pending": 150,
                          "session_user_facts": 50, "artifacts_referenced": 175},
        )
        self._events = EventLog()
        self._T_comp_SP = 100

    async def compact(self, input_chunk, *, covers_through=None):
        return ChatSummary(topic_arc="stub", covers_through_seq=0)


def test_retry_loop_success_emits_compaction_wire_bytes_measured() -> None:
    """Tier 2: #4944① — a successful retry_loop call emits
    ``compaction_wire_bytes_measured`` with a ``wire_bytes`` value equal to
    ``estimate_wire_bytes`` computed independently over the SAME inputs —
    the real consumer this PR's primitive needs (per lead-coder: a measured-
    but-never-emitted primitive is a declaration with no witness of
    reachability, the same shape #4941's declaration≠guarantee lesson
    warns about)."""
    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    SP = "system prompt"
    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    new_msg = {"role": "user", "content": "hello", "seq": 3}

    async def _success_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=800), choices=[])

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    asyncio.run(retry_loop(
        SP=SP, head=head, raw_middle=[],
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_success_call,
    ))

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured, "expected at least one compaction_wire_bytes_measured event on success"
    expected = estimate_wire_bytes(SP=SP, head=head, summary=None, tail=tail, new_msg=new_msg)
    assert all(e.data.get("wire_bytes") == expected for e in measured), (
        f"every compaction_wire_bytes_measured event on this single-call, "
        f"single-iteration success must carry the same measured value "
        f"{expected}; got {[e.data.get('wire_bytes') for e in measured]!r}"
    )


def test_retry_loop_never_emits_compaction_wire_bytes_measured_for_a_non_byte_overflow() -> None:
    """Tier 2: #4944① — a plain (non-413) overflow that never succeeds
    emits ZERO ``compaction_wire_bytes_measured`` events. This event exists
    to bound a request-BODY-BYTE limit specifically; a token-only overflow
    (no ``status_code == 413`` cause) says nothing about that limit, so
    emitting here would be a diagnostic false lead, not a true bound."""
    from reyn.services.compaction.engine import ContextOverflowError, UnrecoveredError

    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    async def _always_overflow(**kwargs):
        raise ContextOverflowError("simulated overflow")

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    try:
        asyncio.run(retry_loop(
            SP="sp", head=[{"role": "user", "content": "h", "seq": 1}],
            raw_middle=[],
            tail=[{"role": "user", "content": "t", "seq": 2}],
            new_msg={"role": "user", "content": "q", "seq": 3},
            cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
        ))
    except UnrecoveredError:
        pass

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured == [], (
        f"a run that never succeeded must emit zero compaction_wire_bytes_"
        f"measured events; got {[e.data for e in measured]!r}"
    )


def test_retry_loop_success_event_carries_accepted_true() -> None:
    """Tier 2: #4944① — the success-side emission carries ``accepted=True``
    (the size that WAS sent and succeeded — a lower bound on the real
    limit), distinguishing it from the failure-side emission below."""
    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    async def _success_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=800), choices=[])

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    asyncio.run(retry_loop(
        SP="sp", head=[{"role": "user", "content": "h", "seq": 1}],
        raw_middle=[], tail=[{"role": "user", "content": "t", "seq": 2}],
        new_msg={"role": "user", "content": "q", "seq": 3},
        cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner, main_call=_success_call,
    ))

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured, "expected at least one compaction_wire_bytes_measured event on success"
    assert all(e.data.get("accepted") is True for e in measured), (
        f"every success-path emission must carry accepted=True; got "
        f"{[e.data.get('accepted') for e in measured]!r}"
    )


def test_retry_loop_that_only_413s_still_emits_wire_bytes_with_accepted_false() -> None:
    """Tier 2: #4944① — lead-coder's TESTS-READ finding on this PR's first
    version, now fixed: a turn whose EVERY attempt gets a real HTTP 413
    (owner's own real-machine shape, never reaching success) used to emit
    ZERO ``compaction_wire_bytes_measured`` events — no diagnostic trail at
    all for exactly the case #4944 exists to diagnose. It must now emit
    one ``accepted=False`` event per recovered 413 iteration, each
    carrying the byte size that WAS SENT and REJECTED (an upper bound on
    the real limit)."""
    from reyn.services.compaction.engine import UnrecoveredError

    class _FakeStatusError(Exception):
        def __init__(self, message: str, *, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_413(**kwargs):
        from reyn.services.compaction.engine import ContextOverflowError
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    try:
        asyncio.run(retry_loop(
            SP="sp", head=head, raw_middle=[], tail=tail,
            new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner, main_call=_always_413,
        ))
    except UnrecoveredError:
        pass

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured, (
        "a run whose every attempt 413s must still emit at least one "
        "compaction_wire_bytes_measured event — this is the exact gap "
        "the fix closes"
    )
    assert all(e.data.get("accepted") is False for e in measured), (
        f"every emission on a run that never succeeded must carry "
        f"accepted=False; got {[e.data.get('accepted') for e in measured]!r}"
    )
    assert all(e.data.get("wire_bytes", 0) > 0 for e in measured), (
        f"the rejected size must be a real positive measurement, not a "
        f"placeholder; got {[e.data.get('wire_bytes') for e in measured]!r}"
    )


# ---------------------------------------------------------------------------
# #5316: the per-component breakdown estimate_wire_bytes threw away
# ---------------------------------------------------------------------------


def test_estimate_wire_bytes_breakdown_components_sum_to_the_total() -> None:
    """Tier 1: #5316 — estimate_wire_bytes_breakdown's 5 fields sum to
    exactly what estimate_wire_bytes (the pre-#5316 total-only function)
    returns for the SAME inputs — the split adds visibility, it does not
    change the measured quantity."""
    from reyn.services.compaction.engine import estimate_wire_bytes_breakdown

    SP = "system prompt text"
    head = [{"role": "user", "content": "h" * 50, "seq": 1}]
    summary = {"topic_arc": "stub", "covers_through_seq": 1}
    tail = [{"role": "user", "content": "t" * 30, "seq": 2}]
    new_msg = {"role": "user", "content": "q" * 20, "seq": 3}

    breakdown = estimate_wire_bytes_breakdown(
        SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg,
    )
    total = estimate_wire_bytes(SP=SP, head=head, summary=summary, tail=tail, new_msg=new_msg)
    assert breakdown.total == total
    assert (
        breakdown.sp_bytes + breakdown.head_bytes + breakdown.summary_bytes
        + breakdown.tail_bytes + breakdown.new_msg_bytes
    ) == total


def test_estimate_wire_bytes_breakdown_isolates_which_component_dominates() -> None:
    """Tier 1: #5316's own reason to exist — a payload where ONE component
    is far larger than the others must show up as that one component's
    field being far larger, not just a bigger opaque total (the exact
    "history支配/単品大/最新message単体のどれか" diagnostic #5316's issue
    body names)."""
    from reyn.services.compaction.engine import estimate_wire_bytes_breakdown

    huge_tail = [{"role": "tool", "content": "x" * 100_000, "seq": 1}]
    breakdown = estimate_wire_bytes_breakdown(
        SP="sp", head=[{"role": "user", "content": "h", "seq": 2}],
        summary=None, tail=huge_tail,
        new_msg={"role": "user", "content": "q", "seq": 3},
    )
    assert breakdown.tail_bytes > breakdown.sp_bytes
    assert breakdown.tail_bytes > breakdown.head_bytes
    assert breakdown.tail_bytes > breakdown.new_msg_bytes


def test_retry_loop_success_event_carries_the_breakdown_fields() -> None:
    """Tier 2: #5316 — the real ``compaction_wire_bytes_measured`` event
    (not a unit-tested helper in isolation) carries the 5 breakdown fields,
    each matching an INDEPENDENTLY computed value over the same inputs —
    not merely present, but correct."""
    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    SP = "system prompt"
    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    new_msg = {"role": "user", "content": "hello", "seq": 3}

    async def _success_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=800), choices=[])

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    asyncio.run(retry_loop(
        SP=SP, head=head, raw_middle=[], tail=tail, new_msg=new_msg,
        cfg=cfg, model="test-model", engine=engine,  # type: ignore[arg-type]
        learner=learner, main_call=_success_call,
    ))

    (event,) = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    expected_sp = len(SP.encode("utf-8"))
    expected_head = _estimate_bytes_list(head)
    expected_tail = _estimate_bytes_list(tail)
    expected_new_msg = estimate_turn_bytes(new_msg)
    assert event.data.get("sp_bytes") == expected_sp
    assert event.data.get("head_bytes") == expected_head
    assert event.data.get("summary_bytes") == 0
    assert event.data.get("tail_bytes") == expected_tail
    assert event.data.get("new_msg_bytes") == expected_new_msg
    assert (
        event.data.get("sp_bytes") + event.data.get("head_bytes")
        + event.data.get("summary_bytes") + event.data.get("tail_bytes")
        + event.data.get("new_msg_bytes")
    ) == event.data.get("wire_bytes")


# ---------------------------------------------------------------------------
# #5316: the learned byte limit, read back (not re-measured) into the
# terminal UnrecoveredError message
# ---------------------------------------------------------------------------


def test_learned_byte_limit_clause_with_both_bounds_names_both() -> None:
    """Tier 1: #5316 — with both an accepted (lower) and rejected (upper)
    bound observed, the clause names both, derived from the inputs, not a
    hardcoded template check."""
    from reyn.services.compaction.engine import _learned_byte_limit_clause

    clause = _learned_byte_limit_clause(
        last_accepted_wire_bytes=12_345, last_rejected_wire_bytes=20_000,
    )
    assert "12345" in clause  # no thousands separator — the actual, single format
    assert str(20_000) in clause


def test_learned_byte_limit_clause_with_no_accepted_bound_degrades() -> None:
    """Tier 1: #5316 — a turn whose FIRST attempt already 413s has no
    accepted bound yet; the clause must still name the rejected bound
    without fabricating an accepted one."""
    from reyn.services.compaction.engine import _learned_byte_limit_clause

    clause = _learned_byte_limit_clause(
        last_accepted_wire_bytes=None, last_rejected_wire_bytes=20_000,
    )
    assert str(20_000) in clause
    assert "accepted this turn" in clause.lower() or "no smaller" in clause.lower()


def test_learned_byte_limit_clause_with_no_rejected_bound_is_empty() -> None:
    """Tier 1: #5316 — with no rejected bound at all (should not arise on a
    genuine byte-limit terminal path, but the function must not fabricate
    content it was not given), the clause is the empty string — a no-op
    append onto the base message."""
    from reyn.services.compaction.engine import _learned_byte_limit_clause

    assert _learned_byte_limit_clause(
        last_accepted_wire_bytes=5_000, last_rejected_wire_bytes=None,
    ) == ""


def test_retry_loop_that_only_413s_names_the_learned_limit_in_the_terminal_message() -> None:
    """Tier 2: #5316 — THE end-to-end proof. A turn whose every attempt
    413s (same fixture as the pre-#5316 accepted=False regression test
    above) raises UnrecoveredError whose message names the actual rejected
    byte size that was observed THIS run — not a generic "413 occurred"
    with no number attached. No accepted bound is claimed (every attempt
    in this fixture failed)."""
    from reyn.services.compaction.engine import UnrecoveredError

    class _FakeStatusError(Exception):
        def __init__(self, message: str, *, status_code: int) -> None:
            super().__init__(message)
            self.status_code = status_code

    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    head = [{"role": "user", "content": "h", "seq": 1}]
    tail = [{"role": "user", "content": "t", "seq": 2}]
    new_msg = {"role": "user", "content": "q", "seq": 3}

    async def _always_413(**kwargs):
        from reyn.services.compaction.engine import ContextOverflowError
        raise ContextOverflowError("simulated 413") from _FakeStatusError(
            "Request Entity Too Large", status_code=413,
        )

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    try:
        asyncio.run(retry_loop(
            SP="sp", head=head, raw_middle=[], tail=tail,
            new_msg=new_msg, cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner, main_call=_always_413,
        ))
        raise AssertionError("expected UnrecoveredError, none raised")
    except UnrecoveredError as exc:
        last_rejected = [
            e.data["wire_bytes"] for e in seen
            if e.type == "compaction_wire_bytes_measured" and e.data.get("accepted") is False
        ][-1]
        assert str(last_rejected) in str(exc), (
            f"terminal message should name the last observed rejected byte "
            f"size {last_rejected}; got: {exc}"
        )
        assert "accepted" in str(exc).lower() and (
            "no smaller" in str(exc).lower() or "not accepted" in str(exc).lower()
            or "no accepted" in str(exc).lower()
        )


def test_compaction_wire_bytes_measured_carries_only_byte_counts_never_content() -> None:
    """Tier 2: #5316 (architect co-vet non-blocking note) — every value in a
    real ``compaction_wire_bytes_measured`` event is an ``int`` or ``bool``,
    never a string (which could carry real payload content). This is the
    company-environment guarantee ``EVENT_AUDIT_REQUIREMENTS`` itself
    cannot express — that dict only declares which fields are REQUIRED, not
    a type/no-content constraint on their values, so a future field like a
    ``head_preview`` string could be added there without this gate ever
    turning red. This test is that gate."""
    cfg = CompactionConfig(
        component_weights={
            "head": 10, "body": 5, "tail": 15, "new_msg": 10, "compaction_batch": 60,
        },
        section_weights={
            "topic_arc": 5, "decisions": 40, "pending": 25,
            "session_user_facts": 10, "artifacts_referenced": 35,
        },
        section_caps_spec_tokens=100,
        use_chars4_estimate=True,
    )
    engine = _MinimalCompactionEngine()
    learner = TokenMultiplierLearner(storage_path=Path(tempfile.mkdtemp()) / "m.json")

    async def _success_call(**kwargs):
        from types import SimpleNamespace
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=800), choices=[])

    seen: list = []
    engine._events.add_subscriber(lambda e: seen.append(e))

    asyncio.run(retry_loop(
        SP="system prompt with secrets", head=[{"role": "user", "content": "sensitive", "seq": 1}],
        raw_middle=[], tail=[{"role": "user", "content": "also sensitive", "seq": 2}],
        new_msg={"role": "user", "content": "and this too", "seq": 3},
        cfg=cfg, model="test-model", engine=engine,  # type: ignore[arg-type]
        learner=learner, main_call=_success_call,
    ))

    (event,) = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    # #4496 PR-1's own framework-stamped keys — always present, legitimately
    # non-numeric (a hash / a counter used as an id), NOT something this
    # kind's own emit-site code controls. Everything else on this event
    # must be int/bool — checked WITHOUT a fixed whitelist of "the fields
    # I expect", so a future field this emit site adds (the exact scenario
    # architect's co-vet warned about, e.g. a "head_preview" string) is
    # caught even though this test was never updated to name it.
    _FRAMEWORK_KEYS = {"emitter", "audit_seq", "agent_id", "run_id"}
    non_numeric = {
        k: v for k, v in event.data.items()
        if k not in _FRAMEWORK_KEYS and not isinstance(v, (int, bool))
    }
    assert non_numeric == {}, (
        f"every compaction_wire_bytes_measured field this emit site controls "
        f"must be int/bool only — got non-numeric values: {non_numeric!r}"
    )
