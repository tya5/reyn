"""Tier 2: #4703 axis① — the compaction call's own spend reaches
``ChatSummary`` (and, from there, the ``compaction_completed`` event
``CompactionController`` emits — see ``lifecycle_forwarder.py``'s own tests
for the conversation-face marker half).

Owner's own complaint: the ``[↑ N turns compacted]`` marker already exists
on the conversation face; what's missing is that compaction is one of the
highest-cost LLM calls a session makes (it re-sends the whole context) and
that spend was never shown anywhere the user would see it. Measured
(tui-coder, issue #4703): ``kind="system"`` already exists and already
carries this exact marker via ``lifecycle_forwarder.py``'s
``on_compaction_completed`` — no new VOCABULARY kind needed, only real
usage figures on the existing event/marker.

Real ``CompactionEngine`` + a scripted ``litellm.acompletion`` (a plain
async callable, Tier 2c) for the engine-level tests — mirrors
``test_cost_chokepoint_1190.py``'s own collaborator choice. The
controller-level test reuses ``test_compaction_controller_invariants.py``'s
own ``_make_controller``/stub-engine pattern (no LLM needed at that layer —
the engine's own return value is what's under test there).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import litellm

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import ChatSummary, CompactionEngine, HistoryChunkToCompact
from tests._support.events import settle
from tests.runtime.test_compaction_controller_invariants import (
    _STUB_BUDGETS,
    _history,
    _make_controller,
)

_PRICED_MODEL = "gemini/gemini-2.5-flash-lite"


def _resp(prompt: int, completion: int, content: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


_SUMMARY_CONTENT = {
    "topic_arc": "arc", "new_turn_seqs": [1],
    "decisions": [], "pending": [], "session_user_facts": [], "artifacts_referenced": [],
}


def _chunk() -> HistoryChunkToCompact:
    return HistoryChunkToCompact(
        messages=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )


def test_chat_summary_carries_the_calls_own_real_usage(monkeypatch) -> None:
    """Tier 2: the core #4703 axis① contract — compact() returns a
    ChatSummary whose prompt_tokens/completion_tokens are the REAL figures
    off the compaction LLM call's own response, and cost_usd is priced
    against them (not fabricated, not the session's cumulative total)."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _resp(prompt=8200, completion=340, content=_SUMMARY_CONTENT)
    monkeypatch.setattr(litellm, "acompletion", _fake)

    engine = CompactionEngine(
        model=_PRICED_MODEL, events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
    )
    summary = asyncio.run(engine.compact(_chunk(), covers_through=1))
    assert summary.prompt_tokens == 8200
    assert summary.completion_tokens == 340
    assert summary.cost_usd is not None and summary.cost_usd > 0.0


def test_chat_summary_to_dict_never_leaks_usage_into_the_persisted_shape() -> None:
    """Tier 2: usage fields are NOT part of to_dict() — the wire shape
    written to history.jsonl as a role: "summary" entry is unchanged by
    #4703 (this is presentation data, not durable summary content)."""
    summary = ChatSummary(
        topic_arc="arc", covers_through_seq=1,
        prompt_tokens=100, completion_tokens=10, cost_usd=0.01,
    )
    d = summary.to_dict()
    assert "prompt_tokens" not in d
    assert "completion_tokens" not in d
    assert "cost_usd" not in d


def test_usage_is_none_not_zero_when_the_response_carries_none(monkeypatch) -> None:
    """Tier 2: a response with no usage object at all (some providers omit
    it) must NOT be coerced to a fabricated 0 — None, the same real-figure-
    vs-unknown discipline #4691's gutter work already applies."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(_SUMMARY_CONTENT)))],
            usage=None,
        )
    monkeypatch.setattr(litellm, "acompletion", _fake)

    engine = CompactionEngine(
        model=_PRICED_MODEL, events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
    )
    summary = asyncio.run(engine.compact(_chunk(), covers_through=1))
    assert summary.prompt_tokens is None
    assert summary.completion_tokens is None
    assert summary.cost_usd is None


def test_compaction_completed_event_carries_the_usage_end_to_end(monkeypatch) -> None:
    """Tier 2: through the REAL CompactionController AND a REAL
    CompactionEngine (lead-coder's TESTS-READ block on this PR: a hand-made
    ``__init__``-bypassing stub was unnecessary — this file's OWN first
    test already proves a real ``CompactionEngine`` is cheap to construct
    with a scripted ``litellm.acompletion``, the same Tier 2c pattern
    ``test_cost_chokepoint_1190.py`` uses). Only ``_budgets`` is overridden
    post-construction (a plain data attribute, not a faked collaborator —
    ``compact()``/the chokepoint/usage-capture are all the genuine
    methods) so this file's small synthetic history actually yields a
    middle candidate, mirroring ``test_compaction_controller_invariants.py``'s
    own ``_STUB_BUDGETS`` shaping."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _resp(prompt=500, completion=50, content=_SUMMARY_CONTENT)
    monkeypatch.setattr(litellm, "acompletion", _fake)

    def _build_engine(events: EventLog) -> CompactionEngine:
        # #5475: built with the SAME EventLog `_make_controller` gives its
        # controller — `compact()` now emits `compaction_started` through
        # `self._events` itself, so an engine built against a separate,
        # private EventLog (as this test did before #5475) would silently
        # emit into a log nobody observes, exactly the disconnect
        # `test_compaction_controller_invariants.py`'s own stub engines
        # were fixed to avoid.
        engine = CompactionEngine(
            model=_PRICED_MODEL, events=events,
            cfg=CompactionConfig(use_chars4_estimate=True),
        )
        engine._budgets = _STUB_BUDGETS  # noqa: SLF001 — test-setup shaping, not an assertion
        return engine

    ctrl, collected, _, events = _make_controller(history=_history(7), engine_factory=_build_engine)

    async def _run() -> None:
        await ctrl.force_compact_now(spill_fn=lambda _candidates: [])
        await settle(events)

    asyncio.run(_run())

    completed = [e for e in collected if e.type == "compaction_completed"]
    assert completed, "compaction_completed must fire"
    assert completed[0].data["prompt_tokens"] == 500
    assert completed[0].data["completion_tokens"] == 50
    assert completed[0].data["cost_usd"] is not None and completed[0].data["cost_usd"] > 0.0
