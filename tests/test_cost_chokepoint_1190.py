"""Tier 2: OS invariant — #1190 stage (i) cost-observability chokepoint.

`recorded_acompletion` is the single cost-recording chokepoint: it absorbs proxy
routing + provider-prefix strip + the response_format fallback, performs the
`litellm.acompletion` call, and records usage via `recorder.record_llm(purpose=...)`
by construction when a recorder is given. `record_llm` + the budget ledger gain a
`purpose` cost-attribution bucket. (Stage ii migrates the 5 bypass sites; stage
iii adds the AST guard making bypass impossible.)

Real instances + a hand-written recorder stub + scripted `litellm.acompletion`
(a plain async callable) — no mocks.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import litellm

from reyn.budget.budget import BudgetLedger
from reyn.llm.llm import LLM_PURPOSES, recorded_acompletion
from reyn.llm.pricing import TokenUsage


class _Recorder:
    """Hand-written recorder capturing record_llm calls (not a mock)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_llm(self, **kw: Any) -> None:
        self.calls.append(kw)


def _resp(prompt: int = 10, completion: int = 5, content: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def test_recorded_acompletion_records_with_purpose(monkeypatch) -> None:
    """Tier 2: the chokepoint records usage via recorder.record_llm tagged with
    the call's purpose (by construction when a recorder is given)."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _resp(prompt=30, completion=7)
    monkeypatch.setattr(litellm, "acompletion", _fake)

    rec = _Recorder()
    resp = asyncio.run(recorded_acompletion(
        model="gemini/gemini-2.5-flash-lite", messages=[{"role": "user", "content": "hi"}],
        purpose="compaction", recorder=rec, agent="a1",
    ))
    assert resp.choices[0].message.content == "ok", "returns the RAW litellm response"
    # one record, tagged compaction (behavioral — the recorded purpose sequence).
    assert [c["purpose"] for c in rec.calls] == ["compaction"]
    call = rec.calls[0]
    assert call["agent"] == "a1"
    assert isinstance(call["usage"], TokenUsage) and call["usage"].total_tokens == 37
    assert "compaction" in LLM_PURPOSES


def test_recorded_acompletion_no_record_when_recorder_none(monkeypatch) -> None:
    """Tier 2: recorder=None (e.g. call_llm's own retry-aware record path, or a
    dogfood site) → the call still runs, nothing is recorded here."""
    called = {"n": 0}

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        called["n"] += 1
        return _resp()
    monkeypatch.setattr(litellm, "acompletion", _fake)

    resp = asyncio.run(recorded_acompletion(
        model="m", messages=[{"role": "user", "content": "x"}],
        purpose="dogfood", recorder=None,
    ))
    assert called["n"] == 1 and resp.choices[0].message.content == "ok"


def test_recorded_acompletion_response_format_fallback(monkeypatch) -> None:
    """Tier 2: when response_format is rejected, the chokepoint retries without it
    (and records the successful call once)."""
    calls: list[bool] = []

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        has_rf = "response_format" in kw
        calls.append(has_rf)
        if has_rf:
            raise ValueError("response_format unsupported")
        return _resp()
    monkeypatch.setattr(litellm, "acompletion", _fake)

    rec = _Recorder()
    asyncio.run(recorded_acompletion(
        model="m", messages=[{"role": "user", "content": "x"}], purpose="judge",
        recorder=rec, response_format={"type": "json_object"},
        fallback_without_response_format=True,
    ))
    assert calls == [True, False], "first attempt with rf fails → retry without rf"
    # records once (on the successful call), tagged judge — the failed attempt
    # raised before recording.
    assert [c["purpose"] for c in rec.calls] == ["judge"]


def test_compaction_engine_records_compaction_purpose(monkeypatch) -> None:
    """Tier 2: stage (ii) — a real CompactionEngine threaded with a recorder
    records its compaction LLM call as purpose="compaction" (end-to-end through
    recorded_acompletion), confirming the recorder threading."""
    from reyn.config import CompactionConfig
    from reyn.events.events import EventLog
    from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _resp(content=json.dumps({
            "topic_arc": "arc", "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))
    monkeypatch.setattr(litellm, "acompletion", _fake)

    rec = _Recorder()
    engine = CompactionEngine(
        model="gpt-4o", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True), recorder=rec,
    )
    asyncio.run(engine.compact(HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )))
    assert [c["purpose"] for c in rec.calls] == ["compaction"]


def test_ledger_persists_purpose_and_omits_when_none(tmp_path: Path) -> None:
    """Tier 2: the budget ledger persists `purpose` when given, and omits the
    field entirely when None (pre-#1190 lines stay byte-identical)."""
    ledger = BudgetLedger(tmp_path / "ledger.jsonl")
    ledger.append(agent="a", model="m", tokens=10, cost_usd=0.0, purpose="phase")
    ledger.append(agent="a", model="m", tokens=5, cost_usd=0.0)  # no purpose

    lines = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text().splitlines() if line.strip()]
    assert lines[0]["purpose"] == "phase"
    assert "purpose" not in lines[1], "None purpose must be omitted (legacy byte-identical)"


def test_recorded_acompletion_rejects_unknown_purpose() -> None:
    """Tier 2: stage (iii) Part 3 — an unknown purpose (typo) is rejected at the
    chokepoint before any LLM call, so a mis-tagged call cannot silently land in
    no per-purpose bucket."""
    import pytest

    with pytest.raises(ValueError, match="purpose"):
        asyncio.run(recorded_acompletion(
            model="m", messages=[{"role": "user", "content": "x"}],
            purpose="compcation",  # typo of "compaction"
            recorder=None,
        ))
    # every advertised purpose is accepted (guards against the list drifting out
    # of sync with the assert).
    assert set(LLM_PURPOSES) >= {"main", "phase", "compaction", "judge",
                                 "skill_node_adapt", "dogfood"}


def test_compaction_engine_threads_agent(monkeypatch) -> None:
    """Tier 2: stage (iii) Part 4 — CompactionEngine(recorder_agent=...) attributes
    its compaction spend to that agent (end-to-end agent threading through
    recorded_acompletion → record_llm)."""
    from reyn.config import CompactionConfig
    from reyn.events.events import EventLog
    from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        return _resp(content=json.dumps({
            "topic_arc": "arc", "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))
    monkeypatch.setattr(litellm, "acompletion", _fake)

    rec = _Recorder()
    engine = CompactionEngine(
        model="gpt-4o", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
        recorder=rec, recorder_agent="researcher",
    )
    asyncio.run(engine.compact(HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )))
    assert [c["agent"] for c in rec.calls] == ["researcher"]


def test_budget_per_purpose_breakdown_visible() -> None:
    """Tier 2: stage (iii) Part 2 — the cost-observability payoff: per-purpose
    spend is aggregated and surfaced (snapshot + /budget full rendering), so a
    user can see how much compaction vs main cost."""
    from reyn.budget.budget import BudgetTracker, CostConfig, format_budget_full

    tracker = BudgetTracker(CostConfig())
    tracker.record_llm(model="m", agent="a", usage=TokenUsage(prompt_tokens=100, completion_tokens=20), purpose="main")
    tracker.record_llm(model="m", agent="a", usage=TokenUsage(prompt_tokens=40, completion_tokens=10), purpose="compaction")
    tracker.record_llm(model="m", agent="a", usage=TokenUsage(prompt_tokens=5, completion_tokens=5), purpose="compaction")

    snap = tracker.snapshot()
    # aggregation: two compaction calls summed, main separate.
    assert snap["purpose_tokens"]["main"] == 120
    assert snap["purpose_tokens"]["compaction"] == 60

    rendered = format_budget_full(snap, attached=None)
    assert "By purpose:" in rendered
    assert "compaction" in rendered and "main" in rendered
