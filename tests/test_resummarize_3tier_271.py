"""Tier 2: OS invariant — #271 re-summarize 3-tier topic_arc bounding.

`CompactionEngine.compact` bounds the produced `topic_arc` to `body_budget` via a
3-tier ladder, replacing the lone blind `hard_truncate` char-cut:
  - T1 fit         — within budget → no LLM, unchanged (no `summary_resummarized`).
  - T2 re-summarize — overshoot → ONE LLM re-compression (distinct relaxation
                      prompt; `summary_resummarized` emitted) = judgment-based loss.
  - T3 hard_truncate — deterministic floor, ALWAYS applied last so
                      `topic_arc ≤ body_budget` is never violated (even when T2
                      still overshoots → `body_summary_hard_truncated`).

Drives a REAL CompactionEngine; `litellm.acompletion` is monkeypatched at the
boundary (a real async callable) to script the compaction JSON + the re-summarize
text, distinguished by system prompt. `use_chars4_estimate=True` for deterministic
offline token counts. No collaborator mocks.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import (
    _COMPACTION_SYSTEM_PROMPT,
    _RESUMMARIZE_SYSTEM_PROMPT,
    CompactionEngine,
    HistoryChunkToCompact,
    estimate_tokens,
)

_MODEL = "gpt-4o"


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _engine(passes: int = 1) -> tuple[CompactionEngine, EventLog]:
    events = EventLog()
    cfg = CompactionConfig(use_chars4_estimate=True, resummarize_passes=passes)
    return CompactionEngine(_MODEL, events, cfg), events


def _run(monkeypatch, engine, events, *, compaction_arc: str, resummarize_arc: str):
    """Monkeypatch litellm.acompletion to script both calls; run compact once."""
    calls = {"compaction": 0, "resummarize": 0}

    async def _scripted(**kwargs):
        sys_prompt = kwargs["messages"][0]["content"]
        if sys_prompt == _RESUMMARIZE_SYSTEM_PROMPT:
            calls["resummarize"] += 1
            return _resp(resummarize_arc)
        calls["compaction"] += 1
        return _resp(json.dumps({
            "topic_arc": compaction_arc,
            "new_turn_seqs": [1],
            "decisions": [], "pending": [],
            "session_user_facts": [], "artifacts_referenced": [],
        }))

    monkeypatch.setattr("litellm.acompletion", _scripted)
    chunk = HistoryChunkToCompact(
        previous_summary=None,
        new_turns=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )
    summary = asyncio.run(engine.compact(chunk))
    return summary, calls, [e.type for e in events.all()]


def test_t1_fit_no_resummarize(monkeypatch) -> None:
    """Tier 2: a topic_arc within body_budget is unchanged — no re-summarize, no event."""
    engine, events = _engine()
    bb = engine.budgets.body_budget
    arc = "x " * (bb // 2)  # ~bb/2 tokens (chars//4 of "x " repeats) — fits
    summary, calls, types = _run(monkeypatch, engine, events, compaction_arc=arc, resummarize_arc="short")
    assert calls["resummarize"] == 0, "T1 fit must not invoke the re-summarize LLM"
    assert "summary_resummarized" not in types
    assert estimate_tokens(summary.topic_arc, _MODEL, use_chars4=True) <= bb


def test_t2_resummarize_fits(monkeypatch) -> None:
    """Tier 2: an overshooting topic_arc is re-summarized (event emitted) and fits."""
    engine, events = _engine()
    bb = engine.budgets.body_budget
    big = "word " * (bb * 4)            # ~4·bb tokens — overshoot
    small = "compressed summary " * (bb // 8)  # fits under bb
    summary, calls, types = _run(monkeypatch, engine, events, compaction_arc=big, resummarize_arc=small)
    assert calls["resummarize"] == 1, "T2 must invoke the re-summarize LLM once"
    assert "summary_resummarized" in types
    # T2 result fit → no hard-truncate needed.
    assert "body_summary_hard_truncated" not in types
    assert estimate_tokens(summary.topic_arc, _MODEL, use_chars4=True) <= bb


def test_t3_floor_when_resummarize_still_overshoots(monkeypatch) -> None:
    """Tier 2: if T2 re-summarize still overshoots, T3 hard_truncate floors it.

    The deterministic bound `topic_arc ≤ body_budget` holds even when the LLM
    re-summary itself overshoots — the dead-end-free guarantee is never violated.
    """
    engine, events = _engine()
    bb = engine.budgets.body_budget
    big = "word " * (bb * 4)             # overshoot
    still_big = "still too long " * (bb * 2)  # re-summary ALSO overshoots
    summary, calls, types = _run(monkeypatch, engine, events, compaction_arc=big, resummarize_arc=still_big)
    assert calls["resummarize"] == 1
    assert "summary_resummarized" in types
    assert "body_summary_hard_truncated" in types, "T3 floor must fire when T2 overshoots"
    assert estimate_tokens(summary.topic_arc, _MODEL, use_chars4=True) <= bb, (
        "the topic_arc ≤ body_budget guarantee must hold even when T2 overshoots"
    )


def test_passes_zero_skips_resummarize(monkeypatch) -> None:
    """Tier 2: resummarize_passes=0 skips T2 (straight to the T3 floor = pre-#271)."""
    engine, events = _engine(passes=0)
    bb = engine.budgets.body_budget
    big = "word " * (bb * 4)
    summary, calls, types = _run(monkeypatch, engine, events, compaction_arc=big, resummarize_arc="short")
    assert calls["resummarize"] == 0, "passes=0 must not invoke re-summarize"
    assert "summary_resummarized" not in types
    assert "body_summary_hard_truncated" in types  # T3 floor still bounds it
    assert estimate_tokens(summary.topic_arc, _MODEL, use_chars4=True) <= bb
