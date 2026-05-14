"""Pure-mode helper functions for the copy_to_work preprocessor phase.

All functions here run in the pure-mode AST sandbox (no I/O, no reyn imports).
File I/O (glob, read, write) is delegated to run_op steps in the preprocessor
chain.

The unsafe-mode resolve_paths function (which calls resolve_skill_path) lives
in copy_to_work_resolver.py to keep this file importable in safe mode.

NOTE: Do NOT add 'from __future__ import annotations' and do NOT import any
reyn modules at the top level — the pure-mode AST sandbox blocks both.
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

    The artifact is an improvement_session whose data.target_skill is the
    short skill name (e.g. "direct_llm"). The OS may pass the artifact in
    two structural shapes:

      A. Wrapped form (typed improvement_session)::

            {"type": "improvement_session", "data": {"target_skill": "direct_llm"}}

      B. Top-level form — when the LLM emits
         ``invoke_skill(input={"target_skill": "..."})`` without a type wrapper::

            {"target_skill": "direct_llm"}

    Priority order:
      1. Top-level ``target_skill`` (form B — actual OS runtime shape)
      2. ``data.target_skill`` (form A — typed improvement_session)
      3. ``data.text`` regex fallback (user_message free-form input)

    Returns a dict with a single key ``target_skill`` (string). The preprocessor
    engine places this at ``data._name`` for the subsequent unsafe resolve_paths
    step to read.

    Raises ValueError if the skill name cannot be determined or is empty.

    Mirrors the same extraction logic as eval_builder's extract_skill_name
    (analyze_skill.py) as part of the R-PURE-MODE-REDEFINE Class B refactor.
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

    # Priority 2: wrapped form — data.target_skill (typed improvement_session,
    # or legacy invocations that nested the input).
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
        "Please use the form \"Improve skill named <name>\" or "
        "pass a structured improvement_session artifact with target_skill set."
    )


def build_copy_plan(artifact):
    """Step 4: combine glob results into a list of {src, rel} pairs.

    Reads:
      data._prep.original_skill_root
      data._glob_skill.matches  — from run_op glob result
      data._glob_phases.matches — from run_op glob result

    Returns: [{src, rel}] where rel is the path relative to original_skill_root.
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    original_skill_root = str(prep.get("original_skill_root", "")).rstrip("/")
    prefix = original_skill_root + "/"

    glob_skill = data.get("_glob_skill", {})
    glob_phases = data.get("_glob_phases", {})

    skill_matches = glob_skill.get("matches", []) if isinstance(glob_skill, dict) else []
    phases_matches = glob_phases.get("matches", []) if isinstance(glob_phases, dict) else []

    all_paths = list(skill_matches) + list(phases_matches)

    plan = []
    for src in all_paths:
        src_str = str(src)
        # Skip eval.md — the improver should not modify evaluation criteria during its run
        if src_str.endswith("/eval.md") or src_str == "eval.md":
            continue
        rel = src_str[len(prefix):] if src_str.startswith(prefix) else src_str
        plan.append({"src": src_str, "rel": rel})

    return plan


def build_write_ops(artifact):
    """Step 6: pair read results with destination paths.

    Reads:
      data._reads  — list of run_op file/read results [{path, content, status, ...}]
      data._prep.work_dir
      data._prep.original_skill_root

    Returns: [{dst, content}] — one entry per successfully-read file.
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    work_dir = str(prep.get("work_dir", "")).rstrip("/")
    original_skill_root = str(prep.get("original_skill_root", "")).rstrip("/")
    prefix = original_skill_root + "/"

    reads = data.get("_reads", [])
    write_ops = []
    for read in reads:
        if not isinstance(read, dict):
            continue
        if read.get("status") != "ok":
            continue
        src = str(read.get("path", ""))
        content = read.get("content", "") or ""
        rel = src[len(prefix):] if src.startswith(prefix) else src
        dst = work_dir + "/" + rel
        write_ops.append({"dst": dst, "content": content})

    return write_ops


def validate_copy(artifact):
    """Step 8: validate the copy results.

    Reads:
      data._copy_plan  — expected list of {src, rel}
      data._write_results  — list of run_op write results

    Returns: {ok, files_written, files_expected, work_dir}
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    work_dir = str(prep.get("work_dir", ""))
    copy_plan = data.get("_copy_plan", [])
    write_results = data.get("_write_results", [])

    files_expected = len(copy_plan) if isinstance(copy_plan, list) else 0
    files_written = sum(
        1 for r in write_results
        if isinstance(r, dict) and r.get("status") == "ok"
    ) if isinstance(write_results, list) else 0

    ok = files_written == files_expected and files_expected > 0
    return {
        "ok": ok,
        "files_written": files_written,
        "files_expected": files_expected,
        "work_dir": work_dir,
    }


def inject_resolved_paths(artifact):
    """Step 9: inject OS-resolved path fields into the session for downstream phases.

    After the copy, downstream phases (run_and_eval, plan_improvements,
    apply_improvements, finalize) need backward-compat path fields:
    target_skill_path, target_skill_root, eval_spec_path, original_skill_root.

    The work_dir copy already happened; target_skill_root is updated to the work copy.

    Reads:
      data._prep.target_skill_path    (resolved by compute_paths in step 1)
      data._prep.target_skill_root      (original — becomes original_skill_root)
      data._prep.eval_spec_path
      data._prep.work_dir             (becomes the new target_skill_root)
      data._prep.skill_slug

    Returns: {target_skill_path, target_skill_root, eval_spec_path, original_skill_root}
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    work_dir = str(prep.get("work_dir", "")).rstrip("/")
    original_skill_root = str(prep.get("original_skill_root", "")).rstrip("/")
    eval_spec_path = str(prep.get("eval_spec_path", ""))

    # After copy, target_skill_path and target_skill_root point to the work copy
    new_target_skill_root = work_dir
    new_target_skill_path = work_dir + "/skill.md"

    return {
        "target_skill_path": new_target_skill_path,
        "target_skill_root": new_target_skill_root,
        "eval_spec_path": eval_spec_path,
        "original_skill_root": original_skill_root,
    }
