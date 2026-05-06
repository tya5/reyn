"""Pure-mode helper functions for the copy_to_work preprocessor phase.

All functions here run in the pure-mode AST sandbox (no I/O, no reyn imports).
File I/O (glob, read, write) is delegated to run_op steps in the preprocessor
chain.

The trusted-mode compute_paths function (which calls resolve_skill_path) lives
in copy_to_work_resolver.py to keep this file importable in pure mode.

NOTE: Do NOT add 'from __future__ import annotations' and do NOT import any
reyn modules at the top level — the pure-mode AST sandbox blocks both.
"""


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
