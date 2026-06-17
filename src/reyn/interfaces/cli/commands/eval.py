"""`reyn eval` — eval sub-commands.

Sub-commands
------------
reyn eval run <skill_name> --dataset <path> [--threshold 0.8] [--tags smoke]
    Run a golden JSONL dataset against a skill; write results and exit 1
    when pass rate is below threshold (CI gate).

reyn eval report <skill_name>
    List past eval-run results for a skill in reverse-chronological order.

reyn eval compare <skill_name> [--baseline X] [--candidate Y] [--threshold 0.05]
    Compare two eval runs (regression diff).  Exit 1 when regressions detected.

reyn eval spec <FILE>  (legacy — run an eval.md spec against an app)
    Preserved for backward compatibility with the Component-A eval workflow.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ── helpers ───────────────────────────────────────────────────────────────────

_EVAL_SPEC_EPILOG = """
Note for skills using `python` preprocessor steps:
  Each python step must be approved before eval — eval runs non-interactively
  and cannot prompt. Two ways to pre-approve:

    (a) Run the target once interactively first:
          reyn run <target_skill> "<sample input>"
        Approve at the prompt; the choice is persisted to .reyn/approvals.yaml.

    (b) Set a project-wide allow in reyn.yaml:
          permissions:
            python.safe: allow      # for safe-mode steps
            python.unsafe: allow    # for unsafe-mode steps (also requires
                                    # --allow-unsafe-python at runtime)

  Without prior approval, the target's run will fail and the case will be
  marked as not-finished. The framing reads as a target-skill bug; it isn't.
"""

_RESULTS_DIR_TEMPLATE = ".reyn/eval-results/{skill}"


# ── registration ──────────────────────────────────────────────────────────────


def register(sub) -> None:
    p = sub.add_parser(
        "eval",
        help="Run a golden-dataset eval (run/report) or an eval.md spec (spec)",
    )
    eval_sub = p.add_subparsers(dest="eval_cmd", metavar="<eval-cmd>")
    eval_sub.required = True

    _register_run(eval_sub)
    _register_report(eval_sub)
    _register_compare(eval_sub)
    _register_spec(eval_sub)
    _register_benchmark(eval_sub)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> None:
    """Top-level dispatcher — called by the CLI after `args.func(args)`."""
    cmd = getattr(args, "eval_cmd", None)
    if cmd == "run":
        _run_golden(args)
    elif cmd == "report":
        _run_report(args)
    elif cmd == "compare":
        _run_compare(args)
    elif cmd == "spec":
        _run_spec(args)
    elif cmd == "benchmark":
        _run_benchmark(args)
    else:
        # Should never happen because eval_sub.required = True.
        print("Error: expected sub-command: run | report | compare | spec | benchmark", file=sys.stderr)
        sys.exit(1)


# ── `reyn eval run` ───────────────────────────────────────────────────────────


def _register_run(eval_sub) -> None:
    p = eval_sub.add_parser(
        "run",
        help="Run a golden JSONL dataset against a skill",
    )
    p.add_argument(
        "skill_name", metavar="SKILL",
        help="Skill name to evaluate (resolved via reyn/project → local → stdlib)",
    )
    p.add_argument(
        "--dataset", required=True, metavar="PATH",
        help="Path to the golden JSONL dataset file",
    )
    p.add_argument(
        "--threshold", type=float, default=0.8, metavar="FLOAT",
        help="Minimum pass rate to exit 0 (default: 0.8).  CI gate: exit 1 if below.",
    )
    p.add_argument(
        "--tags", default=None, metavar="TAGS",
        help="Comma-separated tags to filter cases (e.g. smoke,regression)",
    )
    p.add_argument(
        "--model", default=None, metavar="MODEL",
        help="Model override (default: from reyn.yaml)",
    )


def _run_golden(args: argparse.Namespace) -> None:
    """Execute `reyn eval run`."""
    from reyn.interfaces.cli.invocation_context import InvocationContext
    from reyn.interfaces.cli.skill_loader import resolve_skill_path

    session = InvocationContext.from_args(args)
    model = getattr(args, "model", None) or session.config.model

    # Load dataset
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        print(f"Error: dataset file not found: {dataset_path}", file=sys.stderr)
        sys.exit(1)

    cases = _load_dataset(dataset_path)
    if not cases:
        print("Error: dataset is empty.", file=sys.stderr)
        sys.exit(1)

    # Tag filter
    tag_filter: set[str] | None = None
    if getattr(args, "tags", None):
        tag_filter = {t.strip() for t in args.tags.split(",") if t.strip()}

    filtered = [c for c in cases if _matches_tags(c, tag_filter)]
    if not filtered:
        print(f"No cases match the requested tags: {tag_filter}", file=sys.stderr)
        sys.exit(1)

    # Resolve skill
    skill_dir, inferred_root = resolve_skill_path(args.skill_name)
    skill_md = skill_dir / "skill.md"

    from reyn.core.compiler import load_dsl_skill
    try:
        skill = load_dsl_skill(str(skill_md), skill_root=str(inferred_root))
    except Exception as e:
        print(f"Error: failed to compile skill '{args.skill_name}': {e}", file=sys.stderr)
        sys.exit(1)

    print(f"eval run: {args.skill_name}  [{len(filtered)} case(s)]")
    print(f"  model:   {model}")
    print(f"  dataset: {dataset_path}")
    print(f"  threshold: {args.threshold}")
    print()

    results: list[dict] = []
    start_all = time.monotonic()

    for case in filtered:
        result = _run_case(case, skill, str(inferred_root), model, session)
        results.append(result)
        sym = "pass" if result["pass"] else "FAIL"
        print(f"  [{sym}]  {result['case_id']}")

    elapsed = time.monotonic() - start_all

    # Persist results
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_dir = Path(_RESULTS_DIR_TEMPLATE.format(skill=args.skill_name))
    results_dir.mkdir(parents=True, exist_ok=True)
    result_path = results_dir / f"{timestamp}.jsonl"
    with result_path.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Summary
    passed = sum(1 for r in results if r["pass"])
    total = len(results)
    pass_rate = passed / total if total else 0.0

    print()
    print(f"eval result for {args.skill_name}:")
    print(f"  passed: {passed}/{total} ({pass_rate:.0%})")

    # By-tag breakdown
    all_tags: set[str] = set()
    for r in results:
        all_tags.update(r.get("tags") or [])
    if all_tags:
        print("  by tag:")
        for tag in sorted(all_tags):
            tag_cases = [r for r in results if tag in (r.get("tags") or [])]
            tag_passed = sum(1 for r in tag_cases if r["pass"])
            tag_total = len(tag_cases)
            pct = tag_passed / tag_total if tag_total else 0.0
            print(f"    {tag:<16}  {tag_passed}/{tag_total} ({pct:.0%})")

    print(f"  duration: {elapsed:.1f}s")
    print(f"  results → {result_path}")
    print()

    if pass_rate < args.threshold:
        print(
            f"FAIL: pass rate {pass_rate:.1%} < threshold {args.threshold:.1%}",
            file=sys.stderr,
        )
        sys.exit(1)


def _load_dataset(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of case dicts."""
    cases: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"Error: dataset line {lineno}: {exc}", file=sys.stderr)
            sys.exit(1)
    return cases


def _matches_tags(case: dict, tag_filter: set[str] | None) -> bool:
    if tag_filter is None:
        return True
    case_tags = set(case.get("tags") or [])
    return bool(case_tags & tag_filter)


def _run_case(
    case: dict,
    skill,
    skill_root: str,
    model: str,
    session,
) -> dict:
    """Run a single eval case; return a result record."""
    from reyn.config import _find_project_root, load_project_context
    from reyn.llm.llm import run_async
    from reyn.skill_runtime import SkillRuntime
    from reyn.user_intervention import StdinInterventionBus

    case_id = _make_case_id(case)
    input_artifact = case.get("input", {})
    expected = case.get("expected", {})
    compare_mode = case.get("compare_mode", "exact")
    tags = case.get("tags") or []

    # Wrap raw input dict as an artifact if it lacks a 'type' key.
    if isinstance(input_artifact, dict) and "type" not in input_artifact:
        input_artifact = {"type": "user_message", "data": input_artifact}

    # Workspace isolation: run inside a temporary directory so .reyn/ writes
    # go to tmp_dir/.reyn/ rather than the project's .reyn/.
    project_root = _find_project_root(Path.cwd())
    project_context = load_project_context(session.config, project_root)

    # #997 dir2: config-derived permission/runtime bundle via from_config.
    agent = SkillRuntime.from_config(
        session.config,
        model=model,
        resolver=session.resolver,
        safety=session.safety_for(argparse.Namespace()),
        intervention_bus=StdinInterventionBus(),
        project_context=project_context,
        caller="direct",
    )

    actual: dict = {}
    passed = False
    score = 0.0
    error_msg: str | None = None

    with _isolated_workspace():
        try:
            result = run_async(agent.run(skill, input_artifact))
            actual = result.data
            if result.ok:
                passed, score = _compare(actual, expected, compare_mode)
            else:
                error_msg = f"skill ended with status '{result.status}'"
        except Exception as exc:
            error_msg = str(exc)

    return {
        "case_id": case_id,
        "input": case.get("input", {}),
        "expected": expected,
        "actual": actual,
        "pass": passed,
        "score": score,
        "skill_version_hash": None,  # FP-0006-A landed on run_skill_started events; eval result record wiring deferred
        "tags": tags,
        "compare_mode": compare_mode,
        **({"error": error_msg} if error_msg else {}),
    }


@contextmanager
def _isolated_workspace() -> Iterator[Path]:
    """Context manager: run body inside a throwaway temp directory.

    The Agent uses the CWD-relative path ".reyn" for its state directory.
    By chdir-ing into a tmpdir for the duration of each skill run, all
    .reyn/ writes (events, artifacts, WAL) stay in the tmpdir and never
    pollute the project's .reyn/.
    """
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="reyn-eval-") as tmp:
        try:
            os.chdir(tmp)
            yield Path(tmp)
        finally:
            os.chdir(original_cwd)


def _compare(actual: dict, expected: dict, mode: str) -> tuple[bool, float]:
    """Compare actual final_output against expected.

    Returns (passed, score) where score is 1.0 for exact match, 0.0 otherwise.

    judge mode: Component D (judge_output op) is not yet landed.  Gracefully
    fall back to exact mode and emit a warning so CI logs are self-explaining.
    """
    if mode == "judge":
        warnings.warn(
            "compare_mode='judge' requires Component D (judge_output op) which "
            "is not yet implemented. Falling back to exact mode for this case.",
            stacklevel=3,
        )
        mode = "exact"

    if mode == "exact":
        matched = actual == expected
        return matched, 1.0 if matched else 0.0

    # Unknown mode — treat as fail.
    warnings.warn(
        f"Unknown compare_mode '{mode}'. Treating as fail.",
        stacklevel=3,
    )
    return False, 0.0


def _make_case_id(case: dict) -> str:
    """Derive a stable case identifier from the case dict."""
    if "id" in case:
        return str(case["id"])
    tags = case.get("tags") or []
    tag_str = "_".join(sorted(tags)) if tags else "case"
    # Include a short hash of the input for uniqueness.
    import hashlib
    h = hashlib.sha1(
        json.dumps(case.get("input", {}), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:8]
    return f"{tag_str}_{h}"


# ── `reyn eval report` ────────────────────────────────────────────────────────


def _register_report(eval_sub) -> None:
    p = eval_sub.add_parser(
        "report",
        help="List past eval-run results for a skill",
    )
    p.add_argument(
        "skill_name", metavar="SKILL",
        help="Skill name to show eval history for",
    )
    p.add_argument(
        "--limit", type=int, default=10, metavar="N",
        help="Number of past results to show (default: 10)",
    )
    p.add_argument(
        "--threshold", type=float, default=0.8, metavar="FLOAT",
        help="Threshold to mark pass/fail in the history (default: 0.8)",
    )


def _run_report(args: argparse.Namespace) -> None:
    """Execute `reyn eval report`."""
    results_dir = Path(_RESULTS_DIR_TEMPLATE.format(skill=args.skill_name))
    if not results_dir.exists():
        print(f"No eval results found for skill '{args.skill_name}'.")
        print(f"  (looked in: {results_dir})")
        return

    result_files = sorted(
        results_dir.glob("*.jsonl"),
        key=lambda p: p.name,
        reverse=True,
    )[: args.limit]

    if not result_files:
        print(f"No eval results found for skill '{args.skill_name}'.")
        return

    print(f"{args.skill_name} eval history:")
    for path in result_files:
        summary = _summarise_result_file(path)
        ts_display = _format_timestamp(path.stem)
        pct = summary["pass_rate"]
        passed = summary["passed"]
        total = summary["total"]
        threshold = args.threshold
        gate = "pass" if pct >= threshold else "FAIL"
        print(
            f"  {ts_display}  {pct:.0%} pass ({passed}/{total})"
            f"  threshold: {threshold}  [{gate}]"
        )


def _summarise_result_file(path: Path) -> dict:
    """Compute pass statistics from a JSONL result file."""
    records: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    except Exception:
        return {"passed": 0, "total": 0, "pass_rate": 0.0}

    total = len(records)
    passed = sum(1 for r in records if r.get("pass"))
    pass_rate = passed / total if total else 0.0
    return {"passed": passed, "total": total, "pass_rate": pass_rate}


def _format_timestamp(stem: str) -> str:
    """Format a YYYYMMDDTHHMMSSZ stem into a human-readable string.

    The stem is 16 chars: ``20260514T213000Z``  (15 + trailing Z).
    Falls back to the raw stem if it doesn't match the expected pattern.
    """
    try:
        # Strip trailing 'Z' suffix before parsing.
        bare = stem.rstrip("Z")[:15]
        dt = datetime.strptime(bare, "%Y%m%dT%H%M%S")
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stem


# ── `reyn eval compare` ───────────────────────────────────────────────────────


def _register_compare(eval_sub) -> None:
    p = eval_sub.add_parser(
        "compare",
        help="Compare two eval runs for a skill (regression diff)",
    )
    p.add_argument(
        "skill_name", metavar="SKILL",
        help="Skill name to compare eval runs for",
    )
    p.add_argument(
        "--baseline", default=None, metavar="RUN_ID",
        help=(
            "Baseline run_id (filename stem) or skill_version_hash prefix. "
            "Defaults to the most recent run with a different version_hash than candidate."
        ),
    )
    p.add_argument(
        "--candidate", default=None, metavar="RUN_ID",
        help=(
            "Candidate run_id (filename stem) or skill_version_hash prefix. "
            "Defaults to the most recent eval run for the skill."
        ),
    )
    p.add_argument(
        "--threshold", type=float, default=0.05, metavar="FLOAT",
        help="Score-drop magnitude that triggers a regression alert (default: 0.05).",
    )
    p.add_argument(
        "--format", dest="output_format", choices=["text", "json"],
        default="text", metavar="FORMAT",
        help="Output format: text (default) or json.",
    )


def _run_compare(args: argparse.Namespace) -> None:
    """Execute ``reyn eval compare``."""
    from reyn.dev.eval.compare import compute_diff
    from reyn.dev.eval.result_loader import load_run_by_id, load_runs_for_skill

    skill = args.skill_name
    threshold = args.threshold

    all_runs = load_runs_for_skill(skill, _RESULTS_DIR_TEMPLATE)

    if len(all_runs) < 2:
        count = len(all_runs)
        if count == 0:
            print(
                f"Error: no eval results found for skill '{skill}'.",
                file=sys.stderr,
            )
        else:
            print(
                f"Error: cannot compare; need 2+ runs, found {count} for '{skill}'.",
                file=sys.stderr,
            )
        sys.exit(2)

    # Resolve candidate
    if args.candidate:
        candidate_run = load_run_by_id(skill, args.candidate, _RESULTS_DIR_TEMPLATE)
        if candidate_run is None:
            print(
                f"Error: candidate run '{args.candidate}' not found for skill '{skill}'.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Most recent run
        candidate_run = all_runs[0]

    # Resolve baseline
    if args.baseline:
        baseline_run = load_run_by_id(skill, args.baseline, _RESULTS_DIR_TEMPLATE)
        if baseline_run is None:
            print(
                f"Error: baseline run '{args.baseline}' not found for skill '{skill}'.",
                file=sys.stderr,
            )
            sys.exit(2)
    else:
        # Most recent run with a DIFFERENT version_hash than candidate
        c_hash = candidate_run.get("skill_version_hash") or "unknown"
        baseline_run = None
        for run in all_runs:
            if run["run_id"] == candidate_run["run_id"]:
                continue
            b_hash = run.get("skill_version_hash") or "unknown"
            if b_hash != c_hash or c_hash == "unknown":
                baseline_run = run
                break
        # If no different-hash run exists, fall back to the second-most-recent run
        if baseline_run is None:
            for run in all_runs:
                if run["run_id"] != candidate_run["run_id"]:
                    baseline_run = run
                    break
        if baseline_run is None:
            print(
                f"Error: could not find a baseline run for skill '{skill}'.",
                file=sys.stderr,
            )
            sys.exit(2)

    diff = compute_diff(baseline_run, candidate_run, threshold)

    if args.output_format == "json":
        _print_compare_json(skill, diff)
    else:
        _print_compare_text(skill, diff)

    if diff["alert"]:
        sys.exit(1)


def _hash_display(h: str) -> str:
    """Truncate a version hash to 8 chars for readability."""
    if not h or h == "unknown":
        return "unknown"
    return h[:8]


def _print_compare_text(skill: str, diff: dict) -> None:
    b = diff["baseline"]
    c = diff["candidate"]
    s = diff["summary"]
    threshold = diff["threshold"]

    print(f"eval compare: {skill}")
    print(
        f"baseline:  run_id={b['run_id']}, "
        f"version_hash={_hash_display(b['skill_version_hash'])}, "
        f"timestamp={b['timestamp']}"
    )
    print(
        f"candidate: run_id={c['run_id']}, "
        f"version_hash={_hash_display(c['skill_version_hash'])}, "
        f"timestamp={c['timestamp']}"
    )
    if diff.get("warning"):
        print(f"warning: {diff['warning']}")
    print(f"threshold: {threshold}")
    print()

    cases_compared = s["cases_compared"]
    mean_delta = s["mean_delta"]
    max_reg = s["max_regression"]
    max_imp = s["max_improvement"]
    regressing_count = s["regressing_count"]

    mean_str = f"{mean_delta:+.2f}" if mean_delta is not None else "n/a"
    max_reg_str = (
        f"{max_reg['delta']:+.2f}  (case_id={max_reg['case_id']})" if max_reg else "none"
    )
    max_imp_str = (
        f"{max_imp['delta']:+.2f}  (case_id={max_imp['case_id']})" if max_imp else "none"
    )

    print("Summary:")
    print(f"- cases compared:        {cases_compared}")
    print(f"- mean score Δ:         {mean_str}")
    print(f"- max regression:       {max_reg_str}")
    print(f"- max improvement:      {max_imp_str}")
    print(f"- cases regressing:      {regressing_count}  (above threshold)")

    missing_c = diff.get("missing_in_candidate", [])
    missing_b = diff.get("missing_in_baseline", [])
    if missing_c:
        print(f"- missing in candidate: {len(missing_c)}  ({', '.join(missing_c[:5])}{'...' if len(missing_c) > 5 else ''})")
    if missing_b:
        print(f"- missing in baseline:  {len(missing_b)}  ({', '.join(missing_b[:5])}{'...' if len(missing_b) > 5 else ''})")

    if diff["regressing_cases"]:
        print()
        print("Regressing cases:")
        for rc in diff["regressing_cases"]:
            print(
                f"- {rc['case_id']:<16} "
                f"{rc['baseline_score']:.2f} → {rc['candidate_score']:.2f}"
                f"  ({rc['delta']:+.2f})"
            )

    print()
    if diff["alert"]:
        pct = (regressing_count / cases_compared * 100) if cases_compared else 0.0
        print(
            f"ALERT: {regressing_count} cases regressed beyond threshold "
            f"({regressing_count}/{cases_compared} = {pct:.1f}%)."
        )
    else:
        print("OK: no regressions beyond threshold.")


def _print_compare_json(skill: str, diff: dict) -> None:
    b = diff["baseline"]
    c = diff["candidate"]

    output = {
        "skill": skill,
        "baseline": {
            "run_id": b["run_id"],
            "skill_version_hash": _hash_display(b["skill_version_hash"]),
            "timestamp": b["timestamp"],
        },
        "candidate": {
            "run_id": c["run_id"],
            "skill_version_hash": _hash_display(c["skill_version_hash"]),
            "timestamp": c["timestamp"],
        },
        "threshold": diff["threshold"],
        "summary": diff["summary"],
        "regressing_cases": diff["regressing_cases"],
        "missing_in_candidate": diff.get("missing_in_candidate", []),
        "missing_in_baseline": diff.get("missing_in_baseline", []),
        "alert": diff["alert"],
    }
    if diff.get("warning"):
        output["warning"] = diff["warning"]

    print(json.dumps(output, ensure_ascii=False, indent=2))


# ── `reyn eval spec` (legacy) ─────────────────────────────────────────────────


def _register_spec(eval_sub) -> None:
    """Register the legacy `reyn eval spec <FILE>` sub-command."""
    from reyn.interfaces.cli.common_args import (
        add_limits_args,
        add_model_arg,
        add_output_language_arg,
    )

    p = eval_sub.add_parser(
        "spec",
        help="Run an eval.md spec against an app (legacy Component-A path)",
        epilog=_EVAL_SPEC_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "spec", metavar="FILE",
        help="Path to the eval.md spec file (e.g. .reyn/evals/my_app/eval.md)",
    )
    add_model_arg(p)
    p.add_argument(
        "--skill-root", dest="skill_root", default=None, metavar="DIR",
        help="Skill-tree root override for the target app",
    )
    add_output_language_arg(p)
    add_limits_args(p)


def _run_spec(args: argparse.Namespace) -> None:
    """Execute `reyn eval spec` — the legacy Component-A eval path."""
    from reyn.core.compiler import load_dsl_skill
    from reyn.core.compiler.eval_loader import load_eval_spec
    from reyn.interfaces.cli.eval_report import EvalReport
    from reyn.interfaces.cli.invocation_context import InvocationContext
    from reyn.interfaces.cli.skill_loader import resolve_skill_path, stdlib_root
    from reyn.llm.llm import run_async
    from reyn.llm.pricing import TokenUsage

    session = InvocationContext.from_args(args)

    try:
        spec = load_eval_spec(args.spec)
    except Exception as e:
        print(f"Error loading eval spec: {e}", file=sys.stderr)
        sys.exit(1)

    target_skill_path, target_skill_root = _resolve_spec_target(args, spec)

    sl = stdlib_root()
    eval_app_md = sl / "skills" / "eval" / "skill.md"
    try:
        eval_app = load_dsl_skill(str(eval_app_md), skill_root=str(sl))
    except Exception as e:
        print(f"Error loading eval stdlib app: {e}", file=sys.stderr)
        sys.exit(1)

    model = args.model or spec.model or session.config.model
    output_language = session.output_language_for(args)
    resolved_model = session.resolver.resolve(model).model
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model

    print(f"=== Eval: {spec.skill_dsl_path}  [{len(spec.cases)} case(s)] ===")
    print(f"    model={model_display}")
    print()

    case_results: list[dict] = []
    total_tokens = TokenUsage()
    total_cost_usd = 0.0

    for case in spec.cases:
        case_result, usage, cost = _run_spec_case(
            case, eval_app, args, spec, target_skill_path, target_skill_root,
            model, output_language, session,
        )
        case_results.append(case_result)
        if usage:
            total_tokens = total_tokens + usage
        if cost is not None:
            total_cost_usd += cost
        print()

    from reyn.interfaces.cli.summary import print_eval_total

    all_passed = all(r.get("passed") for r in case_results)
    passed_count = sum(1 for r in case_results if r.get("passed"))
    overall_sym = "pass" if all_passed else "FAIL"
    print(f"{'═' * 55}")
    print(f" [{overall_sym}] {passed_count}/{len(case_results)} cases passed")
    print_eval_total(total_tokens, total_cost_usd)

    report = EvalReport(
        spec_path=args.spec,
        app=spec.skill_dsl_path,
        model=resolved_model,
        cases=case_results,
        total_tokens=total_tokens,
        total_cost_usd=total_cost_usd,
    )
    result_path = report.write_to(".reyn", Path(target_skill_path).parent.name)
    print(f" Results → {result_path}")
    print(f"{'═' * 55}")

    if not all_passed:
        sys.exit(2)


def _resolve_spec_target(args: argparse.Namespace, spec) -> tuple[str, str | None]:
    from reyn.interfaces.cli.skill_loader import resolve_skill_path

    app_ref = spec.skill_dsl_path
    if "/" not in app_ref and not app_ref.endswith(".md"):
        skill_dir, inferred_root = resolve_skill_path(app_ref)
        target_skill_path = str(skill_dir / "skill.md")
        target_skill_root = args.skill_root or str(inferred_root)
        print(f"resolved        : {target_skill_path}  (skill-root: {target_skill_root})")
        return target_skill_path, target_skill_root
    return app_ref, spec.skill_root or args.skill_root


def _run_spec_case(
    case, eval_app, args, spec, target_skill_path, target_skill_root,
    model, output_language, session,
) -> tuple[dict, object, float | None]:
    from reyn.config import _find_project_root, load_project_context
    from reyn.llm.llm import run_async
    from reyn.skill_runtime import SkillRuntime
    from reyn.user_intervention import StdinInterventionBus

    print(f"━━━ case: {case.name} ━━━")
    print(f"  input: {case.input[:120]}")

    phase_criteria = [
        {
            "phase_name": pc.phase,
            "criteria": [
                {"description": qc.text}
                if qc.tag != "aspirational"
                else {"description": qc.text, "required": False}
                for qc in pc.criteria
            ],
        }
        for pc in case.phase_criteria
        if pc.phase is not None
    ]

    input_artifact: dict = {
        "type": "eval_case_input",
        "data": {
            "case_name": case.name,
            "case_input": case.input,
            "spec_path": args.spec,
            "target_skill_path": target_skill_path,
            "phase_criteria": phase_criteria,
        },
    }
    if target_skill_root:
        input_artifact["data"]["skill_root"] = target_skill_root

    project_context = load_project_context(
        session.config, _find_project_root(Path.cwd()),
    )
    # #997 dir2: config-derived permission/runtime bundle via from_config.
    agent = SkillRuntime.from_config(
        session.config,
        model=model,
        resolver=session.resolver,
        safety=session.safety_for(args),
        intervention_bus=StdinInterventionBus(),
        project_context=project_context,
        caller="direct",
    )

    try:
        result = run_async(
            agent.run(eval_app, input_artifact, output_language=output_language)
        )
    except Exception as e:
        print(f"  Error: {e}", file=sys.stderr)
        return (
            {
                "case_name": case.name,
                "passed": False,
                "overall_score": 0.0,
                "passed_criteria": 0,
                "total_criteria": 0,
                "weakest_phase": "",
                "summary": str(e),
            },
            None,
            None,
        )

    data = result.data
    passed_sym = "pass" if data.get("passed") else "FAIL"
    score = data.get("overall_score", 0.0)
    pc_count = data.get("passed_criteria", 0)
    tc_count = data.get("total_criteria", 0)
    print(f"  [{passed_sym}] score={score:.2f}  ({pc_count}/{tc_count} required)")
    if data.get("weakest_phase"):
        print(f"  weakest: {data['weakest_phase']}")
    if data.get("summary"):
        print(f"  {data['summary']}")

    return ({"case_name": case.name, **data}, result.token_usage, result.cost_usd)


# ── `reyn eval benchmark` ─────────────────────────────────────────────────────


def _register_benchmark(eval_sub) -> None:
    """Register the `reyn eval benchmark` sub-command (delegated to eval_benchmark)."""
    from reyn.interfaces.cli.commands.eval_benchmark import register_benchmark

    register_benchmark(eval_sub)


def _run_benchmark(args: argparse.Namespace) -> None:
    """Execute `reyn eval benchmark` (delegated to eval_benchmark)."""
    from reyn.interfaces.cli.commands.eval_benchmark import run_benchmark

    run_benchmark(args)
