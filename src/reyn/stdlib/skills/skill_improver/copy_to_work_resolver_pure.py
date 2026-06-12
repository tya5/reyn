"""Pure-mode resolver — consumes skill_resolve op output (R-PURE-MODE Class D).

Mode: safe. Receives data._resolved (= output of the skill_resolve run_op
preprocessor step). Produces the same shape that the prior unsafe resolve_paths
produced, without any fs I/O.

Output shape (matches legacy resolve_paths field-for-field):
    {
        "skill_glob":          str,   # "<skill_dir>/skill.md"
        "phases_glob":         str,   # "<skill_dir>/phases/*.md"
        "work_dir":            str,   # ".reyn/skill_improver_work/<slug>"
        "original_skill_root": str,   # == skill_dir (project-relative string)
        "skill_slug":          str,   # short skill name
        "target_skill_path":   str,   # "<skill_dir>/skill.md"
        "target_skill_root":   str,   # == skill_dir
        "eval_spec_path":      str,   # "<skill_dir>/eval.md"
    }

The unsafe `resolve_paths` in copy_to_work_resolver.py is kept as a back-compat
fallback for existing direct callers (= tests). No longer used by the
copy_to_work.md preprocessor as of this refactor.
"""
from __future__ import annotations

# ONLY allowlisted stdlib imports — no fs, no reyn imports beyond inline safe ones


def resolve_paths_from_op(artifact: dict) -> dict:
    """Pure dict transform: skill_resolve op output → resolve_paths shape.

    Receives `data._resolved` populated by a preceding `skill_resolve` run_op
    preprocessor step. Computes the same shape that the legacy resolve_paths
    returned, without re-resolving anything (= no fs walk).

    The `name` field of `data._resolved` is used as the skill_slug — it is the
    short skill name (e.g. "skill_improver") that resolve_skill_path originally
    received.

    Raises KeyError if `data._name.target_skill` is absent AND `data._resolved`
    has no `name`, but the resolved `name` is preferred.
    """
    data = artifact.get("data") or {}
    resolved = data.get("_resolved") or {}

    if not resolved.get("resolved"):
        name = resolved.get("name") or data.get("_name", {}).get("target_skill", "<unknown>")
        return {
            "skill_glob":          None,
            "phases_glob":         None,
            "work_dir":            None,
            "original_skill_root": None,
            "skill_slug":          name,
            "target_skill_path":   None,
            "target_skill_root":   None,
            "eval_spec_path":      None,
            "error": f"skill not found: {name!r}",
        }

    skill_dir = resolved["skill_dir"].rstrip("/")
    skill_slug = resolved["name"]
    work_dir = ".reyn/skill_improver_work/" + skill_slug

    return {
        "skill_glob":          skill_dir + "/skill.md",
        "phases_glob":         skill_dir + "/phases/*.md",
        "work_dir":            work_dir,
        "original_skill_root": skill_dir,
        "skill_slug":          skill_slug,
        "target_skill_path":   skill_dir + "/skill.md",
        "target_skill_root":   skill_dir,
        "eval_spec_path":      ".reyn/evals/" + skill_slug + "/eval.md",
    }
