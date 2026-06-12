"""Pure-mode resolver — consumes skill_resolve op output (R-PURE-MODE Class D).

Mode: safe. Receives data._skill_resolved_op (= output of the skill_resolve
run_op preprocessor step). Produces the same eight-field dict that the prior
unsafe resolve_paths produced, without any fs I/O.

Output contract (matches analyze_skill_resolver.resolve_paths exactly):
    skill_dir          — absolute path to the skill directory (no trailing slash)
    skill_root         — skill-tree root containing the skill
    target_skill       — the short skill name (no path, no extension)
    skill_dsl_path     — skill_dir + "/skill.md"
    phases_glob        — glob pattern for all phase files
    artifacts_glob     — glob pattern for all artifact yaml files
    existing_eval_path — where to look for an existing eval.md (skill_dir/eval.md)
    eval_output_path   — canonical write destination for eval.md (redirected for stdlib)
"""
from __future__ import annotations

# ONLY allowlisted stdlib imports: no reyn imports, no I/O


def resolve_paths_from_op(artifact: dict) -> dict:
    """Pure dict transform: skill_resolve op output -> resolve_paths shape.

    Reads data._skill_resolved_op (populated by the preceding skill_resolve
    run_op step). Returns the same eight-field dict that analyze_skill_resolver
    .resolve_paths formerly produced via filesystem I/O.

    Runs in safe mode — no reyn imports at module level, no I/O.
    """
    data = artifact.get("data") or {}
    op_result = data.get("_skill_resolved_op") or {}

    if not op_result.get("resolved"):
        skill_name = op_result.get("name") or data.get("_name", {}).get("target_skill", "<unknown>")
        return {
            "skill_dir": None,
            "skill_root": None,
            "target_skill": skill_name,
            "skill_dsl_path": None,
            "phases_glob": None,
            "artifacts_glob": None,
            "existing_eval_path": None,
            "eval_output_path": None,
            "error": f"skill not found: {skill_name!r}",
        }

    skill_dir_str = str(op_result["skill_dir"]).rstrip("/")
    target_skill = op_result["name"]
    source = op_result.get("source")

    # Derive skill_root from source field (= "stdlib" | "local" | "project").
    # skill_resolve returns source but not skill_root directly — derive it.
    # Stdlib: skill_dir ends with /src/reyn/stdlib/skills/<name>; root is the
    # parent three levels up (src/reyn/stdlib/skills/).
    # Local/project: skill_dir ends with reyn/<source>/<name>; root is two
    # levels up (reyn/<source>/).
    skill_root_str = _derive_skill_root(skill_dir_str, target_skill, source)

    eval_output = _derive_eval_output_path(skill_dir_str, target_skill)

    return {
        "skill_dir": skill_dir_str,
        "skill_root": skill_root_str,
        "target_skill": target_skill,
        "skill_dsl_path": skill_dir_str + "/skill.md",
        "phases_glob": skill_dir_str + "/phases/*.md",
        "artifacts_glob": skill_dir_str + "/artifacts/*.yaml",
        "existing_eval_path": ".reyn/evals/" + target_skill + "/eval.md",
        "eval_output_path": eval_output,
    }


def _derive_skill_root(skill_dir_str: str, target_skill: str, source: str | None) -> str:
    """Derive the skill-tree root from skill_dir and source.

    The skill-tree root is the parent directory of the skill's own directory
    (i.e. the directory that contains all skills of that source type).

    For all sources the rule is: strip the trailing /<target_skill> segment
    from skill_dir to get the skill-tree root.
    """
    # skill_dir ends with /<target_skill> by convention for all resolution
    # paths (stdlib, local, project).  Strip just that suffix.
    suffix = "/" + target_skill
    if skill_dir_str.endswith(suffix):
        return skill_dir_str[: -len(suffix)]
    # Fallback: parent of the skill directory (handles unexpected suffixes).
    parts = skill_dir_str.rsplit("/", 1)
    return parts[0] if len(parts) == 2 else skill_dir_str


def _derive_eval_output_path(skill_dir_str: str, target_skill: str) -> str:
    """Derive the eval.md write destination.

    All skills (stdlib, local, project) write to ``.reyn/evals/<name>/eval.md``
    — inside the default write zone.

    Canonical formula: ``.reyn/evals/<name>/eval.md``.
    Mirrors ``skill_paths.eval_md_path_for``; import is not possible from a
    safe-mode module. Consistency is enforced by ``test_eval_md_path_consistency``.
    """
    # canonical: .reyn/evals/<name>/eval.md (in-zone, single location for all skill types)
    return ".reyn/evals/" + target_skill + "/eval.md"
