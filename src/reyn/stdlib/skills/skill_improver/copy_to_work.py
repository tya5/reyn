"""Deterministic helper functions for the copy_to_work preprocessor phase.

Pure-mode Python preprocessor functions — run sandboxed via reyn._python_harness.
No file I/O: all path computation is string manipulation only.
File I/O (glob, read, write) is delegated to run_op steps in the preprocessor chain.
"""


def compute_paths(artifact: dict) -> dict:
    """Step 1: compute all derived paths from original_dsl_root.

    Receives the improvement_session artifact.
    Returns: {skill_glob, phases_glob, work_dir, original_dsl_root, skill_slug}
    """
    data = artifact.get("data", {})
    original_dsl_root = str(data.get("original_dsl_root", "")).rstrip("/")
    # last path component
    skill_slug = original_dsl_root.rsplit("/", 1)[-1] if "/" in original_dsl_root else original_dsl_root
    work_dir = ".reyn/skill_improver_work/" + skill_slug
    return {
        "skill_glob": original_dsl_root + "/skill.md",
        "phases_glob": original_dsl_root + "/phases/*.md",
        "work_dir": work_dir,
        "original_dsl_root": original_dsl_root,
        "skill_slug": skill_slug,
    }


def build_copy_plan(artifact: dict) -> list:
    """Step 4: combine glob results into a list of {src, rel} pairs.

    Reads:
      data._prep.original_dsl_root
      data._glob_skill.matches  — from run_op glob result
      data._glob_phases.matches — from run_op glob result

    Returns: [{src, rel}] where rel is the path relative to original_dsl_root.
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    original_dsl_root = str(prep.get("original_dsl_root", "")).rstrip("/")
    prefix = original_dsl_root + "/"

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


def build_write_ops(artifact: dict) -> list:
    """Step 6: pair read results with destination paths.

    Reads:
      data._reads  — list of run_op file/read results [{path, content, status, ...}]
      data._prep.work_dir
      data._prep.original_dsl_root

    Returns: [{dst, content}] — one entry per successfully-read file.
    """
    data = artifact.get("data", {})
    prep = data.get("_prep", {})
    work_dir = str(prep.get("work_dir", "")).rstrip("/")
    original_dsl_root = str(prep.get("original_dsl_root", "")).rstrip("/")
    prefix = original_dsl_root + "/"

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


def validate_copy(artifact: dict) -> dict:
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
