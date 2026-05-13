"""Safe-mode helper functions for the analyze_skill preprocessor phase.

All functions here run in the safe-mode AST sandbox (no I/O, no reyn imports
at module level). File I/O and OS calls are delegated to unsafe-mode steps or
run_op steps in the preprocessor chain.

The unsafe-mode resolve_paths function (which calls resolve_skill_path) lives
in analyze_skill_resolver.py to keep this file importable in safe mode.

NOTE: Do NOT add 'from __future__ import annotations' and do NOT import any
reyn modules at the top level — the safe-mode AST sandbox blocks both.
"""
import re

# Regex patterns tried in order to extract a skill name from natural language.
# Pattern 1 matches "skill named <name>" (preferred, explicit).
# Pattern 2 is a loose fallback: "for <name>" at word boundary.
_PATTERNS = [
    re.compile(r"skill named\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"for\s+([A-Za-z_][A-Za-z0-9_]*)(?:\s|$|\.)"),
]


def extract_skill_name(artifact):
    """Extract the target skill name from the input artifact (pure dict/regex).

    Runs in safe mode — no I/O, no reyn imports. All operations are pure
    dict access and regex matching over the artifact payload.

    The OS may pass the artifact in two structural shapes depending on whether
    the LLM emitted a structured ``invoke_skill`` input with the ``type`` field:

      A. Top-level form (no ``data`` wrapper) — observed at runtime when
         the LLM emits ``invoke_skill(name=..., input={"target_skill": "..."})``::

            {"target_skill": "direct_llm", "eval_spec": {...}}

      B. Wrapped form (legacy / typed) — when the artifact carries an explicit
         ``type`` and a ``data`` payload::

            {"type": "eval_builder_request", "data": {"target_skill": "direct_llm"}}
            {"type": "unknown",              "data": {"target_skill": "direct_llm"}}

    Priority order:
      1. Top-level ``target_skill`` (form A — actual OS runtime shape)
      2. ``data.target_skill`` (form B — typed eval_builder_request / wrapped legacy)
      3. ``data.text`` regex fallback (user_message free-form input)

    Returns a dict with a single key ``target_skill`` (string). The preprocessor
    engine places this at ``data._name`` for the subsequent unsafe resolve_paths
    step to read.

    Raises ValueError if the skill name cannot be determined or is empty.

    History:
      G17 (B8-NEW-6) initial fix landed at d1f2d30 only checked form B,
      missing the actual OS runtime shape (form A). B9-NEW-2 retest (B9-S5b)
      revealed the wrong-layer trap. This function preserves all three priority
      levels, moved from analyze_skill_resolver.py (formerly _extract_skill_name)
      into safe mode as part of the R-PURE-MODE-REDEFINE Class B refactor.
    """
    # Priority 1: top-level target_skill — the OS runtime shape for
    # invoke_skill(input={"target_skill": "..."}). No data wrapper.
    if "target_skill" in artifact:
        name = str(artifact["target_skill"]).strip()
        if not name:
            raise ValueError(
                "Artifact has empty top-level 'target_skill' field. "
                "Provide a short skill name (e.g. \"direct_llm\")."
            )
        return {"target_skill": name}

    # Priority 2: wrapped form — data.target_skill (typed
    # eval_builder_request, or legacy invocations that nested the input).
    data = artifact.get("data", {})
    if "target_skill" in data:
        name = str(data["target_skill"]).strip()
        if not name:
            raise ValueError(
                "Artifact has empty 'data.target_skill' field. "
                "Provide a short skill name (e.g. \"direct_llm\")."
            )
        return {"target_skill": name}

    # Priority 3: natural-language text fallback (user_message or similar).
    # text may live at the top level or under data depending on how the OS
    # constructed the artifact.
    text = str(artifact.get("text") or data.get("text") or "").strip()
    for pattern in _PATTERNS:
        match = pattern.search(text)
        if match:
            return {"target_skill": match.group(1)}

    raise ValueError(
        f"Cannot extract skill name from user_message text: {text!r}. "
        "Please use the form \"Generate spec for skill named <name>\" or "
        "pass a structured eval_builder_request artifact with target_skill set."
    )


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
