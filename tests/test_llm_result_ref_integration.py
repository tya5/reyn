"""Tier 3 (integration): R-D10 wiring — runtime + SkillRegistry + llm_result_ref.

Exercises the full off-load + cleanup loop:
  1. Runtime writes a large LLM ``step_completed`` → WAL stores
     ``{"_ref": ...}`` placeholder, side file appears under
     ``<state_dir>/skills/<run_id>_llm_results/``.
  2. Memo lookup with the same args_hash transparently resolves the
     ref back to the original result.
  3. ``SkillRegistry.complete`` removes the per-run ref directory
     alongside the snapshot file.

Drives the runtime's private wal-emit helper directly (no LLM call) to
keep the test deterministic and fast. The helper is internal, but the
behavior under test is the integration boundary between runtime,
SkillRegistry, and llm_result_ref — testing it through the public LLM
path would require an LLM stub which adds noise without value.

Reference: PR-llm-payload-size (R-D10) in the active plan.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reyn.events.state_log import StateLog
from reyn.kernel.runtime import OSRuntime
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.skill import llm_result_ref
from reyn.skill.skill_registry import SkillRegistry
from reyn.skill.skill_resume_analyzer import (
    CommittedStep,
    ResumePlan,
)

_RUN_ID = "run_payload_int"
_PHASE = "draft"


def _make_skill() -> Skill:
    p = Phase(
        name=_PHASE, instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name="payload_demo", entry_phase=_PHASE,
        phases={_PHASE: p},
        graph=SkillGraph(transitions={}, can_finish_phases=[_PHASE]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _setup(tmp_path: Path) -> tuple[SkillRegistry, StateLog, Path]:
    state_dir = tmp_path / ".reyn" / "agents" / "alpha" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    registry = SkillRegistry(
        agent_name="alpha", agent_state_dir=state_dir, state_log=log,
    )
    return registry, log, state_dir


def test_runtime_offloads_large_llm_result_and_writes_ref_in_wal(tmp_path: Path):
    """Tier 3: runtime writes a large LLM result → WAL has _ref, file on disk."""
    registry, log, state_dir = _setup(tmp_path)
    rt = OSRuntime(
        _make_skill(), model="stub/model", run_id=_RUN_ID,
        skill_registry=registry, state_log=log,
    )
    big_result = {"control": {"type": "finish"}, "data": "x" * 100_000}

    async def go():
        await registry.start(
            run_id=_RUN_ID, skill_name="payload_demo",
            skill_input={"type": "input", "data": {}},
        )
        await rt._wal_step_completed_for_llm(
            phase=_PHASE,
            op_invocation_id=f"{_PHASE}.llm.0",
            args_hash="hash_int",
            result=big_result,
        )

    asyncio.run(go())

    # WAL: result is a {"_ref": ...} placeholder
    completed = [
        e for e in log.iter_from(0) if e["kind"] == "step_completed"
    ]
    assert completed
    wal_result = completed[0]["result"]
    assert isinstance(wal_result, dict)
    assert list(wal_result.keys()) == ["_ref"]
    assert wal_result["_ref"] == "hash_int.json"
    # Side file exists with the full payload
    side_file = (
        llm_result_ref.llm_results_dir(state_dir, _RUN_ID) / "hash_int.json"
    )
    assert side_file.is_file()
    assert json.loads(side_file.read_text(encoding="utf-8")) == big_result


def test_memo_lookup_resolves_ref_transparently(tmp_path: Path):
    """Tier 3: resume memo hit on a ref'd result returns the full payload."""
    registry, log, state_dir = _setup(tmp_path)
    big_result = {"control": {"type": "finish"}, "data": "x" * 100_000}

    # Pre-populate: write the ref file as if a prior run had off-loaded it.
    placeholder = llm_result_ref.write_if_large(
        agent_state_dir=state_dir,
        run_id=_RUN_ID,
        args_hash="hash_resume",
        result=big_result,
    )
    assert "_ref" in placeholder

    # Build a ResumePlan with a CommittedStep whose result is the placeholder.
    plan = ResumePlan(
        run_id=_RUN_ID,
        skill_name="payload_demo",
        skill_input={"type": "input", "data": {}},
        current_phase=_PHASE,
        last_phase_artifact_path=None,
        awaiting_intervention_id=None,
        committed_steps=[
            CommittedStep(
                op_invocation_id=f"{_PHASE}.llm.0",
                op_kind="llm",
                phase=_PHASE,
                args_hash="hash_resume",
                seq=10,
                result=placeholder,
            ),
        ],
    )
    rt = OSRuntime(
        _make_skill(), model="stub/model", run_id=_RUN_ID,
        skill_registry=registry, state_log=log, resume_plan=plan,
    )

    # Drive the extraction directly
    memo = plan.committed_steps[0]
    extracted = rt._extract_memoized_llm_result(
        memo, phase=_PHASE, op_invocation_id=f"{_PHASE}.llm.0",
    )
    assert extracted == big_result


def test_skill_registry_complete_removes_ref_directory(tmp_path: Path):
    """Tier 3: SkillRegistry.complete cleans up the ref dir alongside the snapshot."""
    registry, log, state_dir = _setup(tmp_path)
    big = {"data": "x" * 100_000}
    llm_result_ref.write_if_large(
        agent_state_dir=state_dir, run_id=_RUN_ID,
        args_hash="hash_cleanup", result=big,
    )
    ref_dir = llm_result_ref.llm_results_dir(state_dir, _RUN_ID)
    assert ref_dir.is_dir()

    async def go():
        await registry.start(
            run_id=_RUN_ID, skill_name="payload_demo",
            skill_input={"type": "input", "data": {}},
        )
        await registry.complete(run_id=_RUN_ID)

    asyncio.run(go())
    assert not ref_dir.exists(), (
        "complete() must rmtree the per-run llm_results directory"
    )
