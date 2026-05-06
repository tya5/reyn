"""Pure-mode helper functions for the analyze_skill preprocessor phase.

All functions here run in the pure-mode AST sandbox (no I/O, no reyn imports
at module level). File I/O and OS calls are delegated to trusted-mode steps or
run_op steps in the preprocessor chain.

The trusted-mode compute_paths function (which calls resolve_skill_path) lives
in analyze_skill_resolver.py to keep this file importable in pure mode.

NOTE: Do NOT add 'from __future__ import annotations' and do NOT import any
reyn modules at the top level — the pure-mode AST sandbox blocks both.
"""


def inject_resolved_paths(artifact):
    """Inject OS-resolved path fields from data._prep into data._resolved.

    After compute_paths populates data._prep, this pure-mode step mirrors the
    path fields into data._resolved so the LLM instructions can reference them
    with a consistent key name, without re-reading from the nested _prep dict.

    Reads:
      data._prep.skill_dir
      data._prep.skill_root
      data._prep.target_skill
      data._prep.skill_dsl_path
      data._prep.phases_glob
      data._prep.artifacts_glob
      data._prep.existing_eval_path
      data._prep.eval_output_path

    Returns a dict with the same keys, promoted one level up (into data._resolved).
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})

    return {
        "skill_dir": str(prep.get("skill_dir", "")),
        "skill_root": str(prep.get("skill_root", "")),
        "target_skill": str(prep.get("target_skill", "")),
        "skill_dsl_path": str(prep.get("skill_dsl_path", "")),
        "phases_glob": str(prep.get("phases_glob", "")),
        "artifacts_glob": str(prep.get("artifacts_glob", "")),
        "existing_eval_path": str(prep.get("existing_eval_path", "")),
        "eval_output_path": str(prep.get("eval_output_path", "")),
    }
