"""Tier 3a: #2396 Step 3 — the converged phase op-loop caps oversized tool results (was uncapped).

Live opt-in consumer (not latent): a skill opted into ``tool_calls_op_loop_skills`` runs the converged
op-loop (RouterLoop.run_loop + PhaseRouterLoopHost). A phase op returning a LARGE result now flows
through the SAME string offloader chat uses — `RouterLoop.feedback` → `host.cap_tool_result` →
`cap_tool_result_content` → `MediaStore.save_tool_result` → a `.reyn/tool-results/` path-ref + a
bounded preview. Before Step 3 the phase host had NO `cap_tool_result` (feedback got None) → the full
result was inlined into the op-loop history. Real OSRuntime + real CompactionEngine + real MediaStore;
scripted LLM seams (Tier 3a: LLM-replay).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import litellm

import reyn.core.kernel.llm_call_recorder as lcr
from reyn.config import CompactionConfig, PhaseActResultsCompactionConfig
from reyn.core.events.events import EventLog
from reyn.core.kernel.runtime import OSRuntime
from reyn.data.workspace.media_store import MediaStore
from reyn.llm.llm import LLMCallResult, LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.services.compaction.engine import CompactionEngine

_SKILL_NAME = "converged_step3_cap"
_BIG = "lorem ipsum dolor sit amet " * 3000  # ~80KB → well over the per-turn cap → offloaded

_FINISH = {
    "type": "finish",
    "control": {"type": "finish", "decision": "finish", "next_phase": None,
                "confidence": 1.0, "reason": {"summary": "done"}},
    "artifact": {"type": "result", "data": {}},
}


def _skill() -> Skill:
    draft = Phase(name="draft", instructions="d",
                  input_schema={"type": "object", "properties": {}},
                  allowed_ops=["read_file"], max_act_turns=4)
    return Skill(name=_SKILL_NAME, entry_phase="draft", phases={"draft": draft},
                 graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
                 final_output_schema={"type": "object", "properties": {}},
                 final_output_name="result")


class _ReadThenStop:
    """Emit one read_file (large file), capturing the tool message the LLM RECEIVES next turn."""

    def __init__(self) -> None:
        self.calls = 0
        self.tool_msg_seen: str | None = None

    async def __call__(self, *a, **k):  # noqa: ANN002, ANN003
        msgs = k.get("messages") or (a[1] if len(a) > 1 else [])
        for m in msgs:  # capture the read_file tool result the LLM sees on turn 2
            if m.get("role") == "tool" and self.tool_msg_seen is None and self.calls > 0:
                self.tool_msg_seen = str(m.get("content"))
        i = self.calls
        self.calls += 1
        if i == 0:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{"id": "c0", "type": "function",
                             "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})}}],
                finish_reason="tool_calls", usage=TokenUsage(prompt_tokens=10, completion_tokens=5),
            )
        return LLMToolCallResult(content="done", tool_calls=[], finish_reason="stop",
                                 usage=TokenUsage(prompt_tokens=10, completion_tokens=5))


def _run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text(_BIG, encoding="utf-8")

    async def _finish(model, frame, *a, **k):  # noqa: ANN001, ANN002, ANN003
        return LLMCallResult(data=_FINISH, usage=TokenUsage(prompt_tokens=20, completion_tokens=10))
    monkeypatch.setattr(lcr, "call_llm", _finish)
    script = _ReadThenStop()
    monkeypatch.setattr(lcr, "call_llm_tools", script)

    rt = OSRuntime(
        _skill(), model="stub/model", run_id="step3cap",
        tool_calls_op_loop_skills=[_SKILL_NAME],
        media_store=MediaStore(project_root=tmp_path),
        phase_compaction_engine=CompactionEngine(
            model="gpt-3.5-turbo", events=EventLog(),
            cfg=CompactionConfig(use_chars4_estimate=True), T_SP=0,
        ),
        phase_compaction_cfg=PhaseActResultsCompactionConfig(
            use_chars4_estimate=True, recent_act_turns_raw=1, summarize_older_threshold_tokens=1,
        ),
    )
    asyncio.run(rt.run({"type": "input", "data": {}}))
    return script


def test_converged_phase_op_result_is_capped_to_tool_results(tmp_path, monkeypatch):
    """Tier 3a: a large phase op result is offloaded to .reyn/tool-results/ (a path-ref) and the LLM
    receives a BOUNDED preview, not the full ~80KB inline. RED before Step 3 (phase host had no
    cap_tool_result → the full result was inlined)."""
    script = _run(tmp_path, monkeypatch)

    # the large phase op result was offloaded to .reyn/tool-results/ (the cap fired via the phase
    # host — before Step 3 there was NO capper on the phase host, so no file was written here).
    files = list((tmp_path / ".reyn" / "tool-results").glob("*"))
    assert files, "the oversized phase op result was offloaded to .reyn/tool-results/"
    body = files[0].read_text(encoding="utf-8")
    assert "lorem ipsum" in body, "the offloaded body holds the large op result content"

    # the LLM's next-turn tool message is a BOUNDED offload preview (a ref), not the full result inline.
    assert script.tool_msg_seen is not None, "the LLM received the read_file tool result"
    preview = json.loads(script.tool_msg_seen)
    assert preview["_offload_ref"].startswith(".reyn/tool-results/"), "the LLM got a path-ref, not inline"
    assert len(script.tool_msg_seen) < len(body), "the inline preview is bounded (smaller than the offloaded body)"

