"""Tier 2: OS invariant — #4883 compaction schema validation.

Before this fix, ``CompactionEngine.compact()`` accepted a syntactically-valid
but content-free LLM response (e.g. ``"{}"``) as a successful compaction: the
missing ``new_turn_seqs`` fell through an existing fallback that claimed the
FULL input range as covered anyway, and the empty ``topic_arc`` silently
overwrote the real turns in history — no error, no re-prompt, and (per the
existing ``_append_history`` call in ``CompactionController._run_compaction``)
no way to recover the original context once that summary landed.

Drives a REAL ``CompactionEngine``; ``litellm.acompletion`` is monkeypatched
at the boundary (a real async callable) to script responses. No collaborator
mocks (``_validate_chat_summary_fields`` / ``_append_schema_reprompt`` /
``_supports_structured_output`` are exercised through the real ``compact()``
call, not unit-tested in isolation, since their only contract is what
``compact()`` does with them).

The controller-level test (``test_raise_does_not_confirm_covered_range``) is
the load-bearing one: validation alone only closes half the defect (#4883,
lead-coder review) — the other half is proving that when ``compact()`` raises
after exhausting the re-prompt budget, the candidate range is NOT confirmed
as covered, so the original turns remain available for a later attempt.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest  # noqa: F401 — used implicitly by pytest discovery

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact
from tests._support.events import collect_events

_MODEL = "openai/gpt-4o"


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _valid_json(seq: int = 1, topic_arc: str = "did a thing") -> str:
    return json.dumps({
        "new_turn_seqs": [seq],
        "topic_arc": topic_arc,
        "decisions": [], "pending": [],
        "session_user_facts": [], "artifacts_referenced": [],
    })


def _engine(**cfg_kwargs) -> "tuple[CompactionEngine, list]":
    events = EventLog()
    collected = collect_events(events)
    cfg = CompactionConfig(use_chars4_estimate=True, **cfg_kwargs)
    return CompactionEngine(_MODEL, events, cfg), collected


def _chunk() -> HistoryChunkToCompact:
    return HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "real content", "seq": 1}],
        section_token_caps={},
    )


# ---------------------------------------------------------------------------
# (a) regression control: a valid first-attempt response is unchanged
# ---------------------------------------------------------------------------


def test_valid_first_response_no_reprompt(monkeypatch) -> None:
    """Tier 2: a valid response on attempt 1 succeeds with no re-prompt and no
    compaction_schema_invalid event (regression control for the bounded loop)."""
    engine, collected = _engine()
    calls = {"n": 0}

    async def _scripted(**kwargs):
        calls["n"] += 1
        return _resp(_valid_json())

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(_chunk()))

    assert calls["n"] == 1
    assert summary.topic_arc == "did a thing"
    assert summary.covers_through_seq == 1
    assert "compaction_schema_invalid" not in [e.type for e in collected]


# ---------------------------------------------------------------------------
# (b) invalid-then-valid: one re-prompt recovers
# ---------------------------------------------------------------------------


def test_invalid_then_valid_recovers_via_reprompt(monkeypatch) -> None:
    """Tier 2: an empty first response followed by a valid re-prompt response
    succeeds, having consumed exactly one re-prompt attempt."""
    engine, collected = _engine(max_schema_reprompt_attempts=1)
    calls = {"n": 0}

    async def _scripted(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp("{}")
        return _resp(_valid_json())

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(_chunk()))

    assert calls["n"] == 2, "must re-prompt exactly once before succeeding"
    assert summary.topic_arc == "did a thing"
    invalid_events = [e for e in collected if e.type == "compaction_schema_invalid"]
    assert invalid_events, "the invalid first attempt must be observable on the audit trail"
    assert invalid_events[-1].data.get("attempt") == 0


# ---------------------------------------------------------------------------
# (c) persistently invalid: exhausts the budget and raises
# ---------------------------------------------------------------------------


def test_persistently_invalid_exhausts_budget_and_raises(monkeypatch) -> None:
    """Tier 2: an empty response on every attempt exhausts
    max_schema_reprompt_attempts and raises ValueError — the defect's old
    silent-success path (empty summary, full range marked covered) must not
    be reachable."""
    engine, collected = _engine(max_schema_reprompt_attempts=1)
    calls = {"n": 0}

    async def _scripted(**kwargs):
        calls["n"] += 1
        return _resp("{}")

    monkeypatch.setattr("litellm.acompletion", _scripted)

    with pytest.raises(ValueError, match="missing required fields"):
        asyncio.run(engine.compact(_chunk()))

    assert calls["n"] == 2, "1 initial + 1 re-prompt attempt, per max_schema_reprompt_attempts=1"
    invalid_events = [e for e in collected if e.type == "compaction_schema_invalid"]
    assert invalid_events, "the exhausted attempts must be observable on the audit trail"
    assert invalid_events[-1].data.get("attempt") == 1, (
        "the LAST attempt (the one right before raising) must itself be reported, "
        "not just the earlier ones — see compaction_schema_invalid's own docstring "
        "in docs/reference/runtime/events.md"
    )
    assert invalid_events[-1].data.get("max_attempts") == 2


# ---------------------------------------------------------------------------
# (d) THE load-bearing test: a raise must not confirm the range as covered
# ---------------------------------------------------------------------------


def _controller_with_real_engine(
    monkeypatch, *, scripted_responses, history: "list[ChatMessage]",
) -> "tuple[CompactionController, list, list[ChatMessage]]":
    calls = {"n": 0}

    async def _scripted(**kwargs):
        idx = min(calls["n"], len(scripted_responses) - 1)
        calls["n"] += 1
        return _resp(scripted_responses[idx])

    monkeypatch.setattr("litellm.acompletion", _scripted)

    events = EventLog()
    collected = collect_events(events)

    def _latest_summary():
        for m in reversed(history):
            if m.role == "summary":
                return m
        return None

    ctrl = CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True, max_schema_reprompt_attempts=0),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=_latest_summary,
        compaction_engine_factory=lambda: CompactionEngine(_MODEL, events, CompactionConfig(
            use_chars4_estimate=True, max_schema_reprompt_attempts=0,
        )),
        history_appender=history.append,
        make_summary_message=lambda rendered, structured, covers: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={"structured": structured, "covers_through_seq": covers},
        ),
        render_summary=lambda s: str(s),
    )
    return ctrl, collected, history


def _history(n: int) -> "list[ChatMessage]":
    # Large enough (count × per-turn size) that, at the real
    # CompactionEngine's token budgets (head_budget/tail_budget computed
    # against the real model catalog, not a synthetic stub), a middle band
    # of candidates survives head/tail trimming — the sibling
    # controller-invariants suite uses a stub engine with tiny synthetic
    # budgets instead; this test needs the REAL engine (to exercise the
    # real compact() loop), so it sizes the history up instead of shrinking
    # budgets down. n=30 turns of ~2000 tokens each leaves seq 1..21 as the
    # middle candidate band at this model's fallback 128K-token budgets
    # (verified directly against ComputedBudgets, not assumed).
    return [
        ChatMessage(
            role="user" if i % 2 == 1 else "assistant", content="x " * 4000, seq=i,
        )
        for i in range(1, n + 1)
    ]


def test_raise_does_not_confirm_covered_range(monkeypatch) -> None:
    """Tier 2: when compact() exhausts its re-prompt budget and raises, driven
    through the REAL CompactionController — not the engine in isolation — the
    candidate range must NOT be confirmed as covered.

    This is the other half of #4883: post-parse validation alone only stops
    an empty summary from being ACCEPTED. Proving the defect is closed also
    needs proving the raise does not commit anything — no summary appended,
    latest_summary() still None, and a second force_compact_now() call still
    sees the SAME candidates (nothing was silently marked "already covered"),
    so the original turns remain available for a later, hopefully successful,
    compaction attempt.
    """
    ctrl, collected, hist = _controller_with_real_engine(
        monkeypatch, scripted_responses=["{}"], history=_history(30),
    )

    asyncio.run(ctrl.force_compact_now())  # must not raise to the caller

    assert [e for e in collected if e.type == "compaction_failed"], (
        "engine exhaustion must surface as compaction_failed, not swallowed silently"
    )
    assert not [m for m in hist if m.role == "summary"], (
        "a raise must not append a summary — the empty/invalid content must never "
        "reach history"
    )
    # latest_summary() (used to derive prev_cover on the NEXT call) must still
    # report nothing — the candidate range was never confirmed as covered.
    latest = None
    for m in reversed(hist):
        if m.role == "summary":
            latest = m
    assert latest is None

    # A second attempt must see the SAME candidates as the first — nothing was
    # silently marked covered by the failed attempt.
    collected.clear()
    ctrl2, collected2, _ = _controller_with_real_engine(
        monkeypatch, scripted_responses=["{}"], history=list(hist),
    )
    asyncio.run(ctrl2.force_compact_now())
    started = [e for e in collected2 if e.type == "compaction_started"]
    assert started, "the retried candidates must still be seen as compactable"
    assert started[0].data.get("covers_through_seq") == 21, (
        "the same original candidate range must still be up for compaction — "
        "the failed attempt must not have narrowed or consumed it"
    )


# ---------------------------------------------------------------------------
# (e) structured-output leg: schema-constrained request when the model supports it
# ---------------------------------------------------------------------------


def test_structured_output_used_when_model_supports_it(monkeypatch) -> None:
    """Tier 2: on a model litellm reports as schema-capable, compact() requests
    response_format={"type": "json_schema", ...} rather than the bare
    json_object fallback."""
    engine, _collected = _engine()
    captured: dict = {}

    async def _scripted(**kwargs):
        captured.update(kwargs)
        return _resp(_valid_json())

    monkeypatch.setattr("litellm.acompletion", _scripted)
    monkeypatch.setattr(
        "reyn.services.compaction.engine._supports_structured_output",
        lambda model: True,
    )
    asyncio.run(engine.compact(_chunk()))

    rf = captured.get("response_format")
    assert rf is not None and rf.get("type") == "json_schema"
    assert rf["json_schema"]["schema"]["required"] == [
        "new_turn_seqs", "topic_arc", "decisions", "pending",
        "session_user_facts", "artifacts_referenced",
    ]


def test_json_object_used_when_model_does_not_support_structured_output(monkeypatch) -> None:
    """Tier 2: on a model litellm reports as NOT schema-capable, compact() goes
    STRAIGHT to the pre-existing json_object request — the degrade decision is
    made before the call, from the precheck alone, not by attempting
    json_schema first (owner ruling: compaction must never raise on an
    unsupported model the way 0062's per-step structured output does, since a
    raise here means the context window never opens back up). No typed error
    escapes — this call succeeds and returns a real summary."""
    engine, _collected = _engine()
    captured: dict = {}

    async def _scripted(**kwargs):
        captured.update(kwargs)
        return _resp(_valid_json())

    monkeypatch.setattr("litellm.acompletion", _scripted)
    monkeypatch.setattr(
        "reyn.services.compaction.engine._supports_structured_output",
        lambda model: False,
    )
    summary = asyncio.run(engine.compact(_chunk()))  # must NOT raise

    assert captured.get("response_format") == {"type": "json_object"}
    assert summary.topic_arc == "did a thing"


def test_provider_rejection_of_json_schema_is_not_caught_and_retried(monkeypatch) -> None:
    """Tier 2: a PROVIDER rejection of response_format at call time (precheck
    said True, the call itself still fails — capability tables can lag
    reality) propagates as a normal call failure. It is NOT caught and
    silently retried without response_format.

    Corrected condition (lead-coder, after reading 0062's own text directly —
    an earlier review round had asked for the opposite, a catch-and-retry via
    ``fallback_without_response_format=True``, which this test would have
    required and which was withdrawn): 0062 §2.1 is explicit that a raw
    provider rejection "can't be reliably told apart from transient/other
    errors" and must not be catch-classified into a decision. The degrade
    decision compaction makes is entirely in choosing json_object UP FRONT
    when the precheck says False (covered by the sibling test above) — never
    by reacting to a failed json_schema attempt. A rejection here is exactly
    like any other `_acompletion` failure: it surfaces to
    CompactionController's existing try/except (-> compaction_failed), the
    same pre-existing safety net the exhausted-reprompt-budget case above
    already leans on.
    """
    engine, _collected = _engine()

    async def _scripted(**kwargs):
        if kwargs.get("response_format", {}).get("type") == "json_schema":
            raise ValueError("provider rejected response_format for this model")
        return _resp(_valid_json())

    monkeypatch.setattr("litellm.acompletion", _scripted)
    monkeypatch.setattr(
        "reyn.services.compaction.engine._supports_structured_output",
        lambda model: True,
    )

    with pytest.raises(ValueError, match="provider rejected response_format"):
        asyncio.run(engine.compact(_chunk()))
