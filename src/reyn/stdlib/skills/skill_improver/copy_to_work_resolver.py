"""Trusted-mode resolver for the copy_to_work preprocessor phase.

compute_paths runs in trusted mode because it calls resolve_skill_path,
which does filesystem existence checks (Path.exists()). This cannot run
in the pure-mode AST sandbox (which blocks reyn imports and I/O).

All other copy_to_work helper functions (build_copy_plan, build_write_ops,
validate_copy, inject_resolved_paths) remain in copy_to_work.py and run
in pure mode (no I/O, no reyn imports at module level).

Path resolution contract (B6-S1-H1 fix):
    compute_paths receives target_skill (a short skill name, e.g. "direct_llm")
    and calls resolve_skill_path to derive the filesystem paths. The LLM never
    constructs path strings; the OS is the single source of path truth.
"""
from reyn.skill.skill_paths import resolve_skill_path


def compute_paths(artifact):
    """Step 1: resolve target_skill -> filesystem paths using the OS resolver.

    Receives the improvement_session artifact whose target_skill field is
    the short skill name (e.g. "direct_llm"). Uses resolve_skill_path
    to find the actual skill directory — the LLM MUST NOT have constructed a
    path string.

    Returns: {skill_glob, phases_glob, work_dir, original_dsl_root, skill_slug,
              eval_spec_path, target_skill_path, target_dsl_root}

    Raises SkillNotFoundError if target_skill cannot be resolved, preventing
    hallucinated skill names from silently producing empty copy plans (B6-S1-H1).
    """
    data = artifact.get("data", {})
    target_skill = str(data.get("target_skill", "")).strip()

    # OS-level path resolution — structural guarantee against hallucinated paths
    skill_dir, _dsl_root = resolve_skill_path(target_skill)
    skill_dir_str = str(skill_dir).rstrip("/")

    skill_slug = target_skill  # slug == skill name (no path component)
    work_dir = ".reyn/skill_improver_work/" + skill_slug

    return {
        # Glob patterns used by subsequent run_op steps
        "skill_glob": skill_dir_str + "/skill.md",
        "phases_glob": skill_dir_str + "/phases/*.md",
        # Work directory for the temp copy
        "work_dir": work_dir,
        # Original skill root (project-relative string for backward compat)
        "original_dsl_root": skill_dir_str,
        "skill_slug": skill_slug,
        # Derived paths injected into the session artifact for downstream phases
        "target_skill_path": skill_dir_str + "/skill.md",
        "target_dsl_root": skill_dir_str,
        "eval_spec_path": skill_dir_str + "/eval.md",
    }
