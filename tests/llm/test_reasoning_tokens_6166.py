"""Tier 2: #6166 — provider-reported reasoning tokens must persist,
never silently coerced to 0 when absent.

#6093's own investigation measured two turns with BYTE-IDENTICAL output
text whose ``completion_tokens`` differed (16 vs 55) — the 39-token gap
had nowhere to be recorded, because ``TokenUsage`` carried no field for
``usage.completion_tokens_details.reasoning_tokens`` at all. This file
pins the fix: extraction (``_extract_usage`` / ``_extract_reasoning_
tokens``), the ``TokenUsage`` field's own "unstated, not zero" contract
(``None`` default, aggregation, round-trip), and the two audit surfaces
that must carry it (``llm_response_received``, ``router_empty_response_
detected``).

Tests use real ``litellm.types.utils.Usage``/``CompletionTokensDetails
Wrapper`` objects (not mocks) — same discipline
``test_cache_token_capture_1772.py`` already established for the
sibling cache-token field, reused here verbatim.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest
from litellm.types.utils import CompletionTokensDetailsWrapper, Usage

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import _extract_reasoning_tokens, _extract_usage, recorded_acompletion
from reyn.llm.pricing import TokenUsage
from tests._support.events import collect_events

# ── extraction: real, not zero, when absent ──────────────────────────────


def test_extract_reasoning_tokens_present() -> None:
    """Tier 2: a thinking-mode model's reasoning_tokens is captured."""
    u = Usage(
        prompt_tokens=3040, completion_tokens=49,
        completion_tokens_details=CompletionTokensDetailsWrapper(reasoning_tokens=31),
    )
    assert _extract_reasoning_tokens(u) == 31


def test_extract_reasoning_tokens_absent_is_none_not_zero() -> None:
    """Tier 2: no completion_tokens_details at all -> None, never 0 (#6166's
    own point: 'the model used 0 reasoning tokens' and 'nobody told us' are
    different claims)."""
    u = Usage(prompt_tokens=100, completion_tokens=5)
    assert _extract_reasoning_tokens(u) is None


def test_extract_reasoning_tokens_details_present_but_field_none() -> None:
    """Tier 2: completion_tokens_details exists (some OTHER sub-field set)
    but reasoning_tokens itself is None -> still None, not 0."""
    u = Usage(
        prompt_tokens=100, completion_tokens=5,
        completion_tokens_details=CompletionTokensDetailsWrapper(audio_tokens=3),
    )
    assert _extract_reasoning_tokens(u) is None


def test_extract_usage_carries_reasoning_tokens() -> None:
    """Tier 2: _extract_usage threads reasoning_tokens onto TokenUsage."""
    u = Usage(
        prompt_tokens=3040, completion_tokens=49,
        completion_tokens_details=CompletionTokensDetailsWrapper(reasoning_tokens=31),
    )
    tu = _extract_usage(SimpleNamespace(usage=u))
    assert tu is not None
    assert tu.reasoning_tokens == 31


def test_extract_usage_reasoning_tokens_absent_is_none() -> None:
    """Tier 2: _extract_usage's own TokenUsage.reasoning_tokens stays None
    when the provider reported nothing -- the exact #6093 gap."""
    u = Usage(prompt_tokens=14228, completion_tokens=16)
    tu = _extract_usage(SimpleNamespace(usage=u))
    assert tu is not None
    assert tu.reasoning_tokens is None


# ── TokenUsage's own contract: round-trip, aggregation ───────────────────


def test_token_usage_roundtrip_preserves_reasoning_tokens() -> None:
    """Tier 2: reasoning_tokens survives to_dict/from_dict, both when set
    and when None (never silently becomes 0)."""
    with_value = TokenUsage(prompt_tokens=3040, completion_tokens=49, reasoning_tokens=31)
    d = with_value.to_dict()
    assert d["reasoning_tokens"] == 31
    assert TokenUsage.from_dict(d) == with_value

    without_value = TokenUsage(prompt_tokens=14228, completion_tokens=16)
    d2 = without_value.to_dict()
    assert d2["reasoning_tokens"] is None
    assert TokenUsage.from_dict(d2).reasoning_tokens is None


def test_from_dict_missing_key_is_none_not_zero() -> None:
    """Tier 2: a PRE-#6166 persisted dict (no reasoning_tokens key at all,
    #6166's own 'do not backfill existing entries' instruction) reads as
    None -- the same answer a dict that explicitly wrote null gets, never
    coerced to 0 the way the OTHER (deliberately zero-defaulting) usage
    fields are in test_cache_token_capture_1772.py's own from_dict test."""
    rt = TokenUsage.from_dict({"prompt_tokens": 14228, "completion_tokens": 16})
    assert rt.reasoning_tokens is None


def test_token_usage_add_none_plus_none_is_none() -> None:
    """Tier 2: neither operand reported anything -> the sum stays None,
    not 0 (an aggregate of two 'unstated's is still unstated)."""
    a = TokenUsage(prompt_tokens=10, completion_tokens=1)
    b = TokenUsage(prompt_tokens=20, completion_tokens=2)
    assert (a + b).reasoning_tokens is None
    a += b
    assert a.reasoning_tokens is None


def test_token_usage_add_none_plus_value_is_none() -> None:
    """Tier 2: #6166 BLOCKING (lead-coder, measured) -- one side unstated,
    the other real -> the sum is None, NOT the stated side's own figure.
    Reporting '31' here would let a consumer read a lower bound ('31, one
    call unaccounted for') as an exact count -- the SAME '0 tokens
    considered' vs 'nobody told us' conflation #6166's own body names for
    the field itself, reproduced inside aggregation if this returned 31
    instead of None."""
    a = TokenUsage(prompt_tokens=10, completion_tokens=1)  # no reasoning report
    b = TokenUsage(prompt_tokens=20, completion_tokens=2, reasoning_tokens=31)
    assert (a + b).reasoning_tokens is None
    assert (b + a).reasoning_tokens is None


def test_token_usage_add_accumulates_reasoning_tokens() -> None:
    """Tier 2: both sides report -> reasoning_tokens sums like every other
    count field."""
    a = TokenUsage(prompt_tokens=10, completion_tokens=1, reasoning_tokens=5)
    b = TokenUsage(prompt_tokens=20, completion_tokens=2, reasoning_tokens=8)
    assert (a + b).reasoning_tokens == 13
    a += b
    assert a.reasoning_tokens == 13


# ── audit-event surfaces ─────────────────────────────────────────────────


@pytest.fixture
def _reset_event_log():
    yield
    set_llm_request_event_log(None)


def test_llm_response_received_carries_reasoning_tokens(monkeypatch, _reset_event_log) -> None:
    """Tier 2: llm_response_received carries reasoning_tokens as a flat
    field when the provider reported it -- the audit surface #6093's own
    investigation needed and did not have. No mocks: real
    recorded_acompletion + a real async fake for litellm.acompletion
    returning a reasoning-bearing litellm Usage + a real EventLog.

    litellm.acompletion (not LLMReplay) is patched here because LLMReplay
    replays a RECORDED usage verbatim -- no existing fixture carries this
    NEW field, so a real one would have to be freshly authored as a
    golden fixture (lead-coder, #6166 review) just to exercise this one
    extraction; this is disclosed in the test itself, not only the PR
    body, since the next reader opens this file, not the PR."""
    async def _resp(**_kwargs):
        return SimpleNamespace(
            usage=Usage(
                prompt_tokens=3040, completion_tokens=49,
                completion_tokens_details=CompletionTokensDetailsWrapper(reasoning_tokens=31),
            ),
            choices=[],
        )

    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "acompletion", _resp)
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    asyncio.run(recorded_acompletion(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        purpose="main",
        model_class=None,
        recorder=None,
        emit_cost_events=True,
    ))

    resp = next(e for e in collected if e.type == "llm_response_received")
    assert resp.data["reasoning_tokens"] == 31


def test_llm_response_received_reasoning_tokens_absent_is_none(monkeypatch, _reset_event_log) -> None:
    """Tier 2: the SAME event's reasoning_tokens is None -- never 0 --
    when the provider reported nothing (a non-thinking model, the
    overwhelmingly common case).

    litellm.acompletion (not LLMReplay) is patched here for the same
    reason as the sibling test above: LLMReplay replays a recorded
    usage verbatim, and no existing fixture carries this field."""
    async def _resp(**_kwargs):
        return SimpleNamespace(
            usage=Usage(prompt_tokens=100, completion_tokens=5),
            choices=[],
        )

    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.setattr(litellm, "acompletion", _resp)
    log = EventLog()
    collected = collect_events(log)
    set_llm_request_event_log(log)

    asyncio.run(recorded_acompletion(
        model="openai/gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        purpose="main",
        model_class=None,
        recorder=None,
        emit_cost_events=True,
    ))

    resp = next(e for e in collected if e.type == "llm_response_received")
    assert resp.data["reasoning_tokens"] is None
