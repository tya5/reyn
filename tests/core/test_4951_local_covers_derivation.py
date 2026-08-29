"""Tier 2: OS invariant — #4951-A: ``covers_through_seq`` is derived
UNCONDITIONALLY from ``compact()``'s own input (the ``messages`` reyn
itself built and sent — #5531 renamed this field from ``new_turns``, see
``HistoryChunkToCompact``'s own docstring), never read from the LLM's
``new_turn_seqs`` echo.

Before this fix, a non-empty-but-WRONG echo passed straight through
(``_validate_chat_summary_fields``'s emptiness check could not catch it,
and the derivation only fell back to the local input when the echo was
``0``/empty) — reyn is not the LLM's own untrusted memory. Owner's own
framing (relayed by lead-coder): "compaction は圧縮対象メッセージしか
送らないはずなのでいらないと思うんだけど？" — confirmed: there was no
case where trusting the echo over reyn's own input was more correct, only
cases where a wrong echo silently was.

Drives a REAL ``CompactionEngine``; ``litellm.acompletion`` is
monkeypatched at the boundary (a real async callable) to script
responses — same shape as ``tests/core/test_4883_compaction_schema_validation.py``,
which this file is a sibling of (that file's own ``_engine``/``_chunk``
pattern, re-declared locally rather than imported, since a shared helper
module is out of this PR's scope).
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact
from tests._support.events import collect_events

_MODEL = "openai/gpt-4o"


def _resp(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


def _json(*, new_turn_seqs, topic_arc: str = "did a thing") -> str:
    return json.dumps({
        "new_turn_seqs": new_turn_seqs,
        "topic_arc": topic_arc,
        "decisions": [], "pending": [],
        "session_user_facts": [], "artifacts_referenced": [],
    })


def _engine(**cfg_kwargs) -> "tuple[CompactionEngine, list]":
    events = EventLog()
    collected = collect_events(events)
    cfg = CompactionConfig(use_chars4_estimate=True, **cfg_kwargs)
    return CompactionEngine(_MODEL, events, cfg), collected


def _chunk(seqs: "list[int]") -> HistoryChunkToCompact:
    return HistoryChunkToCompact(
        messages=[
            {"role": "user", "text": f"turn {s}", "seq": s} for s in seqs
        ],
        section_token_caps={},
    )


def test_covers_ignores_a_wrong_nonempty_echo(monkeypatch) -> None:
    """Tier 2: #4951-A — the LLM echoes a seq LOWER than what was actually
    sent (the exact failure mode the old code could not catch: non-empty,
    so the old fallback never fired). ``covers_through_seq`` must still
    equal the real max of the INPUT turns, not the wrong echo."""
    engine, _collected = _engine()
    chunk = _chunk([1, 2, 3])

    async def _scripted(**kwargs):
        # Wrong: claims only seq 1 was covered, though 1..3 were sent.
        return _resp(_json(new_turn_seqs=[1]))

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(chunk, covers_through=max(t["seq"] for t in chunk.messages)))
    assert summary.covers_through_seq == 3, (
        f"expected covers_through_seq derived from the real input max "
        f"(3), not the wrong echo (1); got {summary.covers_through_seq}"
    )


def test_covers_ignores_a_wrong_higher_echo(monkeypatch) -> None:
    """Tier 2: #4951-A — the LLM echoes a seq HIGHER than what was
    actually sent (a hallucinated seq, or one from a prior turn the model
    misremembered). ``covers_through_seq`` must still equal the real input
    max, never the inflated echo — an inflated ``covers`` is #4470's own
    failure shape (claiming coverage of content never actually summarised)."""
    engine, _collected = _engine()
    chunk = _chunk([5, 6])

    async def _scripted(**kwargs):
        return _resp(_json(new_turn_seqs=[999]))

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(chunk, covers_through=max(t["seq"] for t in chunk.messages)))
    assert summary.covers_through_seq == 6, (
        f"expected covers_through_seq derived from the real input max (6), "
        f"not the inflated echo (999); got {summary.covers_through_seq}"
    )


def test_empty_new_turn_seqs_no_longer_reprompts(monkeypatch) -> None:
    """Tier 2: #4951-A — an EMPTY ``new_turn_seqs`` (with a valid
    ``topic_arc``) succeeds on the first attempt with no re-prompt and no
    ``compaction_schema_invalid`` event. Before this fix, an empty echo was
    one of the two load-bearing validation errors (#4883); it no longer is
    — reyn does not read the field at all, so its emptiness cannot be a
    defect. ``topic_arc``'s own emptiness check (#4883's OTHER, still
    load-bearing half) is untouched — this test's ``topic_arc`` is valid,
    isolating the one behavior this PR actually changes."""
    engine, collected = _engine()
    chunk = _chunk([7])
    calls = {"n": 0}

    async def _scripted(**kwargs):
        calls["n"] += 1
        return _resp(_json(new_turn_seqs=[]))

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(chunk, covers_through=max(t["seq"] for t in chunk.messages)))

    assert calls["n"] == 1, "an empty new_turn_seqs must not trigger a re-prompt"
    assert summary.covers_through_seq == 7
    assert "compaction_schema_invalid" not in [e.type for e in collected]


def test_empty_topic_arc_still_reprompts_regardless_of_new_turn_seqs(monkeypatch) -> None:
    """Tier 2: #4951-A does not touch #4883's OTHER validation half —
    ``topic_arc`` emptiness must still be load-bearing even when
    ``new_turn_seqs`` is present and correct, proving the two checks were
    genuinely independent (not silently coupled through a shared branch)."""
    engine, collected = _engine(max_schema_reprompt_attempts=1)
    chunk = _chunk([1])
    calls = {"n": 0}

    async def _scripted(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(_json(new_turn_seqs=[1], topic_arc=""))
        return _resp(_json(new_turn_seqs=[1]))

    monkeypatch.setattr("litellm.acompletion", _scripted)
    summary = asyncio.run(engine.compact(chunk, covers_through=max(t["seq"] for t in chunk.messages)))

    assert calls["n"] == 2, "an empty topic_arc must still trigger exactly one re-prompt"
    assert summary.topic_arc == "did a thing"
    invalid_events = [e for e in collected if e.type == "compaction_schema_invalid"]
    assert invalid_events, "the invalid first attempt must still be observable"
