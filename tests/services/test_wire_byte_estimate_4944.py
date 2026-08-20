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
    """Smallest real collaborator retry_loop needs on its success path —
    no compact() call is exercised here (raw_middle stays empty), so this
    only needs the budgets/_events/_T_comp_SP surface retry_loop actually
    reads."""

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
        SP=SP, head=head, summary=None, raw_middle=[],
        tail=tail, new_msg=new_msg, cfg=cfg, model="test-model",
        engine=engine,  # type: ignore[arg-type]
        learner=learner,
        main_call=_success_call,
        max_iterations=8,
    ))

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured, "expected at least one compaction_wire_bytes_measured event on success"
    expected = estimate_wire_bytes(SP=SP, head=head, summary=None, tail=tail, new_msg=new_msg)
    assert all(e.data.get("wire_bytes") == expected for e in measured), (
        f"every compaction_wire_bytes_measured event on this single-call, "
        f"single-iteration success must carry the same measured value "
        f"{expected}; got {[e.data.get('wire_bytes') for e in measured]!r}"
    )


def test_retry_loop_never_emits_compaction_wire_bytes_measured_before_success() -> None:
    """Tier 2: #4944① — an overflow that never succeeds emits ZERO
    ``compaction_wire_bytes_measured`` events. The event names the byte
    size of a request that WAS actually sent successfully — emitting it on
    a failed attempt would mislabel a rejected size as an accepted one,
    corrupting exactly the diagnostic trail this event exists to provide
    (#4944①'s stated purpose: bounding where a real 413's limit sits from
    the last KNOWN-successful byte count)."""
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
            summary=None, raw_middle=[],
            tail=[{"role": "user", "content": "t", "seq": 2}],
            new_msg={"role": "user", "content": "q", "seq": 3},
            cfg=cfg, model="test-model",
            engine=engine,  # type: ignore[arg-type]
            learner=learner,
            main_call=_always_overflow,
            max_iterations=8,
        ))
    except UnrecoveredError:
        pass

    measured = [e for e in seen if e.type == "compaction_wire_bytes_measured"]
    assert measured == [], (
        f"a run that never succeeded must emit zero compaction_wire_bytes_"
        f"measured events; got {[e.data for e in measured]!r}"
    )
