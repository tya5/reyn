"""run_skill kind handler — invoke a sub-skill in-process."""
from __future__ import annotations
from pathlib import Path
from typing import Literal

from . import register
from .context import OpContext
from ..models import RunSkillIROp


async def handle(op: RunSkillIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    from ..compiler import load_dsl_skill
    from ..sub_skill_runner import invoke_sub_skill
    from ..skill_paths import resolve_skill_path

    # Resolve sub-skill: prefer preloaded preprocessor sub-skills (set up at compile time)
    # before falling back to filesystem resolution.
    sub_skill = None
    if ctx.skill is not None:
        sub_skill = ctx.skill.preprocessor_sub_skills.get(op.skill)

    if sub_skill is None:
        skill_ref = op.skill
        if "/" not in skill_ref and not skill_ref.endswith(".md"):
            skill_dir, inferred_root = resolve_skill_path(skill_ref)
            skill_md_path = str(skill_dir / "skill.md")
            dsl_root = str(inferred_root) if inferred_root else None
        else:
            skill_md_path = skill_ref
            dsl_root = None
        sub_skill = load_dsl_skill(skill_md_path, dsl_root=dsl_root)

    model = op.model or ctx.model or "standard"

    # Compute sub-state-dir based on caller context.
    sub_state_dir = ctx.sub_state_dir_override
    if sub_state_dir is None:
        parent_state = ctx.workspace.state_dir
        if ctx.state_dir_strategy == "preprocessor":
            sub_state_dir = str(
                parent_state / "preprocessor"
                / ctx.preprocessor_phase_name
                / f"{ctx.preprocessor_step_index}_{op.skill}"
            )
        else:
            safe_name = op.skill.replace("/", "_").replace(".", "_")
            if op.workspace == "shared":
                sub_state_dir = str(parent_state)
            else:
                sub_state_dir = str(parent_state / "invoke" / safe_name)

    ctx.events.emit("run_skill_started", skill=op.skill, state_dir=sub_state_dir)

    run_result = await invoke_sub_skill(
        sub_skill, op.input,
        model=model,
        subscribers=ctx.subscribers,
        resolver=ctx.resolver,
        output_language=op.output_language or ctx.output_language,
        max_phase_visits=ctx.max_phase_visits,
    )

    sub_state = Path(sub_state_dir)
    parent_state_path = ctx.workspace.state_dir
    try:
        rel = sub_state.relative_to(parent_state_path)
        events_glob = str(rel / "runs" / "*.jsonl")
        artifacts_glob = str(rel / "artifacts" / "**" / "*.json")
    except ValueError:
        events_glob = str(sub_state / "runs" / "*.jsonl")
        artifacts_glob = str(sub_state / "artifacts" / "**" / "*.json")

    usage = run_result.token_usage
    ctx.events.emit(
        "run_skill_completed",
        skill=op.skill,
        status=run_result.status,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
    )

    return {
        "kind": "run_skill",
        "status": run_result.status,
        "skill": op.skill,
        "success": run_result.ok,
        "final_output": run_result.data,
        "phase_artifacts": run_result.phase_artifacts,
        "events_glob": events_glob,
        "artifacts_glob": artifacts_glob,
        "workspace": sub_state_dir,
        "_token_usage": usage,
    }


register("run_skill", handle)
