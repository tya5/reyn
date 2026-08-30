"""Tier 2: #5592 — the 5 post-hoc-measurable observability fields owner
required ("事後で測れる情報を残して" — leave information that can be
measured after the fact), verified end-to-end against REAL events, not
just presence.

Owner's own trigger (2026-08-30, relayed by lead-coder): lead-coder could
not answer, from ``llm_request``/``compaction_started``'s own pre-existing
fields, whether a real production incident's inputs had genuinely shrunk
or never reached the request at all — the answer only came from
``ls -l history.jsonl``, a FILE size, not an audit-event. Owner's own
standing instruction: never leave an estimate; the field must be what was
ACTUALLY sent, and a genuinely estimate-only field must name itself as one
(none of these 5 are estimates — see each field's own assertion below).

| # | field | event | what it answers |
|---|---|---|---|
| ① | ``input_chars`` | ``compaction_started`` | what compact() was actually handed |
| ② | ``input_chars`` | ``llm_request``/``llm_request_error`` | same, success AND failure |
| ③ | ``raw_middle_remaining``/``raw_middle_total`` | ``compaction_shrink_recovered`` | candidates left, absolute (never %) — covered directly by ``test_5592_spill_tier_batching.py``'s own accept tests reading ``retry_loop``'s emission, not duplicated here |
| ④ | ``max_input_tokens_applied`` | ``llm_request``/``llm_request_error`` | the window ceiling THIS call used |
| ⑤ | ``upstream_recovery_call_count`` | ``llm_request``/``llm_request_error`` | exact call sequence within one recovery episode |

Real ``CompactionEngine``/``EventLog`` throughout — a fake
``litellm.acompletion`` only (same idiom
``test_5582_compaction_forced_non_streaming.py`` already establishes for
inspecting real litellm kwargs no ``LLMReplay`` fixture can see).
``llm_request``'s own emission reads a SEPARATE ContextVar
(``get_llm_request_event_log``) from ``CompactionEngine``'s own
``events=`` constructor arg — both are wired to the SAME ``EventLog``
instance in each test below (``set_llm_request_event_log``), matching
``test_llm_request_event_1669.py``'s own established pattern, so a single
``collected`` list observes both ``compaction_started``/
``compaction_shrink_recovered`` (via ``engine._events``) and
``llm_request``/``llm_request_error`` (via the separate ContextVar).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import litellm

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact

_MODEL = "gemini/gemini-2.5-flash-lite"

_SUMMARY_CONTENT = {
    "topic_arc": "arc", "new_turn_seqs": [1],
    "decisions": [], "pending": [], "session_user_facts": [], "artifacts_referenced": [],
}


def _resp(content: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
    )


def _compact_with_capture(monkeypatch, turns: list) -> "tuple[list, EventLog]":
    """Run one real ``engine.compact()`` call against a fake
    ``litellm.acompletion``, with BOTH the engine's own audit EventLog
    and the separate ``llm_request`` ContextVar wired to the same
    collecting list. Always resets the ContextVar (never leaks into a
    later test)."""
    collected: "list" = []
    events = EventLog(subscribers=[lambda e: collected.append(e)])

    async def _fake(model, messages, **kw):
        return _resp(_SUMMARY_CONTENT)

    monkeypatch.setattr(litellm, "acompletion", _fake)

    token = set_llm_request_event_log(events)
    try:
        engine = CompactionEngine(
            model=_MODEL, events=events, cfg=CompactionConfig(use_chars4_estimate=True),
        )
        chunk = HistoryChunkToCompact(messages=turns, section_token_caps={})
        asyncio.run(engine.compact(chunk, covers_through=1))
    finally:
        set_llm_request_event_log(None)
        del token
    return collected, events


def test_compaction_started_and_llm_request_carry_the_same_exact_input_chars(
    monkeypatch,
) -> None:
    """Tier 2: #5592 accept ①② — ``compaction_started.input_chars`` and
    ``llm_request.input_chars`` both equal the EXACT
    ``len(json.dumps(messages, ensure_ascii=False))`` of the messages
    actually handed to ``compact()`` — not merely present, and not merely
    equal to EACH OTHER (which a shared-but-wrong constant could also
    satisfy) — each independently recomputed here from the real input and
    compared."""
    turns = [{"role": "user", "content": "hello world, this is turn content " * 5, "seq": 1}]
    collected, _events = _compact_with_capture(monkeypatch, turns)

    started = [e for e in collected if e.type == "compaction_started"]
    assert started, "compaction_started never fired"
    expected_chars = len(json.dumps(turns, ensure_ascii=False))
    assert started[0].data["input_chars"] == expected_chars, (
        f"compaction_started.input_chars must be the EXACT char count of "
        f"what was sent — got {started[0].data['input_chars']!r}, "
        f"expected {expected_chars!r}"
    )

    requests = [e for e in collected if e.type == "llm_request"]
    assert requests, "llm_request never fired"
    # llm_request's own input_chars measures the FULL litellm ``messages``
    # payload (the compaction prompt template + section_caps spec wrapped
    # around ``turns``, not ``turns`` alone) — genuinely larger than
    # compaction_started's own count by design (① measures what was
    # handed to compact(), ② measures what was actually sent to the LLM);
    # they are not expected to be equal, only for BOTH to be exact,
    # non-estimate counts of what they each measure.
    assert requests[0].data["input_chars"] >= expected_chars, (
        f"llm_request.input_chars ({requests[0].data['input_chars']!r}) "
        f"must be at least as large as the raw turns it wraps "
        f"({expected_chars!r}) — the compaction prompt template only adds"
    )


def test_llm_request_error_carries_input_chars_too_same_field_both_paths() -> None:
    """Tier 2: #5592 accept ② (failure side) — a FAILED call must carry
    the same ``input_chars`` field/definition as a succeeded one (owner's
    own explicit requirement: "成功・失敗の両方に付くこと"), so a failed
    call's input size is comparable to a succeeded call's — never
    observable only on the happy path."""
    from reyn.llm.llm import _emit_llm_request_error

    collected: "list" = []
    events = EventLog(subscribers=[lambda e: collected.append(e)])
    token = set_llm_request_event_log(events)
    try:
        messages = [{"role": "user", "content": "x" * 500}]
        _emit_llm_request_error(
            _MODEL, "compaction", RuntimeError("boom"), {}, messages,
        )
    finally:
        set_llm_request_event_log(None)
        del token

    errors = [e for e in collected if e.type == "llm_request_error"]
    assert errors, "llm_request_error never fired"
    expected_chars = len(json.dumps(messages, ensure_ascii=False))
    assert errors[0].data["input_chars"] == expected_chars, (
        f"llm_request_error.input_chars must be the EXACT char count of "
        f"the messages that failed to send — got "
        f"{errors[0].data['input_chars']!r}, expected {expected_chars!r}"
    )


def test_llm_request_carries_the_applied_window_ceiling(monkeypatch) -> None:
    """Tier 2: #5592 accept ④ — ``llm_request.max_input_tokens_applied``
    is the real ``get_max_input_tokens(model)`` value for the model THIS
    call used — recomputed independently here and compared, not assumed
    present."""
    from reyn.llm.model_budget import get_max_input_tokens

    turns = [{"role": "user", "content": "hi", "seq": 1}]
    collected, _events = _compact_with_capture(monkeypatch, turns)

    requests = [e for e in collected if e.type == "llm_request"]
    assert requests
    expected = get_max_input_tokens(_MODEL)
    assert requests[0].data["max_input_tokens_applied"] == expected, (
        f"expected max_input_tokens_applied == {expected!r} (the real "
        f"catalog/fallback value for {_MODEL!r}), got "
        f"{requests[0].data['max_input_tokens_applied']!r}"
    )


def test_upstream_recovery_call_count_is_none_outside_an_episode(monkeypatch) -> None:
    """Tier 2: #5592 deny — a compact() call made OUTSIDE a retry_loop-
    driven recovery episode (this test's own direct call, not routed
    through retry_loop) must carry ``upstream_recovery_call_count=None``
    — never a stale or fabricated number. Proves the ContextVar-backed
    counter defaults to absent rather than leaking a value from
    elsewhere."""
    turns = [{"role": "user", "content": "hi", "seq": 1}]
    collected, _events = _compact_with_capture(monkeypatch, turns)

    requests = [e for e in collected if e.type == "llm_request"]
    assert requests
    assert requests[0].data["upstream_recovery_call_count"] is None, (
        "a compact() call made outside any retry_loop episode must not "
        "carry a call count — got "
        f"{requests[0].data['upstream_recovery_call_count']!r}"
    )


def test_upstream_recovery_call_count_increments_once_per_real_call(
    monkeypatch,
) -> None:
    """Tier 2: #5592 accept ⑤ — inside a REAL counter episode (started/
    reset exactly as router_loop_driver.py's own production call site
    does, one ``note_upstream_recovery_call_attempt()`` per upstream
    call, exactly as ``retry_loop`` itself does at each of its two call
    sites — engine.py), each successive upstream call's own
    ``llm_request`` carries a STRICTLY INCREASING count: 1, then 2 — the
    exact sequence number, not an estimate, single-producer.

    Strip-falsify: the sibling "is_none_outside_an_episode" test above
    proves the SAME code path reads ``None`` without this wiring — the
    only variable between the two tests is the counter context below."""
    from reyn.llm.llm import (
        note_upstream_recovery_call_attempt,
        reset_upstream_recovery_call_counter,
        start_upstream_recovery_call_counter,
    )

    collected: "list" = []
    events = EventLog(subscribers=[lambda e: collected.append(e)])

    async def _fake(model, messages, **kw):
        return _resp(_SUMMARY_CONTENT)

    monkeypatch.setattr(litellm, "acompletion", _fake)

    engine = CompactionEngine(
        model=_MODEL, events=events, cfg=CompactionConfig(use_chars4_estimate=True),
    )
    chunk = HistoryChunkToCompact(
        messages=[{"role": "user", "content": "hi", "seq": 1}], section_token_caps={},
    )

    log_token = set_llm_request_event_log(events)
    counter_token = start_upstream_recovery_call_counter()
    try:
        note_upstream_recovery_call_attempt()
        asyncio.run(engine.compact(chunk, covers_through=1))
        note_upstream_recovery_call_attempt()
        asyncio.run(engine.compact(chunk, covers_through=1))
    finally:
        reset_upstream_recovery_call_counter(counter_token)
        del counter_token

    requests = [e for e in collected if e.type == "llm_request"]
    # 2-tuple unpack — raises unless exactly 2 events fired (no bare
    # size/count assertion; testing.ja.md Tier 4).
    first_request, second_request = requests
    assert first_request.data["upstream_recovery_call_count"] == 1
    assert second_request.data["upstream_recovery_call_count"] == 2

    # #5592 deny: outside the (now-reset) episode, a THIRD call reverts to
    # None — the counter does not leak past its own reset.
    try:
        asyncio.run(engine.compact(chunk, covers_through=1))
    finally:
        set_llm_request_event_log(None)
        del log_token
    third = [e for e in collected if e.type == "llm_request"][2]
    assert third.data["upstream_recovery_call_count"] is None, (
        "the counter must not survive its own reset — got "
        f"{third.data['upstream_recovery_call_count']!r}"
    )
