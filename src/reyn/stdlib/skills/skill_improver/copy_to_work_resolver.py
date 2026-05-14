"""Unsafe-mode resolver for the copy_to_work preprocessor phase.

resolve_paths runs in unsafe mode because it calls resolve_skill_path,
which does filesystem existence checks (Path.exists()). This cannot run
in the safe-mode AST sandbox (which blocks reyn imports and I/O).

The safe-mode name extraction step (extract_skill_name) lives in
copy_to_work.py and runs in safe mode (no I/O, no reyn imports at module
level). All other copy_to_work helper functions (build_copy_plan,
build_write_ops, validate_copy, inject_resolved_paths) remain in
copy_to_work.py and run in safe mode.

Path resolution contract (B6-S1-H1 fix):
    resolve_paths receives the input artifact after the safe-mode
    extract_skill_name step has already placed the resolved skill name at
    data._name.target_skill. It calls resolve_skill_path to derive the
    filesystem paths. The LLM never constructs path strings; the OS is the
    single source of path truth.

Raises SkillNotFoundError if target_skill cannot be resolved, preventing
hallucinated skill names from silently producing empty copy plans (B6-S1-H1).
"""
from reyn.skill.skill_paths import resolve_skill_path


def resolve_paths(artifact):
    """Step 2: resolve target_skill (from data._name) -> filesystem paths.

    Runs in unsafe mode (called by the preprocessor engine, not the safe-mode
    sandbox) because resolve_skill_path performs Path.exists() checks.

    Expects data._name.target_skill to be populated by the preceding safe-mode
    extract_skill_name step (in copy_to_work.py). All skill-name extraction
    and dict/regex logic lives in copy_to_work.py (safe mode); this function
    only performs the filesystem resolution step.

    Returns: {skill_glob, phases_glob, work_dir, original_skill_root, skill_slug,
              eval_spec_path, target_skill_path, target_skill_root}

    Raises KeyError if data._name.target_skill is absent (preprocessor misconfigured).
    Raises SkillNotFoundError if target_skill cannot be resolved, preventing
    hallucinated skill names from silently producing empty copy plans (B6-S1-H1).
    """
    # Read the name extracted by the preceding safe-mode step.
    target_skill = artifact["data"]["_name"]["target_skill"]

    # OS-level path resolution — structural guarantee against hallucinated paths
    skill_dir, _skill_root = resolve_skill_path(target_skill)
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
        "original_skill_root": skill_dir_str,
        "skill_slug": skill_slug,
        # Derived paths injected into the session artifact for downstream phases
        "target_skill_path": skill_dir_str + "/skill.md",
        "target_skill_root": skill_dir_str,
        "eval_spec_path": skill_dir_str + "/eval.md",
    }
