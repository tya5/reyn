"""Tier 2: #1267 — converged op-loop compaction×resume reuses the recorded summary
(end-to-end, real WAL ``_SummaryMemo``).

Companion to the path-agnostic unit gates (test_compaction_summary_wal_memo_1267):
this drives the REAL WAL-backed summary memo through OSRuntime — run 1 fires C-4a
post-loop compaction (recording the summary as a WAL step); the resume re-runs the
op-loop (op + act-turn memos HIT), re-fires compaction, and the summary **memo-HITS**
(no re-summarize) despite a CHANGING scripted summariser — proving the #1267 fix on
the converged phase variant.

Frozen clock isolates the summary memo from the (separately tested, #1264) datetime
robustness — the act-turn LLM memos HIT trivially, so the op-loop replays and
compaction re-fires, leaving the compaction summary as the variable under test.

Falsification: reverting the summary_memo wiring makes the resume re-summarize (the
changing summariser is called again on run 2).

Real OSRuntime + real CompactionEngine + real WAL; scripted seams = module-level
``call_llm`` / ``call_llm_tools`` (act-turns) + ``litellm.acompletion`` (the summary).
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import litellm

import reyn.core.kernel.llm_call_recorder as lcr
import reyn.schemas.models as _models
from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.core.kernel.runtime import OSRuntime
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.services.compaction.engine import CompactionEngine
from reyn.skill.skill_resume_analyzer import CommittedStep, ResumePlan

_SKILL_NAME = "compaction_summary_resume"


class _FrozenInstant(_dt.datetime):
    def astimezone(self, tz=None):  # noqa: ANN001, ANN201
        return self


_FROZEN = _FrozenInstant(2026, 1, 1, tzinfo=_dt.timezone.utc)


class _FrozenClock(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        return _FROZEN


_FINISH = {
    "type": "finish",
    "control": {
        "type": "finish", "decision": "finish", "next_phase": None,
        "confidence": 1.0, "reason": {"summary": "done"},
    },
    "artifact": {"type": "result", "data": {}},
}


def _skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["write_file"], max_act_turns=6,
    )
    return Skill(
        name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


class _TwoWritesThenStop:
    """turn 1 + 2 emit distinct write_file ops; turn 3 stops. Two accumulated
    control_ir_results (> recent_act_turns_raw=1) → C-4a post-loop compaction fires."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        i = self.calls
        self.calls += 1
        if i < 2:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{
                    "id": f"c{i}", "type": "function",
                    "function": {"name": "write_file",
                                 "arguments": '{"path": ".reyn/c%d.txt", "content": "x"}' % i},
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            )
        return LLMToolCallResult(
            content=None, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
        )


def _finish_llm():
    async def _f(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    return _f


class _SummaryResp:
    def __init__(self, text: str) -> None:
        self.choices = [type("C", (), {"message": type("M", (), {"content": text})(), "finish_reason": "stop"})()]
        self.usage = None


def _committed_steps_from_wal(state_log: StateLog) -> list[CommittedStep]:
    steps: list[CommittedStep] = []
    for e in state_log.iter_from(0):
        if e["kind"] != "step_completed":
            continue
        steps.append(CommittedStep(
            op_invocation_id=e["op_invocation_id"], op_kind=e.get("op_kind", ""),
            phase=e["phase"], args_hash=e.get("args_hash", ""), seq=e.get("seq", 0),
            result=e.get("result"), usage=e.get("usage"),
        ))
    return steps


def _engine_cfg():
    eng = CompactionEngine(
        model="gpt-3.5-turbo", events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
    )
    cfg = PhaseActResultsCompactionConfig(
        use_chars4_estimate=True, recent_act_turns_raw=1, summarize_older_threshold_tokens=1,
    )
    return eng, cfg


def test_converged_compaction_summary_memo_hits_on_resume(tmp_path, monkeypatch) -> None:
    """Tier 2: the converged op-loop's compaction summary memo-HITS on resume — the
    summariser is NOT re-called despite a changing scripted value (#1267 end-to-end)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_models, "datetime", _FrozenClock)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    summary_calls = {"n": 0}

    async def _changing_summary(model, messages, **kw):  # noqa: ANN001, ANN003
        summary_calls["n"] += 1
        return _SummaryResp(f"COMPACTED_V{summary_calls['n']}")
    monkeypatch.setattr(litellm, "acompletion", _changing_summary)

    # ── Run 1: 2 ops → C-4a compaction fires → summary recorded (V1) ──────────────
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _TwoWritesThenStop())
    eng, cfg = _engine_cfg()
    sl1 = StateLog(wal)
    rt1 = OSRuntime(
        _skill(), model="stub/model", run_id="compsum",
        tool_calls_op_loop_skills=[_SKILL_NAME], state_log=sl1,
        phase_compaction_engine=eng, phase_compaction_cfg=cfg,
    )
    r1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert r1.ok, f"run 1 must complete; got {r1.status}"
    assert "phase_act_results_compacted" in [e.type for e in rt1.events.all()], (
        "run 1 must fire compaction (>recent_act_turns_raw)"
    )
    # The converged path has TWO compaction surfaces (C-4b in-loop per-turn + C-4a
    # post-loop), so run 1 may summarise more than once. Capture the run-1 total; the
    # gate is that resume adds ZERO new summariser calls (all memo-HIT).
    run1_summaries = summary_calls["n"]
    assert run1_summaries >= 1, f"run 1 must summarise at least once; got {run1_summaries}"

    # ── Run 2: resume — op-loop replays, compaction re-fires, summary MEMO-HITS ────
    committed = _committed_steps_from_wal(sl1)
    plan = ResumePlan(
        run_id="compsum", skill_name=_SKILL_NAME,
        skill_input={"type": "input", "data": {}}, current_phase="draft",
        last_phase_artifact_path=None, awaiting_intervention_id=None,
        committed_steps=committed,
    )
    monkeypatch.setattr(lcr, "call_llm", _finish_llm())
    monkeypatch.setattr(lcr, "call_llm_tools", _TwoWritesThenStop())
    eng2, cfg2 = _engine_cfg()
    sl2 = StateLog(wal)
    rt2 = OSRuntime(
        _skill(), model="stub/model", run_id="compsum",
        tool_calls_op_loop_skills=[_SKILL_NAME], state_log=sl2, resume_plan=plan,
        phase_compaction_engine=eng2, phase_compaction_cfg=cfg2,
    )
    r2 = asyncio.run(rt2.run({"type": "input", "data": {}}))
    assert r2.ok, f"resume must complete; got {r2.status}"

    # THE GATE: resume added ZERO new summariser calls — every compaction summary
    # memo-HIT its recorded value (no re-summarize), despite the changing scripted
    # summariser. Falsification: reverting the summary_memo wiring makes run 2
    # re-summarize (summary_calls["n"] grows past run1_summaries with changed values).
    assert summary_calls["n"] == run1_summaries, (
        "the converged compaction summary must memo-HIT on resume (no re-summarize) — "
        f"resume re-called the summariser: run1={run1_summaries} total={summary_calls['n']}"
    )


class _JsonModeActsThenFinish:
    """json-mode act-loop: 2 act turns (each a write_file op) then a finish decide.
    Two accumulated control_ir_results (> recent_act_turns_raw=1) → _run_act_loop's
    per-turn compaction fires. Fresh per run; deterministic replay."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        i = self.calls
        self.calls += 1
        if i < 2:
            return LLMCallResult(
                data={"type": "act", "ops": [
                    {"kind": "write_file", "path": ".reyn/j%d.txt" % i, "content": "x"},
                ]},
                usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            )
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))


def _jsonmode_skill() -> Skill:
    draft = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["write_file"], max_act_turns=6,
    )
    return Skill(
        name="jsonmode_compaction_resume", entry_phase="draft", phases={"draft": draft},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def test_jsonmode_compaction_summary_memo_hits_on_resume(tmp_path, monkeypatch) -> None:
    """Tier 2: the json-mode _run_act_loop compaction summary memo-HITS on resume —
    the JSON-MODE site's resume wiring threads the summary_memo (verify-each-site, the
    #1248/C-3 site-wiring discipline; the converged mirror is the sibling test)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_models, "datetime", _FrozenClock)
    wal = tmp_path / ".reyn" / "wal.jsonl"

    summary_calls = {"n": 0}

    async def _changing_summary(model, messages, **kw):  # noqa: ANN001, ANN003
        summary_calls["n"] += 1
        return _SummaryResp(f"COMPACTED_V{summary_calls['n']}")
    monkeypatch.setattr(litellm, "acompletion", _changing_summary)

    # ── Run 1: json-mode 2 act-turns → per-turn compaction fires → summary recorded ─
    monkeypatch.setattr(lcr, "call_llm", _JsonModeActsThenFinish())
    eng, cfg = _engine_cfg()
    sl1 = StateLog(wal)
    rt1 = OSRuntime(
        _jsonmode_skill(), model="stub/model", run_id="jsoncompsum",
        state_log=sl1, phase_compaction_engine=eng, phase_compaction_cfg=cfg,
    )  # NOT in tool_calls_op_loop_skills → json-mode path
    r1 = asyncio.run(rt1.run({"type": "input", "data": {}}))
    assert r1.ok, f"run 1 must complete; got {r1.status}"
    assert "phase_act_results_compacted" in [e.type for e in rt1.events.all()], (
        "run 1 (json-mode) must fire per-turn compaction"
    )
    run1_summaries = summary_calls["n"]
    assert run1_summaries >= 1, f"run 1 must summarise at least once; got {run1_summaries}"

    # ── Run 2: resume — act-loop replays, compaction re-fires, summary MEMO-HITS ───
    committed = _committed_steps_from_wal(sl1)
    plan = ResumePlan(
        run_id="jsoncompsum", skill_name="jsonmode_compaction_resume",
        skill_input={"type": "input", "data": {}}, current_phase="draft",
        last_phase_artifact_path=None, awaiting_intervention_id=None,
        committed_steps=committed,
    )
    monkeypatch.setattr(lcr, "call_llm", _JsonModeActsThenFinish())
    eng2, cfg2 = _engine_cfg()
    sl2 = StateLog(wal)
    rt2 = OSRuntime(
        _jsonmode_skill(), model="stub/model", run_id="jsoncompsum",
        state_log=sl2, resume_plan=plan,
        phase_compaction_engine=eng2, phase_compaction_cfg=cfg2,
    )
    r2 = asyncio.run(rt2.run({"type": "input", "data": {}}))
    assert r2.ok, f"resume must complete; got {r2.status}"
    assert summary_calls["n"] == run1_summaries, (
        "the json-mode compaction summary must memo-HIT on resume (no re-summarize) — "
        f"resume re-called the summariser: run1={run1_summaries} total={summary_calls['n']}"
    )
