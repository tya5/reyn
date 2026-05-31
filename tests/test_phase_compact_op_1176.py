"""Tier 2: OS invariant — #1176 B1 phase-axis on-demand voluntary compaction.

The phase axis gains an on-demand counterpart to its automatic act-loop
compaction: a `compact` op reaches `OpContext.compact_now` (wired by the act
loop) which compacts the accumulated `control_ir_results` via the SAME
`compact_control_ir_results` primitive + the SAME older/recent split as the auto
path — so the emitted events are shape-identical (replay-consistent). Verified
with a REAL CompactionEngine + a scripted litellm boundary (no mocks):

  - the compact_now callback frees tokens, replaces the accumulator (holder),
    and returns the chat-byte-identical {freed_tokens, free_window_after/before};
  - it emits the SAME `phase_act_results_compacted` event shape as a direct
    auto-path call (lead-coder's safety requirement);
  - it is a no-op (freed 0, no event) when there is nothing older to compact;
  - the ContextFrame context-size signal is omitted from serialization when None
    (ample window → byte-identical frame) and present when filling.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.events.events import EventLog
from reyn.kernel.phase_executor import (
    _ControlIRResultsHolder,
    _make_phase_compact_now,
)
from reyn.schemas.models import ContextFrame
from reyn.services.compaction.engine import CompactionEngine, compact_control_ir_results


class _LLMSummaryFake:
    """Fake acompletion returning a canned summary (no mock — a real callable)."""

    def __init__(self, summary: str = "PHASE_SUMMARY") -> None:
        self._summary = summary

    async def __call__(self, model: str, messages: list, **kwargs: Any) -> Any:  # noqa: ARG002
        class _Msg:
            content = self._summary

        class _Choice:
            message = _Msg()

        class _Response:
            choices = [_Choice()]

        return _Response()


def _engine(events: EventLog) -> CompactionEngine:
    return CompactionEngine(
        model="gpt-3.5-turbo", events=events,
        cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
    )


def _cfg() -> PhaseActResultsCompactionConfig:
    # threshold trivially exceeded so the older split actually compacts; keep
    # the last 2 results raw (same split the auto path uses).
    return PhaseActResultsCompactionConfig(
        use_chars4_estimate=True, summarize_older_threshold_tokens=10,
        recent_act_turns_raw=2,
    )


def _big_results(n: int) -> list[dict]:
    return [
        {"kind": "grep", "matches": [f"src/f{i}.py:{j}" for j in range(40)]}
        for i in range(n)
    ]


def _run(coro):
    return asyncio.run(coro)


def _types(events: EventLog) -> list[str]:
    return [e.type for e in events.all()]


# ── on-demand compact_now ─────────────────────────────────────────────────────


def test_phase_compact_now_frees_tokens_and_updates_holder(monkeypatch) -> None:
    """Tier 2: compact_now compacts the older split, shrinks the holder, and
    returns exact-token freed/free-window (chat-byte-identical contract)."""
    import litellm

    monkeypatch.setattr(litellm, "acompletion", _LLMSummaryFake("SHORT SUMMARY"))
    events = EventLog()
    engine, cfg = _engine(events), _cfg()
    holder = _ControlIRResultsHolder(_big_results(6))
    before_n = len(holder.get())

    compact_now = _make_phase_compact_now(holder, engine, cfg, events, "p")
    result = _run(compact_now())

    assert set(result) >= {"freed_tokens", "free_window_after", "free_window_before"}
    assert result["freed_tokens"] > 0, "compacting 6 big results must free tokens"
    # holder replaced: older collapsed to one placeholder + the 2 recent raw.
    assert len(holder.get()) < before_n
    assert any(r.get("kind") == "__compacted_phase_results__" for r in holder.get())
    assert "phase_act_results_compacted" in _types(events)


def test_phase_compact_now_event_shape_identical_to_auto(monkeypatch) -> None:
    """Tier 2: safety — the on-demand path emits the SAME phase_act_results_compacted
    event shape as a direct auto-path call (replay-consistency, lead-coder req)."""
    import litellm

    monkeypatch.setattr(litellm, "acompletion", _LLMSummaryFake("S"))
    older = _big_results(4)

    # auto path: call the primitive directly.
    ev_auto = EventLog()
    _run(compact_control_ir_results(
        older, engine=_engine(ev_auto), cfg=_cfg(), events=ev_auto, phase="p",
    ))
    auto_evs = [e for e in ev_auto.all() if e.type == "phase_act_results_compacted"]

    # on-demand path: via the compact_now callback (same older as its full set).
    ev_od = EventLog()
    holder = _ControlIRResultsHolder(older)  # all 4 older when recent split removes 2
    _run(_make_phase_compact_now(holder, _engine(ev_od), _cfg(), ev_od, "p")())
    od_evs = [e for e in ev_od.all() if e.type == "phase_act_results_compacted"]

    assert auto_evs and od_evs, "both paths must emit the compaction event"
    assert set(auto_evs[0].data.keys()) == set(od_evs[0].data.keys()), (
        "on-demand compaction event shape must match the auto path "
        f"(auto={sorted(auto_evs[0].data)}, on-demand={sorted(od_evs[0].data)})"
    )


def test_phase_compact_now_noop_when_nothing_older(monkeypatch) -> None:
    """Tier 2: with results <= recent_act_turns_raw there is nothing older to
    compact — no LLM call, no compaction event, freed_tokens 0."""
    import litellm

    monkeypatch.setattr(litellm, "acompletion", _LLMSummaryFake("S"))
    events = EventLog()
    holder = _ControlIRResultsHolder(_big_results(2))  # == recent_act_turns_raw
    result = _run(_make_phase_compact_now(holder, _engine(events), _cfg(), events, "p")())
    assert result["freed_tokens"] == 0
    assert "phase_act_results_compacted" not in _types(events)
    assert len(holder.get()) == 2, "holder unchanged when nothing older"


def test_holder_get_set_roundtrip() -> None:
    """Tier 2: the holder is a faithful get/set handle (copies, no aliasing)."""
    h = _ControlIRResultsHolder([{"a": 1}])
    assert h.get() == [{"a": 1}]
    h.set([{"b": 2}, {"c": 3}])
    assert h.get() == [{"b": 2}, {"c": 3}]


# ── ContextFrame context-size signal serialization ────────────────────────────


def _frame(**kw) -> ContextFrame:
    base = dict(
        current_phase="p", instructions="do", candidate_outputs=[],
        input_artifact={},
    )
    base.update(kw)
    return ContextFrame(**base)


def test_frame_omits_signal_when_none() -> None:
    """Tier 2: an absent (ample-window) signal is omitted from the serialized
    frame entirely, so the LLM-facing JSON + replay keys stay byte-stable."""
    dumped = _frame().model_dump(mode="json")
    assert "context_size_signal" not in dumped


def test_frame_includes_signal_when_set() -> None:
    """Tier 2: a present (filling-window) signal rides in the serialized frame."""
    dumped = _frame(context_size_signal="## Context window\n  - free ...").model_dump(mode="json")
    assert dumped.get("context_size_signal", "").startswith("## Context window")
