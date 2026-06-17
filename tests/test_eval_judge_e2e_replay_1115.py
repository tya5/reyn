"""Tier 2: OS invariant — eval→judge cross-workspace artifact-handle boundary (e2e).

End-to-end guard for the #1116→#1117 boundary: the `eval` skill resolves a phase
artifact's state_dir handle to an absolute path (via Agent.phase_artifacts) and
hands it to the `judge_phase` sub-skill, which `file.read`s it to judge. #1116
(state_dir-relative handles) silently broke that read across the workspace
boundary; #1117 fixed it but the existing coverage stops at the Workspace
primitive (test_eval_judge_artifact_handle_1115_partb) — the judge SKILL run
reading the handed path had no e2e.

This drives the real `judge_phase` skill via OSRuntime; the judge LLM is scripted
(a real callable, not a mock) so what's verified is the OS orchestration + the
boundary — NOT LLM behavior (→ Tier 2 OS invariant). Per [[non-default handle]]:
the artifact path is a REAL store_artifact handle resolved to absolute (not a
trivial hand-built path), and the assertion is behavioral — the realistic
artifact content crosses the boundary and is read during the judge run. A
trivial path or aliased handle would let the same #1116 silent break slip.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import reyn.kernel.llm_call_recorder as runtime_mod
from reyn.compiler import load_dsl_skill
from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.kernel.runtime import OSRuntime, RunResult
from reyn.llm.llm import LLMCallResult
from reyn.llm.pricing import TokenUsage
from reyn.security.permissions.permissions import PermissionResolver
from reyn.skill.skill_paths import stdlib_root

_MARKER = "MARKER-7f3a-eval-judge-boundary"


class _ScriptedLLM:
    """Replay a fixed list of LLM outputs (a real callable, not a mock)."""

    def __init__(self, script: list[dict]) -> None:
        self._script = script
        self.call_count = 0

    async def __call__(self, model: str, frame: Any, *a: Any, **kw: Any) -> LLMCallResult:
        idx = self.call_count
        self.call_count += 1
        if idx >= len(self._script):
            raise RuntimeError(f"LLM script exhausted (call {idx}/{len(self._script)})")
        return LLMCallResult(data=self._script[idx], usage=TokenUsage(10, 20))


def test_eval_judge_cross_workspace_artifact_handle_e2e(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: judge_phase reads a REAL eval-resolved artifact handle across the
    workspace boundary and produces a verdict — the #1116/#1117 boundary, e2e."""
    base = tmp_path / "repo"
    base.mkdir()
    monkeypatch.chdir(base)

    # Producer stores a realistic, non-default artifact (unique marker) and we
    # resolve the REAL state_dir handle to absolute — exactly what eval's
    # Agent.phase_artifacts hands to judge_phase.
    artifact = {"type": "draft_doc", "data": {"draft_text": f"{_MARKER} — the body", "word_count": 4}}
    producer = Workspace(events=EventLog(), base_dir=base)
    handle = producer.store_artifact("draft", artifact, skill_name="article_writer")
    assert not Path(str(handle)).is_absolute(), (
        "precondition: a real state_dir-relative handle (the #1116 format), not a "
        "trivial absolute path — otherwise the test can't catch the handle-mishandle break"
    )
    abs_path = str(producer.resolve_artifact_handle(handle))

    # Run the real judge_phase skill: act turn reads the handed path, decide turn
    # emits the verdict. The LLM is scripted; the read_file op is the boundary.
    # #1240 Wave 2a: judge_phase migrated allowed_ops file→fine, so the scripted
    # op is the fine read_file kind (was the coarse {kind:file, op:read}).
    judge_skill = load_dsl_skill(stdlib_root() / "skills" / "judge_phase" / "skill.md")
    script = [
        {"type": "act", "ops": [{"kind": "read_file", "path": abs_path}]},
        {
            "type": "decide",
            "control": {
                "type": "finish", "decision": "finish", "next_phase": None,
                "confidence": 1.0, "reason": {"summary": "judged"},
            },
            "artifact": {
                "type": "phase_judgment_raw",
                "data": {
                    "phase_name": "draft", "passed": True,
                    "criteria_results": [{
                        "description": "has a body", "required": True,
                        "met": True, "reason": "the artifact data contains body text",
                    }],
                    "summary": "artifact read and judged",
                },
            },
            "ops": [],
        },
    ]
    monkeypatch.setattr(runtime_mod, "call_llm", _ScriptedLLM(script))

    collected: list[Any] = []
    rt = OSRuntime(
        judge_skill,
        model="stub/model",
        permission_resolver=PermissionResolver({"python.safe": "allow"}, interactive=False),
        subscribers=[lambda e: collected.append(e)],
    )
    result = asyncio.run(rt.run({
        "type": "input",
        "data": {
            "phase_name": "draft",
            "artifact_path": abs_path,
            "criteria": [{"description": "has a body"}],
        },
    }))

    # (1) the judge run finished → a verdict was produced.
    assert isinstance(result, RunResult) and result.ok, (
        f"judge_phase must finish + produce a verdict; got status={result.status}"
    )
    # (2) behavioral boundary proof: the realistic artifact content crossed the
    # workspace handle boundary and was read during the judge run. If the handle
    # were mishandled (the #1116 break), the file.read returns not-found → the
    # marker never appears in the run's event stream.
    assert any(_MARKER in json.dumps(getattr(e, "data", {}), default=str) for e in collected), (
        "the realistic artifact content must have been read by the judge via the "
        "cross-workspace-resolved handle — its absence means the eval→judge "
        "boundary regressed (#1116-class silent break)"
    )
