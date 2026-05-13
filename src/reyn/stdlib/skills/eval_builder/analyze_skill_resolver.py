"""Unsafe-mode resolver for the analyze_skill preprocessor phase.

resolve_paths runs in unsafe mode because it calls resolve_skill_path,
which does filesystem existence checks (Path.exists()). This cannot run
in the safe-mode AST sandbox (which blocks reyn imports and I/O).

The safe-mode name extraction step (extract_skill_name) and the
inject_resolved_paths helper live in analyze_skill.py and run in safe
mode (no I/O, no reyn imports at module level).

Path resolution contract:
    resolve_paths receives the input artifact after the safe-mode
    extract_skill_name step has already placed the resolved skill name at
    data._name.target_skill. It calls resolve_skill_path to derive the
    filesystem paths. The LLM never constructs path strings; the OS is the
    single source of path truth.

Raises SkillNotFoundError if the resolved skill does not exist on disk.
"""
from reyn.skill.skill_paths import resolve_skill_path


def resolve_paths(artifact: dict) -> dict:
    """Resolve the target skill name (from data._name) to all filesystem paths.

    Runs in unsafe mode (called by the preprocessor engine, not the safe-mode
    sandbox) because resolve_skill_path performs Path.exists() checks.

    Expects data._name.target_skill to be populated by the preceding safe-mode
    extract_skill_name step. All skill-name extraction and regex logic lives in
    analyze_skill.py (safe mode); this function only performs the filesystem
    resolution step.

    Returns a dict with the following keys:
        skill_dir         — absolute-relative path to the skill directory
        skill_root        — skill-tree root containing the skill (e.g. "src/reyn/stdlib")
        target_skill      — the short skill name (no path, no .md)
        skill_dsl_path    — skill_dir + "/skill.md"
        phases_glob       — glob pattern for all phase files
        artifacts_glob    — glob pattern for all artifact yaml files
        existing_eval_path — where to look for an existing eval.md (skill_dir/eval.md)
        eval_output_path  — canonical write destination for eval.md (redirected for stdlib)

    Raises:
        KeyError            if data._name.target_skill is absent (preprocessor misconfigured)
        SkillNotFoundError  if the resolved skill does not exist on disk
    """
    # Read the name extracted by the preceding safe-mode step.
    target_skill = artifact["data"]["_name"]["target_skill"]

    # OS-level path resolution — structural guarantee against hallucinated paths
    skill_dir, skill_root = resolve_skill_path(target_skill)
    skill_dir_str = str(skill_dir).rstrip("/")
    skill_root_str = str(skill_root).rstrip("/")

    # For stdlib skills the skill directory is an absolute path (inside the
    # package tree, outside the workspace write zone). Redirect eval_output_path
    # to reyn/local/<name>/eval.md so write_eval can write there.
    existing_eval = skill_dir_str + "/eval.md"
    eval_output = _derive_eval_output_path(skill_dir_str, target_skill)

    return {
        "skill_dir": skill_dir_str,
        "skill_root": skill_root_str,
        "target_skill": target_skill,
        "skill_dsl_path": skill_dir_str + "/skill.md",
        "phases_glob": skill_dir_str + "/phases/*.md",
        "artifacts_glob": skill_dir_str + "/artifacts/*.yaml",
        "existing_eval_path": existing_eval,
        "eval_output_path": eval_output,
    }


def _derive_eval_output_path(skill_dir_str: str, target_skill: str) -> str:
    """Derive the eval.md write destination.

    For stdlib skills the skill directory is an absolute path (installed
    inside the package tree), which is outside the workspace write zone.
    Redirect to reyn/local/<name>/eval.md so write_eval can write there.

    For reyn/local/ and reyn/project/ skills the skill directory is a
    CWD-relative path — write alongside skill.md.
    """
    from pathlib import Path as _Path

    p = _Path(skill_dir_str)
    if p.is_absolute():
        # Stdlib skill: absolute path → redirect to workspace-local
        return "reyn/local/" + target_skill + "/eval.md"
    # Local/project skill: relative path → write alongside skill.md
    return skill_dir_str + "/eval.md"
