"""Trusted-mode resolver for the analyze_skill preprocessor phase.

compute_paths runs in trusted mode because it calls resolve_skill_path,
which does filesystem existence checks (Path.exists()). This cannot run
in the pure-mode AST sandbox (which blocks reyn imports and I/O).

The pure-mode inject_resolved_paths helper lives in analyze_skill.py and
runs in pure mode (no I/O, no reyn imports at module level).

Path resolution contract:
    compute_paths receives the input artifact (either eval_builder_request or
    user_message) and extracts the target skill NAME only. It then calls
    resolve_skill_path to derive the filesystem paths. The LLM never
    constructs path strings; the OS is the single source of path truth.

Input forms supported:
    - artifact type=eval_builder_request: data.target_skill directly
    - artifact type=user_message: data.text parsed via regex patterns

Raises ValueError if the skill name cannot be extracted from user_message,
and SkillNotFoundError if the resolved skill does not exist on disk.
"""
import re

from reyn.skill.skill_paths import eval_md_path_for, resolve_skill_path


# Regex patterns tried in order to extract a skill name from natural language.
# Pattern 1 matches "skill named <name>" (preferred, explicit).
# Pattern 2 is a loose fallback: "for <name>" at word boundary.
_PATTERNS = [
    re.compile(r"skill named\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"for\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s|$|\.)"),
]


def _extract_skill_name(artifact: dict) -> str:
    """Extract the target skill name from an artifact dict.

    Supports two input forms:
      - eval_builder_request: reads data.target_skill directly
      - user_message: applies regex patterns to data.text

    Raises ValueError if the skill name cannot be determined.
    """
    artifact_type = artifact.get("type", "")
    data = artifact.get("data", {})

    if artifact_type == "eval_builder_request":
        name = str(data.get("target_skill", "")).strip()
        if not name:
            raise ValueError(
                "eval_builder_request artifact has empty or missing 'target_skill'. "
                "Provide a short skill name (e.g. \"direct_llm\")."
            )
        return name

    # Treat anything else (including user_message) as natural-language text
    text = str(data.get("text", "")).strip()
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)

    raise ValueError(
        f"Cannot extract skill name from user_message text: {text!r}. "
        "Please use the form \"Generate spec for skill named <name>\" or "
        "pass a structured eval_builder_request artifact with target_skill set."
    )


def compute_paths(artifact: dict) -> dict:
    """Resolve the target skill name to all filesystem paths needed by analyze_skill.

    Runs in trusted mode (called by the preprocessor engine, not the pure-mode
    sandbox) because resolve_skill_path performs Path.exists() checks.

    Returns a dict with the following keys:
        skill_dir         — absolute-relative path to the skill directory
        dsl_root          — DSL root containing the skill (e.g. "src/reyn/stdlib")
        target_skill      — the short skill name (no path, no .md)
        skill_dsl_path    — skill_dir + "/skill.md"
        phases_glob       — glob pattern for all phase files
        artifacts_glob    — glob pattern for all artifact yaml files
        existing_eval_path — where to look for an existing eval.md (skill_dir/eval.md)
        eval_output_path  — canonical write destination for eval.md (redirected for stdlib)

    Raises:
        ValueError          if the skill name cannot be extracted from user_message
        SkillNotFoundError  if the resolved skill does not exist on disk
    """
    target_skill = _extract_skill_name(artifact)

    # OS-level path resolution — structural guarantee against hallucinated paths
    skill_dir, dsl_root = resolve_skill_path(target_skill)
    skill_dir_str = str(skill_dir).rstrip("/")
    dsl_root_str = str(dsl_root).rstrip("/")

    # eval_md_path_for returns skill_dir/eval.md via resolve_skill_path;
    # for stdlib skills this is inside src/ (read-only zone) — eval_output_path
    # redirects to reyn/local/<name>/eval.md so write_eval can write there.
    existing_eval = skill_dir_str + "/eval.md"
    eval_output = _derive_eval_output_path(skill_dir_str, target_skill)

    return {
        "skill_dir": skill_dir_str,
        "dsl_root": dsl_root_str,
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
