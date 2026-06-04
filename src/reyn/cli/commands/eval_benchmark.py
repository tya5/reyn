"""`reyn eval benchmark` — generic batch skill runner.

Executes a skill against a JSONL task file concurrently, writing per-instance
results to an output directory.  Designed for large benchmark runs (e.g.
SWE-bench Verified) but intentionally skill-agnostic.

Usage
-----
reyn eval benchmark <skill_name> \\
    --tasks <jsonl-path> \\
    --output <results-dir> \\
    --concurrency <N>           # default 4
    [--limit <N>]               # subset for quick try
    [--resume]                  # continue from prior run

Capability tiers (C7 — honest verification accounting)
-------------------------------------------------------
Tier 1 (docker):         Docker daemon reachable — official SWE-bench image eval (PR3).
                         Delegates authoritative scoring to swebench.harness.run_evaluation.main,
                         which pulls the official pre-built image and runs the test_cmd via Docker.
                         swebench is a Tier1-ONLY lazy optional dependency — NOT imported at
                         module top, NOT part of reyn's core install.
                         Install separately: pip install swebench
Tier 2 (linux_host):     Linux host without Docker — uv-based faithful build (future PR).
Tier 3 (no_faithful_env): macOS/Windows or no Docker — skip verification; never emit
                          a non-faithful PASS/FAIL.

THE INVARIANT: NEVER count a verify_skipped result as pass or fail.
pass_rate is computed over the faithful-verified subset ONLY.  When that
subset is empty, pass_rate is null.

Binary-sharpening (C7): For Tier-1 evaluated results, the authoritative C7
pass/fail is the harness verdict (harness_resolved) from swebench's official
eval — NOT the skill's self-check (tests_passed).  compute_faithful_accounting
uses harness_resolved when present; tests_passed is recorded as informational only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import platform
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ── capability detection (C7 — honest-skip foundation) ───────────────────────

_TIER_DOCKER = "docker"
_TIER_LINUX_HOST = "linux_host"
_TIER_NO_FAITHFUL_ENV = "no_faithful_env"

# Human-readable skip reason injected into every Tier 3 result record.
_NO_FAITHFUL_ENV_REASON = (
    "no faithful verification environment (requires Docker or a Linux host); "
    "SWE-bench specs need the official Linux build toolchain"
)

# Stub reason for Tier 2 (linux_host) — faithful eval not yet implemented.
_TIER2_STUB_REASON = (
    "faithful eval not yet implemented (Tier2 uv-based Linux build — future PR)"
)

# Reason emitted when Tier 1 is selected but swebench is not installed.
_TIER1_SWEBENCH_MISSING_REASON = (
    "Tier1 requires swebench (pip install swebench) — not installed"
)


def classify_verification_tier(
    *,
    docker_available: bool,
    platform_system: str,
) -> str:
    """Pure classifier: return the verification tier string from explicit inputs.

    Tier 1 (docker):         Docker daemon is reachable.
    Tier 2 (linux_host):     Linux host, no Docker.
    Tier 3 (no_faithful_env): macOS / Windows / any other platform without Docker.

    Intentionally pure (no I/O) so it can be unit-tested without any
    subprocess or platform probing.  The probe that gathers the real
    inputs is ``detect_verification_tier()``.
    """
    if docker_available:
        return _TIER_DOCKER
    if platform_system == "Linux":
        return _TIER_LINUX_HOST
    return _TIER_NO_FAITHFUL_ENV


def detect_verification_tier() -> str:
    """Probe real environment and return the verification tier string.

    Calls ``classify_verification_tier`` with:
    - docker_available: True iff ``docker info`` exits 0 within 5 s.
    - platform_system: ``platform.system()`` (= "Linux" / "Darwin" / "Windows" …).

    Keep this thin — all classification logic lives in the pure classifier.
    """
    docker_ok = False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        docker_ok = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        docker_ok = False

    return classify_verification_tier(
        docker_available=docker_ok,
        platform_system=platform.system(),
    )


def _make_verify_skip_record(tier: str, reason: str) -> dict:
    """Return the three fields added to every verify-skipped result record."""
    return {
        "verify_tier": tier,
        "verify_skipped": True,
        "verify_skip_reason": reason,
    }


# ── Tier-1: model_patch extraction ──────────────────────────────────────────────


def extract_model_patch(workspace: "Path") -> str:
    """Extract ``git diff HEAD`` from the skill's workspace directory.

    This is the AI's solution: the cumulative diff of all changes made
    by the skill relative to the base commit checked out in the workspace.

    Returns the diff string (may be empty if no changes were made).
    Raises ``subprocess.CalledProcessError`` if git is not available or the
    workspace is not a git repository.
    """
    result = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # git diff HEAD exits 0 whether or not there are changes
    result.check_returncode()
    return result.stdout


def build_swebench_prediction(instance_id: str, model_patch: str) -> dict:
    """Build the swebench prediction dict for one instance.

    swebench's run_evaluation.main loads the full instance (repo/base_commit/
    test_patch/version) from the official HuggingFace dataset by instance_id,
    so the prediction only needs the model_patch (the AI's solution diff).

    Constants mirror swebench.harness.run_evaluation:
      KEY_INSTANCE_ID = "instance_id"
      KEY_MODEL       = "model_name_or_path"
      KEY_PREDICTION  = "model_patch"
    """
    return {
        "instance_id": instance_id,
        "model_name_or_path": "reyn",
        "model_patch": model_patch,
    }


# ── Tier-1: swebench delegation (LAZY IMPORT — swebench is NOT a reyn core dep) ──
#
# swebench is a Tier1-only optional benchmark extra.  It is intentionally NOT
# imported at module top so that:
#   (a) reyn's runtime and all non-Tier1 code paths never import swebench, and
#   (b) the import error on missing swebench is surfaced as an honest-skip,
#       not a module-level ImportError that breaks every `reyn eval benchmark` run.
#
# Install separately when running Tier1 evals on a Docker host:
#   pip install swebench
#
# DO NOT add swebench to reyn's core install_requires / pyproject.toml deps.


def run_tier1_swebench_eval(
    instance_id: str,
    model_patch: str,
    dataset_name: str = "princeton-nlp/SWE-bench_Verified",
    split: str = "test",
    run_id: str | None = None,
    timeout: int = 1800,
    working_dir: "Path | None" = None,
) -> dict:
    """Delegate authoritative scoring to swebench's official run_evaluation.main.

    This is the Tier-1 faithful eval path.  It pulls the official pre-built
    SWE-bench Docker image (namespace="swebench"), applies model_patch +
    test_patch in the container, runs the test_cmd, and parses the result
    via swebench's official logic.

    Returns a dict with:
      - "resolved":  bool — authoritative pass/fail verdict from swebench
      - "run_id":    str  — the swebench run_id used
      - "report_path": str — path to the per-instance report.json

    Raises:
      - ``RuntimeError("swebench_missing")`` if swebench is not installed
        (caller should convert to honest-skip with _TIER1_SWEBENCH_MISSING_REASON).
      - ``RuntimeError("eval_error:<msg>")`` on any other evaluation failure.

    IMPORTANT: swebench writes its logs relative to cwd.
    ``working_dir`` must be set to a temp directory so logs are isolated and
    don't pollute the reyn project tree.

    Architecture note:
    - swebench.harness.run_evaluation.main is used (not run_instances directly)
      because main handles the full flow: predictions file → dataset load →
      run_instances → clean → make_run_report.
    - The predictions are written to a temp JSON file as a JSON array (one dict
      per line in swebench format) and passed via predictions_path.
    - Per-instance report.json path:
        logs/run_evaluation/<run_id>/reyn/<instance_id>/report.json
      (relative to working_dir; LOG_REPORT="report.json";
       model_name_or_path="reyn" with "/" → "__" replacement = "reyn")
    """
    # Lazy import — Tier1 ONLY.  swebench is NOT a reyn core dependency.
    try:
        from swebench.harness.run_evaluation import (  # type: ignore[import]
            LOG_REPORT,
            RUN_EVALUATION_LOG_DIR,
        )
        from swebench.harness.run_evaluation import (
            main as swebench_run_evaluation_main,
        )
    except ImportError:
        raise RuntimeError("swebench_missing")

    import os
    import uuid

    effective_run_id = run_id or ("reyn_tier1_" + uuid.uuid4().hex[:8])
    prediction = build_swebench_prediction(instance_id, model_patch)

    with tempfile.TemporaryDirectory(prefix="reyn-tier1-") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Write predictions to a JSON file (swebench expects a JSON array)
        predictions_path = tmp_path / "predictions.json"
        predictions_path.write_text(
            json.dumps([prediction], ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        # swebench writes logs relative to cwd — we run from working_dir (or
        # a temp subdir) so logs don't land in reyn's project tree.
        run_cwd = working_dir if working_dir is not None else tmp_path
        run_cwd = Path(run_cwd)

        try:
            orig_cwd = Path.cwd()
            os.chdir(run_cwd)
            try:
                swebench_run_evaluation_main(
                    dataset_name=dataset_name,
                    split=split,
                    instance_ids=[instance_id],
                    predictions_path=str(predictions_path),
                    run_id=effective_run_id,
                    max_workers=1,
                    force_rebuild=False,
                    cache_level="env",
                    clean=False,
                    open_file_limit=4096,
                    timeout=timeout,
                    namespace="swebench",
                    rewrite_reports=False,
                    modal=False,
                    instance_image_tag="latest",
                    env_image_tag="latest",
                    report_dir=str(run_cwd),
                )
            finally:
                os.chdir(orig_cwd)
        except Exception as exc:
            raise RuntimeError(f"eval_error:{exc}") from exc

        # Read the per-instance report.json.
        # swebench writes it at:
        #   <cwd>/logs/run_evaluation/<run_id>/reyn/<instance_id>/report.json
        # model_name_or_path="reyn" → no "/" so no "__" substitution needed.
        report_path = (
            run_cwd
            / RUN_EVALUATION_LOG_DIR
            / effective_run_id
            / "reyn"
            / instance_id
            / LOG_REPORT
        )

        if not report_path.exists():
            raise RuntimeError(
                f"eval_error:swebench report.json not found at {report_path}"
            )

        try:
            report_data = json.loads(report_path.read_text(encoding="utf-8"))
            resolved: bool = bool(report_data[instance_id]["resolved"])
        except (KeyError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"eval_error:failed to parse report.json: {exc}") from exc

        return {
            "resolved": resolved,
            "run_id": effective_run_id,
            "report_path": str(report_path),
        }


# ── public entry points (called from eval.py) ─────────────────────────────────


def register_benchmark(eval_sub) -> None:
    """Register the `reyn eval benchmark` sub-command."""
    p = eval_sub.add_parser(
        "benchmark",
        help="Run a skill against a JSONL task file in batch (generic benchmark runner)",
    )
    p.add_argument(
        "skill_name", metavar="SKILL",
        help="Skill name to run (resolved via reyn/project → local → stdlib)",
    )
    p.add_argument(
        "--tasks", required=True, metavar="PATH",
        help="Path to JSONL task file; each line is one task input (matches skill's input_schema)",
    )
    p.add_argument(
        "--output", required=True, metavar="DIR",
        help="Output directory; results are written under <DIR>/run_<timestamp>/",
    )
    p.add_argument(
        "--concurrency", type=int, default=4, metavar="N",
        help="Maximum number of concurrent skill runs (default: 4)",
    )
    p.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Stop after the first N tasks (applied after --resume filtering)",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="Resume from the latest prior run in <output>; skip already-completed tasks",
    )
    p.add_argument(
        "--model", default=None, metavar="MODEL",
        help="Model override (default: from reyn.yaml)",
    )
    p.add_argument(
        "--allow-shell", dest="allow_shell", action="store_true",
        help=(
            "Enable the 'shell' Control IR op for every task in this batch. "
            "Required when the target skill declares `permissions.shell: true` "
            "(e.g. `swe_bench`, which checks out repos + runs tests). "
            "Off by default for safety; matches `reyn run --allow-shell`."
        ),
    )
    p.add_argument(
        "--allow-unsafe-python", "--allow-untrusted-python",
        dest="allow_unsafe_python", action="store_true",
        help=(
            "Enable unsafe-mode Python preprocessor steps (no AST sandboxing) "
            "for every task in this batch. Safe-mode python steps run without "
            "this flag. Off by default; matches `reyn run --allow-unsafe-python`."
        ),
    )
    p.add_argument(
        "--clone-task-repo", dest="clone_task_repo", action="store_true",
        help=(
            "Before each task runs, initialise its workspace by cloning "
            "https://github.com/<task.data.repo>.git and checking out "
            "<task.data.base_commit>. Required for SWE-bench tasks "
            "whose skill expects a pre-cloned repo (= setup phase issues "
            "`git checkout` against the empty workspace otherwise). "
            "Off by default for generic batch runs."
        ),
    )


def run_benchmark(args: argparse.Namespace) -> None:
    """Entry point for `reyn eval benchmark` — called by eval._dispatch."""
    from reyn.llm.llm import run_async as _run_async

    run_async = _run_async
    run_async(_run_benchmark_async(args))


# ── JSONL task parsing ────────────────────────────────────────────────────────


def load_tasks(path: Path) -> list[dict]:
    """Parse a JSONL file; raise SystemExit on malformed lines.

    Tolerates trailing blank lines; surface a clear error on JSON decode failure
    including line number and decode reason.
    """
    tasks: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            print(
                f"Error: tasks file line {lineno}: {exc}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not isinstance(obj, dict):
            print(
                f"Error: tasks file line {lineno}: expected a JSON object, got {type(obj).__name__}",
                file=sys.stderr,
            )
            sys.exit(1)
        tasks.append(obj)
    return tasks


# ── instance_id / run_id helpers ──────────────────────────────────────────────


def _instance_id(task: dict, index: int) -> str:
    """Derive a stable instance identifier from the task dict."""
    if "instance_id" in task:
        return str(task["instance_id"])
    return f"task_{index:04d}"


def _make_run_id() -> str:
    """Generate a run identifier from current UTC time."""
    return "run_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


# ── resume: read prior completed set ─────────────────────────────────────────


def _find_latest_run_dir(output_root: Path) -> Path | None:
    """Return the most recent run_* subdirectory, or None if none exists."""
    candidates = sorted(
        (d for d in output_root.iterdir() if d.is_dir() and d.name.startswith("run_")),
        key=lambda d: d.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _load_completed_ids(run_dir: Path) -> set[str]:
    """Read completed instance_ids from a prior run's summary.json."""
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        return set()
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
        return set(data.get("completed_ids", []))
    except Exception:
        return set()


# ── summary.json helpers ──────────────────────────────────────────────────────


def compute_faithful_accounting(results: list[dict]) -> dict:
    """Compute faithful PASS-rate accounting from a list of result records.

    THE INVARIANT: pass_rate is computed over the faithful-verified subset
    ONLY (results where verify_skipped is not True).  A verify_skipped result
    is NEVER counted as pass or fail.

    Binary-sharpening (C7 PR3): For Tier-1 evaluated results, the authoritative
    C7 verdict is harness_resolved (the swebench official eval result), NOT the
    skill's self-check (tests_passed).  The pass-source priority is:

      1. harness_resolved  — present on Tier-1 (Docker) results; authoritative.
      2. tests_passed      — present on Tier-2/3 results; informational only
                             when harness_resolved is also present.

    When a faithful result has harness_resolved, it is used exclusively.
    When it only has tests_passed (no harness_resolved), tests_passed is used.
    A faithful result with neither field contributes to faithful_verified_count
    but not to faithful_passed (it neither increments nor decrements the count).

    Returns a dict with:
      - faithful_verified_count: int — number of results where verify_skipped is falsy
      - faithful_passed: int | None — pass count over faithful subset using
        harness_resolved (authoritative) if present, else tests_passed; None if
        no faithful result has either field
      - faithful_pass_rate: float | None — pass_rate over faithful subset (None if
        faithful_verified_count == 0 or no verdict field present)
      - skip_count: int — number of results with verify_skipped == True
    """
    faithful = [r for r in results if not r.get("verify_skipped")]
    skipped = [r for r in results if r.get("verify_skipped")]

    faithful_count = len(faithful)
    skip_count = len(skipped)

    # Determine pass count using the binary-sharpened priority:
    # harness_resolved (authoritative Tier-1 verdict) > tests_passed (self-check)
    has_any_verdict = any(
        "harness_resolved" in r or "tests_passed" in r for r in faithful
    )
    if has_any_verdict and faithful_count > 0:
        passed = 0
        for r in faithful:
            if "harness_resolved" in r:
                # Tier-1 path: use the harness verdict exclusively
                if r["harness_resolved"] is True:
                    passed += 1
            elif "tests_passed" in r:
                # Tier-2/3 path: use the skill self-check
                if r["tests_passed"] is True:
                    passed += 1
        pass_rate: float | None = passed / faithful_count
    else:
        passed = None
        pass_rate = None

    return {
        "faithful_verified_count": faithful_count,
        "faithful_passed": passed,
        "faithful_pass_rate": pass_rate,
        "skip_count": skip_count,
    }


def _write_summary(
    run_dir: Path,
    run_id: str,
    skill_name: str,
    results: list[dict],
    total_tasks: int,
) -> None:
    """Write (or overwrite) summary.json from current results list.

    The ``pass_rate`` field is the FAITHFUL pass_rate — computed over
    verify-skipped=false results only.  When the faithful subset is empty,
    pass_rate is null.  The ``passed`` field likewise counts only faithful
    results.  verify-skipped counts are shown prominently under
    ``verify_accounting``.
    """
    completed = len(results)
    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)
    avg_cost = total_cost / completed if completed else 0.0

    # Faithful PASS-rate accounting (C7 invariant)
    acct = compute_faithful_accounting(results)
    passed = acct["faithful_passed"]
    pass_rate = acct["faithful_pass_rate"]

    # Backward compat: also keep the old has_tests_passed path for completed_ids
    # attempts: only if field present
    has_attempts = any("attempts" in r for r in results)
    if has_attempts:
        attempt_values = [r["attempts"] for r in results if isinstance(r.get("attempts"), (int, float))]
        avg_attempts: float | None = (sum(attempt_values) / len(attempt_values)) if attempt_values else None
    else:
        avg_attempts = None

    # completed_ids: used by --resume
    completed_ids = [r["instance_id"] for r in results]

    summary = {
        "run_id": run_id,
        "skill": skill_name,
        "total": total_tasks,
        "completed": completed,
        "passed": passed,
        "pass_rate": pass_rate,
        # Prominent faithful-verification accounting block (C7 invariant).
        # Never bury the skip count — it must be visible at a glance.
        "verify_accounting": {
            "faithful_verified": acct["faithful_verified_count"],
            "faithful_passed": acct["faithful_passed"],
            "faithful_pass_rate": acct["faithful_pass_rate"],
            "verify_skipped": acct["skip_count"],
            "total": completed,
        },
        "total_cost_usd": total_cost,
        "avg_cost_per_instance": avg_cost,
        "avg_attempts": avg_attempts,
        "completed_ids": completed_ids,
    }

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── workspace isolation ───────────────────────────────────────────────────────


@contextmanager
def _benchmark_isolated_workspace(
    task: dict | None = None,
    clone_task_repo: bool = False,
) -> Iterator[Path]:
    """Run body inside a throwaway temp directory to isolate .reyn/ writes.

    When ``clone_task_repo`` is True AND the task carries both ``repo``
    and ``base_commit`` fields (= SWE-bench task input convention), the
    workspace is initialised as a git checkout at the target commit
    before the skill runs (= FP-0008 PR-E). This unblocks tasks whose
    skill expects to operate on a pre-cloned repo (e.g. the swe_bench
    setup phase issues ``git checkout <base_commit>`` which only works
    inside an existing repository).

    The clone uses ``https://github.com/<repo>.git`` as the URL. On
    clone / checkout failure, the workspace is left empty (= no .git
    dir) so the task itself fails with a clear "not a git repository"
    error rather than hanging or masking the cause.
    """
    with tempfile.TemporaryDirectory(prefix="reyn-benchmark-") as tmp:
        tmp_path = Path(tmp)
        if clone_task_repo and task is not None:
            data = task.get("data", task) if isinstance(task, dict) else {}
            if isinstance(data, dict):
                repo = data.get("repo")
                base_commit = data.get("base_commit")
                if isinstance(repo, str) and isinstance(base_commit, str):
                    _init_workspace_from_repo(tmp_path, repo, base_commit)
        yield tmp_path


def _init_workspace_from_repo(
    workspace: Path, repo: str, base_commit: str,
) -> None:
    """Clone ``https://github.com/<repo>.git`` + checkout base_commit into workspace.

    Defensive: any subprocess failure (timeout, network, missing commit)
    is logged via stdlib logging and silently absorbed. The workspace
    is left as-is; the calling task will surface the missing-repo error
    via its own setup phase. The 600s clone timeout covers large repos
    on slow networks.
    """
    url = f"https://github.com/{repo}.git"
    try:
        subprocess.run(
            ["git", "clone", url, "."],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=600,
        )
        subprocess.run(
            ["git", "checkout", base_commit],
            cwd=workspace,
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logging.getLogger(__name__).warning(
            "benchmark workspace clone failed for repo=%s commit=%s: %s",
            repo, base_commit, exc,
        )


# ── per-task runner ───────────────────────────────────────────────────────────


async def _run_single_task(
    task: dict,
    instance_id: str,
    skill,
    skill_root: str,
    model: str,
    session,
    run_dir: Path,
    semaphore: asyncio.Semaphore,
    shell_allowed: bool = False,
    unsafe_python: bool = False,
    python_allowed_modules: list[str] | None = None,
    clone_task_repo: bool = False,
    verify_tier: str = _TIER_NO_FAITHFUL_ENV,
) -> dict:
    """Run a single task under the semaphore; return a result record.

    ``shell_allowed`` + ``unsafe_python`` are derived once at batch start
    (= from ``--allow-shell`` / ``--allow-unsafe-python``) and threaded through
    every task; ``Agent.from_config`` then derives the permission_resolver (and
    the rest of the runtime bundle) per task so the Agent's op_catalog exposes
    the shell op uniformly (#997 dir2 — the benchmark cannot omit the bundle).
    Without this, the LLM doesn't see ``shell`` in the catalog and hallucinates
    a fake schema (= ``{kind: "shell", op: "run", command: ...}`` vs the real
    ``{kind: "shell", cmd: ...}``), every retry hits the validator, the
    batch reports $0.00 cost + 100% error rate. See PR-D follow-up to PR-B
    (= FP-0008 sandbox_2 calibration block 2026-05-28).

    ``verify_tier`` is detected ONCE at batch start via
    ``detect_verification_tier()`` and passed through to every task.  It
    drives the honest-skip decision (C7 invariant): when there is no
    faithful verification environment, the result is marked verify_skipped
    instead of emitting a non-faithful PASS/FAIL.
    """
    from reyn.agent import Agent
    from reyn.config import _find_project_root, load_project_context

    async with semaphore:
        # Build input artifact
        input_artifact: dict
        if "type" in task:
            input_artifact = task
        else:
            input_artifact = {"type": "user_message", "data": task}

        project_root = _find_project_root(Path.cwd())
        project_context = load_project_context(session.config, project_root)

        result_data: dict = {}
        error_msg: str | None = None
        cost_usd: float | None = None
        # For Tier-1: extract the model_patch while the workspace is still live.
        # Stored here so it's accessible after the workspace context manager exits.
        _tier1_model_patch: str | None = None

        try:
            with _benchmark_isolated_workspace(
                task=task, clone_task_repo=clone_task_repo,
            ) as workspace_path:
                # #997 dir2: the permission/runtime bundle (permission_resolver,
                # mcp_servers, python_allowed_modules, prompt_cache_enabled,
                # sandbox_config, resolver) is derived from config inside
                # from_config — the benchmark cannot omit it (this WAS the FP-0008
                # gap: a missing permission_resolver/shell_allowed left shell out
                # of the catalog, the LLM hallucinated a fake schema). The
                # permission resolver is derived per task from shell_allowed +
                # unsafe_python (identical to the prior batch-built one, which used
                # the same _build_permission_resolver). interactive=False: a
                # benchmark subprocess is non-interactive.
                # #1199 S3.1c-1: the benchmark runs non-interactive in an
                # isolated, ephemeral per-task workspace clone — the operator's
                # explicit benchmark invocation IS the grant. With the
                # non-interactive decl auto-grant removed (S3.1c-1), a
                # benchmarked skill that declares out-of-zone file paths (e.g.
                # swe_bench's file.read/write: "*") needs a config-grant to be
                # approved without an interactive prompt. Scope it to the
                # benchmark's isolated workspace (same procedure as the
                # shell_allowed / unsafe_python pre-approvals above). setdefault
                # preserves any explicit operator setting.
                session.config.permissions.setdefault("file.read", "allow")
                session.config.permissions.setdefault("file.write", "allow")
                agent = Agent.from_config(
                    session.config,
                    shell_allowed=shell_allowed,
                    model=model,
                    resolver=session.resolver,
                    safety=session.safety_for(argparse.Namespace()),
                    python_allowed_modules=python_allowed_modules or None,
                    unsafe_python=unsafe_python,
                    interactive=False,
                    # PR-N9: benchmark is a non-interactive subprocess. An
                    # interactive bus blocks on tty raw_mode at limit-checkpoint
                    # boundaries (= sandbox_2 13977 4h+ hang at apply visit 6).
                    # Passing None routes through safety/limit_handler.py:173-179
                    # ``no_bus`` clean abort, the same path scripted/headless
                    # callers use. See FP-0008 PR-N9 / issue #1045.
                    intervention_bus=None,
                    project_context=project_context,
                    caller="direct",
                    workspace_base_dir=workspace_path,
                )
                run_result = await agent.run(skill, input_artifact)

                # Tier-1: extract model_patch while workspace is still live.
                # The workspace directory is deleted when the context manager exits,
                # so git diff HEAD must be captured here.
                if verify_tier == _TIER_DOCKER:
                    try:
                        _tier1_model_patch = extract_model_patch(workspace_path)
                    except Exception:
                        # Extraction failure is handled in the dispatch block below.
                        _tier1_model_patch = None

            if run_result.ok:
                result_data = run_result.data
            else:
                error_msg = f"skill ended with status '{run_result.status}'"
            cost_usd = run_result.cost_usd

            # Write P6 event log (per-instance)
            log_dir = run_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            _write_instance_log(log_dir / f"{instance_id}.jsonl", run_result)

        except Exception as exc:
            error_msg = str(exc)

        record: dict = {
            "instance_id": instance_id,
            "cost_usd": cost_usd,
            "error": error_msg,
        }

        # ── C7: Verification tier dispatch ────────────────────────────────────
        # Dispatch on verify_tier to determine whether this result is
        # faithfully verified or should be marked as skipped.
        #
        # Tier 1 (docker): Delegate authoritative scoring to swebench's
        #   official run_evaluation.main.  Extracts model_patch (captured above
        #   while the workspace was live) then calls swebench with the official
        #   pre-built image.  On success, sets harness_resolved (bool) as the
        #   authoritative C7 pass/fail.  On error (missing swebench, docker
        #   failure, patch-extraction failure, etc.) → honest-skip with a clear
        #   reason; NEVER fake PASS/FAIL.
        # Tier 2 (linux_host): uv-based build eval (future PR).
        #   Until then, stub — mark skipped with pending reason.
        # Tier 3 (no_faithful_env): ALWAYS skip — no faithful env available.
        #   On macOS/Windows, the AI's self-check (tests_passed from the skill)
        #   MUST NOT be promoted to an authoritative pass/fail.
        if verify_tier == _TIER_DOCKER:
            # Tier 1: Docker-based official SWE-bench image eval (C7 PR3).
            # model_patch was captured inside the workspace context above.
            task_data = task.get("data", task) if isinstance(task, dict) else {}
            dataset_name = (
                task_data.get("dataset_name", "princeton-nlp/SWE-bench_Verified")
                if isinstance(task_data, dict)
                else "princeton-nlp/SWE-bench_Verified"
            )
            tier1_skip_reason: str | None = None
            if _tier1_model_patch is None:
                tier1_skip_reason = "Tier1 eval error: failed to extract model_patch (git diff HEAD)"
            else:
                try:
                    tier1_result = run_tier1_swebench_eval(
                        instance_id=instance_id,
                        model_patch=_tier1_model_patch,
                        dataset_name=dataset_name,
                    )
                    record["verify_tier"] = _TIER_DOCKER
                    record["verify_skipped"] = False
                    record["harness_resolved"] = tier1_result["resolved"]
                except RuntimeError as rte:
                    msg = str(rte)
                    if msg == "swebench_missing":
                        tier1_skip_reason = _TIER1_SWEBENCH_MISSING_REASON
                    else:
                        # eval_error:<msg> or other runtime failure
                        clean_msg = msg.removeprefix("eval_error:") if msg.startswith("eval_error:") else msg
                        tier1_skip_reason = f"Tier1 eval error: {clean_msg}"
                except Exception as exc:
                    tier1_skip_reason = f"Tier1 eval error: {exc}"
            if tier1_skip_reason is not None:
                record.update(_make_verify_skip_record(_TIER_DOCKER, tier1_skip_reason))
        elif verify_tier == _TIER_LINUX_HOST:
            # Tier 2 (linux_host) — uv-based build eval (future PR).
            # Until then, stub — mark skipped with pending reason.
            record.update(_make_verify_skip_record(_TIER_LINUX_HOST, _TIER2_STUB_REASON))
        else:
            # Tier 3 (no_faithful_env) — always skip.
            # The AI's self-check result (tests_passed from skill output) is
            # NOT promoted to an authoritative pass/fail when verify_skipped.
            record.update(_make_verify_skip_record(_TIER_NO_FAITHFUL_ENV, _NO_FAITHFUL_ENV_REASON))

        # Lift well-known optional fields from result_data generically.
        # tests_passed is still recorded for debugging/analysis, but it is
        # excluded from pass_rate computation when verify_skipped is True
        # (enforced in compute_faithful_accounting via the faithful subset filter).
        if "tests_passed" in result_data:
            record["tests_passed"] = result_data["tests_passed"]
        if "attempts" in result_data:
            record["attempts"] = result_data["attempts"]

        # Write patch file if the output includes a patch field
        if "patch" in result_data and isinstance(result_data.get("patch"), str):
            patches_dir = run_dir / "patches"
            patches_dir.mkdir(parents=True, exist_ok=True)
            (patches_dir / f"{instance_id}.diff").write_text(
                result_data["patch"], encoding="utf-8"
            )

        return record


def _write_instance_log(log_path: Path, run_result) -> None:
    """Write a minimal per-instance event log entry."""
    entry = {
        "status": run_result.status,
        "cost_usd": run_result.cost_usd,
    }
    if run_result.error:
        entry["error"] = run_result.error
    log_path.write_text(json.dumps(entry, ensure_ascii=False) + "\n", encoding="utf-8")


# ── main async loop ───────────────────────────────────────────────────────────


async def _run_benchmark_async(args: argparse.Namespace) -> None:
    """Core async implementation of `reyn eval benchmark`."""
    from reyn.cli import session as session_mod
    from reyn.cli.skill_loader import resolve_skill_path
    from reyn.compiler import load_dsl_skill

    session = session_mod.Session.from_args(args)
    model = getattr(args, "model", None) or session.config.model

    # Derive shell_allowed + unsafe_python once at batch start and thread them
    # through every task; Agent.from_config derives the permission_resolver (+
    # the rest of the runtime bundle) per task (#997 dir2). Without the right
    # shell_allowed the Agent's OS runtime keeps `shell` out of the op_catalog
    # and the LLM hallucinates a fake schema on every retry — see
    # _run_single_task docstring.
    shell_allowed = session.shell_allowed_for(args)
    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    clone_task_repo = bool(getattr(args, "clone_task_repo", False))

    # C7: Detect verification tier ONCE at batch start.
    # All tasks in this run share the same host environment, so detecting
    # once and threading through is correct and efficient.
    verify_tier = detect_verification_tier()

    # Load tasks
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"Error: tasks file not found: {tasks_path}", file=sys.stderr)
        sys.exit(1)

    all_tasks = load_tasks(tasks_path)
    if not all_tasks:
        print("Error: tasks file is empty.", file=sys.stderr)
        sys.exit(1)

    # Resolve skill
    skill_dir, inferred_root = resolve_skill_path(args.skill_name)
    skill_md = skill_dir / "skill.md"
    try:
        skill = load_dsl_skill(str(skill_md), skill_root=str(inferred_root))
    except Exception as e:
        print(f"Error: failed to compile skill '{args.skill_name}': {e}", file=sys.stderr)
        sys.exit(1)

    # Build output dir
    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)

    run_id = _make_run_id()
    run_dir = output_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Resume: find completed ids from latest prior run
    skip_ids: set[str] = set()
    if args.resume:
        prior = _find_latest_run_dir(output_root)
        # prior may be run_dir itself if timestamps collide in tests; skip it
        if prior and prior != run_dir:
            skip_ids = _load_completed_ids(prior)
            if skip_ids:
                print(f"  --resume: skipping {len(skip_ids)} already-completed instance(s)")

    # Filter tasks
    pending: list[tuple[int, dict]] = []
    for i, task in enumerate(all_tasks):
        iid = _instance_id(task, i)
        if iid not in skip_ids:
            pending.append((i, task))

    # Apply --limit (N remaining, not N total)
    if args.limit is not None and args.limit > 0:
        pending = pending[: args.limit]

    total_tasks = len(all_tasks)
    print(f"eval benchmark: {args.skill_name}")
    print(f"  model:       {model}")
    print(f"  tasks:       {tasks_path}  ({total_tasks} total, {len(pending)} to run)")
    print(f"  concurrency: {args.concurrency}")
    print(f"  output:      {run_dir}")
    print(f"  verify_tier: {verify_tier}")
    if verify_tier == _TIER_NO_FAITHFUL_ENV:
        print("  NOTE: no faithful verification env — all results will be verify_skipped=true")
        print("        pass_rate will be null (0 faithful-verified tasks)")
    print()

    results: list[dict] = []
    semaphore = asyncio.Semaphore(args.concurrency)

    # Collect coroutines; run with semaphore
    async def _task_with_progress(index: int, task: dict) -> None:
        iid = _instance_id(task, index)
        try:
            record = await _run_single_task(
                task=task,
                instance_id=iid,
                skill=skill,
                skill_root=str(inferred_root),
                model=model,
                session=session,
                run_dir=run_dir,
                semaphore=semaphore,
                shell_allowed=shell_allowed,
                unsafe_python=unsafe_python,
                clone_task_repo=clone_task_repo,
                verify_tier=verify_tier,
            )
        except Exception as exc:
            # A task failure never aborts the batch — log and record the error.
            record = {"instance_id": iid, "cost_usd": None, "error": str(exc)}
        results.append(record)
        status = "pass" if record.get("tests_passed") is True else (
            "error" if record.get("error") else "done"
        )
        print(f"  [{status}]  {iid}")
        # Write summary incrementally after each completion
        _write_summary(run_dir, run_id, args.skill_name, results, total_tasks)

    await asyncio.gather(*(_task_with_progress(i, t) for i, t in pending))

    # Final summary — C7 invariant: always show the prominent accounting line.
    print()
    completed = len(results)
    errors = sum(1 for r in results if r.get("error"))
    acct = compute_faithful_accounting(results)
    faithful_n = acct["faithful_verified_count"]
    skip_k = acct["skip_count"]
    faithful_passed = acct["faithful_passed"]
    faithful_rate = acct["faithful_pass_rate"]

    print(f"benchmark complete: {args.skill_name}")
    print(f"  completed: {completed}/{len(pending)}")
    if errors:
        print(f"  errors:    {errors}")
    # Prominent accounting line — required by C7 invariant.  Never bury the
    # skip count.  This line appears in stdout AND summary.json.
    if faithful_passed is not None:
        rate_str = f"  ({faithful_rate:.1%})" if faithful_rate is not None else ""
        print(
            f"  faithful verified: {faithful_n}/{completed}"
            f"  |  passed: {faithful_passed}/{faithful_n}{rate_str}"
            f"  |  skipped (no faithful env): {skip_k}"
        )
    else:
        print(
            f"  faithful verified: {faithful_n}/{completed}"
            f"  |  skipped (no faithful env): {skip_k}"
        )
    if faithful_n == 0:
        print("  pass_rate: n/a (0 faithful verified)")
    print(f"  summary → {run_dir / 'summary.json'}")
