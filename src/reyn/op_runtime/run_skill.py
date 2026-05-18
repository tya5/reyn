"""run_skill kind handler — invoke a sub-skill in-process."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Literal

from reyn.schemas.models import RunSkillIROp

from . import register
from .context import OpContext

_log = logging.getLogger(__name__)


def _resolve_skill_ref(skill_ref: str) -> tuple[str, Path, str | None]:
    """Resolve a sub-skill reference to ``(skill_md_path, path_for_hash, skill_root)``.

    Accepts three reference shapes on the ``op.skill`` field:

    1. **Bare name** — e.g. ``"direct_llm"``. Resolved via
       ``resolve_skill_path`` search order (reyn/local → reyn/project →
       stdlib/skills).
    2. **Short ``<name>/skill.md``** — e.g. ``"direct_llm/skill.md"``.
       B41-NF-S7-1: eval router LLMs construct this shape when describing a
       stdlib skill (= ``target_skill_path`` field name implies a path so the
       LLM appends ``/skill.md`` to the bare name). Without this fallback every
       such invocation hits ``FileNotFoundError`` relative to CWD. Resolved
       by stripping the trailing ``/skill.md`` and applying
       ``resolve_skill_path`` to the leading segment; on resolution miss the
       literal-path interpretation is preserved for pre-B41 callers who
       genuinely pass a 2-segment relative path.
    3. **Multi-segment literal path** — e.g.
       ``"reyn/local/my_app/skill.md"``. Passed through to
       ``load_dsl_skill`` as written.
    """
    from reyn.skill.skill_paths import resolve_skill_path

    parts = skill_ref.split("/")
    if "/" not in skill_ref and not skill_ref.endswith(".md"):
        # form 1
        skill_dir, inferred_root = resolve_skill_path(skill_ref)
        skill_md_path = str(skill_dir / "skill.md")
        path_for_hash = skill_dir / "skill.md"
        skill_root = str(inferred_root) if inferred_root else None
        return skill_md_path, path_for_hash, skill_root
    if len(parts) == 2 and parts[1] == "skill.md" and parts[0]:
        # form 2: ``<name>/skill.md`` — try stdlib resolution by the
        # leading segment first.
        try:
            skill_dir, inferred_root = resolve_skill_path(parts[0])
            skill_md_path = str(skill_dir / "skill.md")
            path_for_hash = skill_dir / "skill.md"
            skill_root = str(inferred_root) if inferred_root else None
            return skill_md_path, path_for_hash, skill_root
        except FileNotFoundError:
            pass  # fall through to form 3 literal-path interpretation
    # form 3
    return skill_ref, Path(skill_ref), None


def _compute_skill_hash(skill_path: Path) -> str:
    """Return the sha256 hex digest of a skill.md file's raw bytes.

    Full 64-character hex is used for collision safety; downstream consumers
    that prefer a shorter prefix can truncate after reading the field.

    Returns "unknown" when the file does not exist (e.g. dynamically-constructed
    skills that have no on-disk skill.md) so the runtime never crashes on a
    missing hash.
    """
    try:
        content = skill_path.read_bytes()
    except (FileNotFoundError, OSError):
        return "unknown"
    return hashlib.sha256(content).hexdigest()


async def handle(op: RunSkillIROp, ctx: OpContext, caller: Literal["preprocessor", "control_ir"]) -> dict:
    from reyn.compiler import load_dsl_skill
    from reyn.skill.sub_skill_runner import invoke_sub_skill

    # Resolve sub-skill: prefer preloaded preprocessor sub-skills (set up at compile time)
    # before falling back to filesystem resolution.
    sub_skill = None
    if ctx.skill is not None:
        sub_skill = ctx.skill.preprocessor_sub_skills.get(op.skill)

    skill_md_path_for_hash: Path | None = None
    if sub_skill is None:
        skill_md_path, skill_md_path_for_hash, skill_root = _resolve_skill_ref(op.skill)
        sub_skill = load_dsl_skill(skill_md_path, skill_root=skill_root)
    else:
        # Pre-loaded preprocessor sub-skill: derive path from the skill spec if
        # available, otherwise the hash falls back to "unknown".
        if hasattr(sub_skill, "source_path") and sub_skill.source_path:
            skill_md_path_for_hash = Path(sub_skill.source_path)

    # Model class resolution: op.model is only honoured when it is a known model
    # class in the resolver mapping (e.g. "light", "standard", "strong").
    # Literal LiteLLM strings (e.g. "gpt-3.5-turbo") injected by the LLM are
    # rejected here and fall back to the runtime model, because the proxy config
    # (reyn.yaml models:) is the single source of truth for model selection.
    # This prevents LLM-hallucinated model strings from bypassing the proxy.
    if op.model and ctx.resolver and not ctx.resolver.is_known_class(op.model):
        _log.warning(
            "run_skill: op.model %r is not a known model class — ignoring and "
            "inheriting runtime model %r instead. Use a model class (light / "
            "standard / strong) defined in reyn.yaml models:.",
            op.model,
            ctx.model,
        )
        model = ctx.model or "standard"
    else:
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

    skill_hash = _compute_skill_hash(skill_md_path_for_hash) if skill_md_path_for_hash else "unknown"
    ctx.events.emit("run_skill_started", skill=op.skill, state_dir=sub_state_dir, skill_version_hash=skill_hash)

    # FP-0016 Component D: per-skill credential scoping. Construct a
    # ScopedSecretStore for the sub-skill based on its required_credentials
    # declaration. If the parent already had a (non-unrestricted) scope,
    # intersect with it — a sub-skill can never have wider access than its
    # parent. Emit a P6 event recording the effective scope.
    from reyn.secrets.store import ScopedSecretStore

    allowed: list[str] = list(sub_skill.required_credentials)  # default ["*"]
    parent = ctx.secret_store
    if parent is not None and not parent.is_unrestricted:
        parent_allowed = parent.allowed_keys
        if "*" in allowed:
            # Sub-skill declared full delegation, but parent is scoped —
            # cap at parent's set.
            allowed = sorted(parent_allowed)
        else:
            # Intersect explicit declarations.
            allowed = [k for k in allowed if k in parent_allowed]

    scoped_store = ScopedSecretStore(allowed_keys=allowed)
    ctx.events.emit(
        "sub_skill_credential_scope",
        skill=op.skill,
        allowed_keys=sorted(set(allowed)) if "*" not in allowed else ["*"],
    )

    run_result = await invoke_sub_skill(
        sub_skill, op.input,
        model=model,
        subscribers=ctx.subscribers,
        resolver=ctx.resolver,
        intervention_bus=ctx.intervention_bus,
        # Propagate the parent's PermissionResolver so the sub-skill's workspace
        # can check approvals recorded in the session.  Without this, the
        # sub-skill's workspace has no resolver and denies all non-CWD reads.
        permission_resolver=ctx.permission_resolver,
        output_language=op.output_language or ctx.output_language,
        max_phase_visits=ctx.max_phase_visits,
        caller=ctx.caller,
        # R-D13: stamp parent_run_id on the child snapshot so
        # /skill list and future cascade-discard can walk the tree.
        parent_run_id=ctx.parent_skill_run_id,
        secret_store=scoped_store,
        # Issue #214: forward plan_step so the sub-skill's EventLog
        # stamps "plan N/M" context into every emit. None = not in a plan.
        plan_step=ctx.plan_step,
    )

    # PR20: per-run events live at
    #   <state_dir>/events/<caller>/skill_runs/**/*.jsonl
    # The recursive glob spans monthly subdirs without enumerating them.
    sub_state = Path(sub_state_dir)
    parent_state_path = ctx.workspace.state_dir
    events_glob = f"events/{ctx.caller}/skill_runs/**/*.jsonl"
    try:
        rel = sub_state.relative_to(parent_state_path)
        artifacts_glob = str(rel / "artifacts" / "**" / "*.json")
    except ValueError:
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
