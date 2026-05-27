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
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

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


def _write_summary(
    run_dir: Path,
    run_id: str,
    skill_name: str,
    results: list[dict],
    total_tasks: int,
) -> None:
    """Write (or overwrite) summary.json from current results list."""
    completed = len(results)
    total_cost = sum(r.get("cost_usd") or 0.0 for r in results)
    avg_cost = total_cost / completed if completed else 0.0

    # tests_passed: only computed if ANY result has the field
    has_tests_passed = any("tests_passed" in r for r in results)
    passed = sum(1 for r in results if r.get("tests_passed") is True) if has_tests_passed else None
    pass_rate = (passed / completed) if (has_tests_passed and completed) else None

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
        "total_cost_usd": total_cost,
        "avg_cost_per_instance": avg_cost,
        "avg_attempts": avg_attempts,
        "completed_ids": completed_ids,
    }

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ── workspace isolation ───────────────────────────────────────────────────────


@contextmanager
def _benchmark_isolated_workspace() -> Iterator[Path]:
    """Run body inside a throwaway temp directory to isolate .reyn/ writes."""
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="reyn-benchmark-") as tmp:
        try:
            os.chdir(tmp)
            yield Path(tmp)
        finally:
            os.chdir(original_cwd)


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
    permission_resolver=None,
    python_allowed_modules: list[str] | None = None,
) -> dict:
    """Run a single task under the semaphore; return a result record.

    ``shell_allowed`` + ``permission_resolver`` are derived once at batch
    start (= from ``--allow-shell`` / ``--allow-unsafe-python`` + the
    skill's declared permissions) and threaded through every task so the
    Agent's op_catalog exposes the shell op uniformly. Without this, the
    LLM doesn't see ``shell`` in the catalog and hallucinates a fake
    schema (= ``{kind: "shell", op: "run", command: ...}`` vs the real
    ``{kind: "shell", cmd: ...}``), every retry hits the validator, the
    batch reports $0.00 cost + 100% error rate. See PR-D follow-up to PR-B
    (= FP-0008 sandbox_2 calibration block 2026-05-28).
    """
    from reyn.agent import Agent
    from reyn.config import _find_project_root, load_project_context
    from reyn.user_intervention import StdinInterventionBus

    async with semaphore:
        # Build input artifact
        input_artifact: dict
        if "type" in task:
            input_artifact = task
        else:
            input_artifact = {"type": "user_message", "data": task}

        project_root = _find_project_root(Path.cwd())
        project_context = load_project_context(session.config, project_root)

        agent = Agent(
            model=model,
            resolver=session.resolver,
            intervention_bus=StdinInterventionBus(),
            safety=session.safety_for(argparse.Namespace()),
            shell_allowed=shell_allowed,
            permission_resolver=permission_resolver,
            python_allowed_modules=python_allowed_modules or list(session.config.python.allowed_modules),
            mcp_servers=session.config.mcp,
            prompt_cache_enabled=session.config.prompt_cache_enabled,
            project_context=project_context,
            caller="direct",
        )

        result_data: dict = {}
        error_msg: str | None = None
        cost_usd: float | None = None

        try:
            with _benchmark_isolated_workspace():
                run_result = await agent.run(skill, input_artifact)

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

        # Lift well-known optional fields from result_data generically
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

    # Derive shell_allowed + permission_resolver once at batch start (= mirrors
    # `reyn run`'s pattern in cli/commands/run.py). Without this, the Agent's
    # OS runtime keeps `shell` out of the op_catalog and the LLM hallucinates
    # a fake schema on every retry — see _run_single_task docstring.
    shell_allowed = session.shell_allowed_for(args)
    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    from reyn.cli.commands.run import _build_permission_resolver
    perm_resolver = _build_permission_resolver(
        session.config, shell_allowed, unsafe_python=unsafe_python,
    )

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
                permission_resolver=perm_resolver,
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

    # Final summary
    print()
    completed = len(results)
    errors = sum(1 for r in results if r.get("error"))
    print(f"benchmark complete: {args.skill_name}")
    print(f"  completed: {completed}/{len(pending)}")
    if errors:
        print(f"  errors:    {errors}")
    has_tests_passed = any("tests_passed" in r for r in results)
    if has_tests_passed:
        passed = sum(1 for r in results if r.get("tests_passed") is True)
        rate = passed / completed if completed else 0.0
        print(f"  passed:    {passed}/{completed} ({rate:.1%})")
    print(f"  summary → {run_dir / 'summary.json'}")
