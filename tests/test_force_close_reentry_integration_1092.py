"""Tier 3a: end-to-end force-close re-entry through the real OSRuntime (#1092 PR-D2).

D2 is an ACTIVATION (it REPLACES D1's FD2-from-partial outcome with re-entry), so
it is live-verified end-to-end in its own PR. This is the shared force-close
FIRING infra (a sized fixture that pushes the phase content past the
TurnBudgetEngine threshold for gpt-3.5-turbo) that PR-E reuses/extends for its
by-construction guarantee. Two paths:

1. happy — force-close fires → the orchestrator re-enters the SAME phase → the
   checkpoint reaches the re-entered seed frame → the run converges to a genuine
   finish (the re-entered phase, starting from the small checkpoint + a fresh
   raw-discarded frame, is below threshold → stops → FD2 finishes);
2. pathological — a phase whose work IRREDUCIBLY exceeds the threshold every visit
   re-enters repeatedly → bounded by the EXISTING max_phase_visits loop limit (the
   run terminates with ``loop_limit_exceeded``, not an infinite loop). No new cap.

Real OSRuntime + real control_ir_executor (read_file) + real CompactionEngine /
TurnBudgetEngine; the only scripted seams are call_llm / call_llm_tools and the
compaction acompletion (the convergence-harness pattern).
"""
from __future__ import annotations

import asyncio
import json

import litellm

import reyn.kernel.llm_call_recorder as lcr
from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.events.events import EventLog
from reyn.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.services.compaction.engine import CompactionEngine
from tests.test_routerloop_convergence_compaction_1092 import (
    _FINISH,
    _SKILL_NAME,
    _skill,
)

# ~48K chars > the gpt-3.5-turbo force-close threshold (~9968 tok ≈ 39.9K chars
# at chars4) → one read_file of this fixture pushes the phase content past it.
_BIG = "x" * 48_000


def _read_file_op(call_id: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": call_id, "type": "function",
            "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})},
        }],
        finish_reason="tool_calls",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
    )


def _empty_stop() -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None, tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=0),
    )


def _wrap_up_finish() -> LLMToolCallResult:
    return LLMToolCallResult(
        content="CONSOLIDATED: read big.txt; key facts X, Y; remaining: finish.",
        tool_calls=[], finish_reason="stop",
        usage=TokenUsage(prompt_tokens=10, completion_tokens=8),
    )


class _ForceCloseScript:
    """call_llm_tools script. The wrap-up (force-close) call is identified by
    ``tools == []`` → returns a content-bearing finish (the consolidation). A
    normal call emits a read_file op (fills content past threshold) when
    ``always_read`` OR it is the very first normal call; otherwise empty-stop (so
    the re-entered phase converges)."""

    def __init__(self, *, always_read: bool) -> None:
        self.always_read = always_read
        self.normal_calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        if k.get("tools") == []:
            return _wrap_up_finish()
        self.normal_calls += 1
        if self.always_read or self.normal_calls == 1:
            return _read_file_op(f"c{self.normal_calls}")
        return _empty_stop()


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return type("R", (), {"data": _FINISH, "usage": TokenUsage(prompt_tokens=20, completion_tokens=10)})()
    return _f


class _SummaryResp:
    choices = [type("C", (), {"message": type("M", (), {"content": "S"})(), "finish_reason": "stop"})()]
    usage = None


def _setup(monkeypatch, tmp_path, *, always_read: bool):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text(_BIG)
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _ForceCloseScript(always_read=always_read))

    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        return _SummaryResp()
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    engine = CompactionEngine(
        model="gpt-3.5-turbo", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
    )
    pcfg = PhaseActResultsCompactionConfig(
        use_chars4_estimate=True, recent_act_turns_raw=1,
        summarize_older_threshold_tokens=1,
    )
    return OSRuntime(
        _skill(), model="stub/model", run_id="fc_reentry",
        tool_calls_op_loop_skills=[_SKILL_NAME],
        phase_compaction_engine=engine, phase_compaction_cfg=pcfg,
    )


def test_force_close_fires_reenters_and_converges(tmp_path, monkeypatch) -> None:
    """Tier 3a: a phase whose content exceeds the threshold once force-closes →
    the OS re-enters the SAME phase with the checkpoint → the re-entered phase
    (below threshold) finishes → the run CONVERGES to a genuine finish."""
    rt = _setup(monkeypatch, tmp_path, always_read=False)
    result = asyncio.run(rt.run({"type": "input", "data": {}}))
    types = [e.type for e in rt.events.all()]
    assert "force_close_triggered" in types
    assert "phase_force_close_checkpoint_persisted" in types
    assert "phase_force_close_reentered" in types
    assert result is not None  # converged to a genuine finish (not aborted)


def test_pathological_reentry_bounded_by_max_phase_visits(tmp_path, monkeypatch) -> None:
    """Tier 3a: a phase whose work IRREDUCIBLY exceeds the threshold every visit
    re-enters repeatedly and is bounded by the EXISTING max_phase_visits loop
    limit (no new cap). The run TERMINATES (not infinite) and emits
    ``loop_limit_exceeded`` — confirming the existing bound holds end-to-end across
    force-close re-entries. (PR-E adds the by-construction guarantee that makes
    this coarse visit-cap-abort unreachable in well-configured cases.)"""
    rt = _setup(monkeypatch, tmp_path, always_read=True)
    asyncio.run(rt.run({"type": "input", "data": {}}))  # terminates (bounded), no hang
    types = [e.type for e in rt.events.all()]
    # The existing max_phase_visits loop limit fired = the re-entry is bounded,
    # not infinite.
    assert "loop_limit_exceeded" in types
    # It actually re-entered multiple times before the visit cap stopped it.
    assert types.count("phase_force_close_reentered") >= 2
