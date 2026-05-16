"""`reyn dogfood` — scenario-based regression testing framework (FP-0036).

Subcommands:
  run        Run a scenario set YAML file; record results under .reyn/dogfood/runs/
  coverage   Show feature-map coverage across one or more scenario set YAML files
  report     Print 4-band breakdown + Brier score from a stored run
  compare    Regression diff between a baseline run and a candidate run
  baseline   Symlink a run as a named baseline under .reyn/dogfood/baselines/
  publish    Create a GitHub Discussion thread from a stored run's summary.json

The CLI delegates to:
  load_scenario_set  — F1 (reyn.dogfood.scenarios)
  run_scenario_set   — F2 (reyn.dogfood.runner, this slice)
  compute_coverage   — F4 (reyn.dogfood.coverage)
  compare_runs       — F2 (reyn.dogfood.compare, this slice)
  publish_run        — FP-0036 (reyn.dogfood.publish)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def register(sub) -> None:
    p = sub.add_parser(
        "dogfood",
        help="Dogfood scenario regression testing (FP-0036)",
        description=(
            "Run scenario sets against the chat router, measure 4-band outcomes "
            "(verified / inconclusive / refuted / blocked), compare against baselines "
            "and surface regressions across releases."
        ),
    )
    dsub = p.add_subparsers(dest="dogfood_cmd", metavar="<subcommand>")
    dsub.required = True
    p.set_defaults(func=_no_subcommand)

    # --- run ---
    run_p = dsub.add_parser(
        "run",
        help="Run a scenario set and record results",
        description=(
            "Execute every scenario in <SET_YAML> through the chat router and "
            "record per-scenario outcomes under .reyn/dogfood/runs/<run_id>/."
        ),
    )
    run_p.add_argument("set_yaml", metavar="SET_YAML",
                       help="Path to the scenario set YAML file.")
    run_p.add_argument("--n", type=int, default=1, metavar="N",
                       help="Number of repetitions for stability bands (default: 1).")
    run_p.add_argument("--replay", metavar="FIXTURE_DIR",
                       help=(
                           "Run in replay mode using recorded LLM fixtures instead of "
                           "live LLM calls.  Pass the fixture directory recorded by "
                           "'reyn dogfood run' (F5 integration)."
                       ))
    run_p.add_argument("--agent", default="default", metavar="AGENT",
                       help="Chat-router agent name (default: 'default').")
    run_p.add_argument("--storage", metavar="DIR",
                       help=(
                           "Root directory for run output. "
                           "Default: .reyn/dogfood/runs/<run_id>."
                       ))
    run_p.add_argument("--run-id", metavar="RUN_ID",
                       help="Explicit run ID (UUID generated if omitted).")
    run_p.add_argument("--with-interpretation", action="store_true",
                       help=(
                           "After verifier scoring, generate a 3-line LLM "
                           "interpretation per scenario summarising whether "
                           "the run matched expectations. Adds ~$0.0005 / "
                           "scenario at flash-lite tier."
                       ))
    run_p.add_argument("--interpretation-model", metavar="MODEL", default=None,
                       help=(
                           "Override the LiteLLM model id used for "
                           "interpretation (default: openai/gemini-2.5-flash-lite)."
                       ))
    run_p.set_defaults(func=run_run)

    # --- coverage ---
    cov_p = dsub.add_parser(
        "coverage",
        help="Show feature-map coverage across scenario sets",
        description=(
            "Parse the feature map and walk one or more scenario set YAML files "
            "to produce a coverage matrix — covered feature count, uncovered list."
        ),
    )
    cov_p.add_argument("set_yamls", nargs="*", metavar="SET_YAML",
                        help=(
                            "One or more scenario set YAML files.  "
                            "Defaults to dogfood/scenarios/*.yaml if omitted."
                        ))
    cov_p.add_argument("--feature-map", default="docs/feature-map.md",
                        metavar="FILE",
                        help="Path to the feature map Markdown file (default: docs/feature-map.md).")
    cov_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit coverage as JSON instead of the default table.")
    cov_p.set_defaults(func=run_coverage)

    # --- report ---
    rep_p = dsub.add_parser(
        "report",
        help="Print 4-band breakdown + Brier from a stored run",
        description=(
            "Read the summary.json from a previous 'reyn dogfood run' and print "
            "the 4-band outcome breakdown (verified / inconclusive / refuted / blocked) "
            "plus Brier score if outcome predictions were present in the scenarios."
        ),
    )
    rep_p.add_argument("run_id", metavar="RUN_ID",
                        help=(
                            "Run ID or path to the run directory under "
                            ".reyn/dogfood/runs/."
                        ))
    rep_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit report as JSON instead of the default table.")
    rep_p.set_defaults(func=run_report)

    # --- compare ---
    cmp_p = dsub.add_parser(
        "compare",
        help="Regression diff between a baseline and a candidate run",
        description=(
            "Compare two stored runs. Exits 1 if the verified-rate drop "
            "exceeds --threshold (default 5 percentage points). Exits 2 on errors."
        ),
    )
    cmp_p.add_argument("baseline_run_id", metavar="BASELINE",
                        help="Run ID (or path) for the baseline run.")
    cmp_p.add_argument("candidate_run_id", metavar="CANDIDATE",
                        help="Run ID (or path) for the candidate run.")
    cmp_p.add_argument("--threshold", type=float, default=0.05, metavar="FLOAT",
                        help=(
                            "Verified-rate drop (0.0–1.0) that triggers a regression "
                            "alert (exit code 1). Default: 0.05 (5 percentage points)."
                        ))
    cmp_p.add_argument("--json", dest="output_json", action="store_true",
                        help="Emit comparison as JSON instead of the default table.")
    cmp_p.set_defaults(func=run_compare)

    # --- baseline ---
    bl_p = dsub.add_parser(
        "baseline",
        help="Symlink a run as a named baseline",
        description=(
            "Create a named baseline under .reyn/dogfood/baselines/<label>/ "
            "pointing at the given run. Use this label in 'reyn dogfood compare'."
        ),
    )
    bl_p.add_argument("run_id", metavar="RUN_ID",
                       help="Run ID (or path) to mark as a baseline.")
    bl_p.add_argument("--label", metavar="NAME",
                       help=(
                           "Baseline label. Defaults to the run_id if omitted. "
                           "Example: --label v1.2-stable"
                       ))
    bl_p.set_defaults(func=run_baseline)

    # --- publish ---
    from reyn.dogfood.publish import (  # noqa: E402
        _DEFAULT_TEMPLATE_PATH,
        DEFAULT_CATEGORY_SLUG,
        DEFAULT_REPO,
    )
    pub_p = dsub.add_parser(
        "publish",
        help="Create a GitHub Discussion thread from a stored run",
        description=(
            "Read the summary.json from a stored dogfood run, render a Discussion "
            "body from the Markdown template, and create a thread in the configured "
            "GitHub Discussions category. Authentication via GH_TOKEN or GITHUB_TOKEN "
            "env var (same convention as the gh CLI)."
        ),
    )
    pub_p.add_argument("run_id", metavar="RUN_ID",
                        help=(
                            "Run ID or path to the run directory under "
                            ".reyn/dogfood/runs/."
                        ))
    pub_p.add_argument("--repo", metavar="OWNER/REPO", default=None,
                        help=(
                            f"GitHub repository (default: '{DEFAULT_REPO}', "
                            "or detected from 'git remote get-url origin')."
                        ))
    pub_p.add_argument("--category", metavar="SLUG", default=DEFAULT_CATEGORY_SLUG,
                        help=(
                            f"Discussion category slug (default: '{DEFAULT_CATEGORY_SLUG}')."
                        ))
    pub_p.add_argument("--dry-run", action="store_true",
                        help=(
                            "Render the Discussion title and body to stdout without "
                            "posting to GitHub."
                        ))
    pub_p.add_argument("--template", metavar="PATH", default=None,
                        help=(
                            "Override the Discussion body template path "
                            f"(default: {_DEFAULT_TEMPLATE_PATH})."
                        ))
    pub_p.add_argument("--batch-id", metavar="N", default=None,
                        help=(
                            "Batch number (e.g. 27). Required if summary.json "
                            "does not carry a 'batch_id' field."
                        ))
    pub_p.add_argument("--topic", metavar="TOPIC", default=None,
                        help=(
                            "Short topic description. Required if summary.json "
                            "does not carry a 'topic' field."
                        ))
    pub_p.add_argument("--with-transcripts", action="store_true",
                        help=(
                            "Append a per-scenario folding markdown section "
                            "to the Discussion body (input + truncated reply "
                            "+ interpretation + verifier verdicts). "
                            "Reads scenarios/<id>/output.json from the run "
                            "directory."
                        ))
    pub_p.add_argument("--scenario-set", metavar="PATH", default=None,
                        help=(
                            "Path to the source scenario set YAML, used to "
                            "fill the per-scenario Input field when "
                            "--with-transcripts is set."
                        ))
    pub_p.set_defaults(func=run_publish)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print(
        "Usage: reyn dogfood <subcommand>  (run | coverage | report | compare | baseline)",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _dogfood_base_dir() -> Path:
    return Path.cwd() / ".reyn" / "dogfood"


def _runs_dir() -> Path:
    return _dogfood_base_dir() / "runs"


def _baselines_dir() -> Path:
    return _dogfood_base_dir() / "baselines"


def _resolve_run_dir(run_id_or_path: str) -> Path:
    """Resolve a run_id or path string to an absolute run directory."""
    candidate = Path(run_id_or_path)
    if candidate.exists() and candidate.is_dir():
        return candidate.resolve()
    # Treat as a run_id under the default runs directory
    run_dir = _runs_dir() / run_id_or_path
    if not run_dir.exists():
        print(
            f"Error: Run directory not found: {run_dir}\n"
            f"  Tried: {run_id_or_path} (as path) and {run_dir} (as run_id)",
            file=sys.stderr,
        )
        sys.exit(2)
    return run_dir


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------

def run_run(args: argparse.Namespace) -> None:
    """Execute a scenario set and record results."""
    set_yaml = Path(args.set_yaml)
    if not set_yaml.exists():
        print(f"Error: Scenario set not found: {set_yaml}", file=sys.stderr)
        sys.exit(2)

    replay_dir = Path(args.replay) if args.replay else None
    storage_dir = Path(args.storage) if args.storage else None

    try:
        from reyn.dogfood.scenarios import load_scenario_set  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: reyn.dogfood.scenarios is not available ({exc}).\n"
            "Ensure F1 (scenarios.py) is installed.",
            file=sys.stderr,
        )
        sys.exit(2)

    scenario_set = load_scenario_set(str(set_yaml))

    # Build the live-LLM runner_fn (injected seam for the real agent path)
    live_runner_fn = _build_live_runner(args.agent)

    try:
        from reyn.dogfood.runner import run_scenario_set
    except ImportError as exc:
        print(f"Error loading runner: {exc}", file=sys.stderr)
        sys.exit(2)

    print(f"dogfood run: {scenario_set.name}  ({len(scenario_set.scenarios)} scenarios, n={args.n})")
    if replay_dir:
        print(f"  replay mode: {replay_dir}")

    result = asyncio.run(
        run_scenario_set(
            scenario_set,
            run_id=getattr(args, "run_id", None),
            storage_dir=storage_dir,
            agent_name=args.agent,
            n=args.n,
            replay_fixture_dir=replay_dir,
            runner_fn=live_runner_fn if not replay_dir else None,
            with_interpretation=getattr(args, "with_interpretation", False),
            interpretation_model=getattr(args, "interpretation_model", None),
        )
    )

    agg = result.aggregate()
    run_dir = storage_dir or (_runs_dir() / result.run_id)

    print()
    print(f"  run_id     : {result.run_id}")
    print(f"  verified   : {agg['verified']}")
    print(f"  inconclusive: {agg['inconclusive']}")
    print(f"  refuted    : {agg['refuted']}")
    print(f"  blocked    : {agg['blocked']}")
    print(f"  total      : {agg['total']}")
    print(f"  verified % : {agg['verified_rate'] * 100:.1f}%")
    if agg.get("brier_score") is not None:
        print(f"  Brier      : {agg['brier_score']:.4f}")
    print()
    print(f"  results → {run_dir / 'summary.json'}")


def _build_live_runner(agent_name: str):
    """Return an async runner_fn that drives the chat router via Agent.run.

    This injects the real headless execution path.  For MVP, returns the
    stub (inconclusive) runner; the full integration follows the same pattern
    as reyn cron's _build_runner().
    """
    # MVP: return None so the runner uses its default stub.
    # Full integration: wire up the chat MessageBus / Agent.run path here,
    # capture events + artifacts from the session, and populate RunResult.
    return None


# ---------------------------------------------------------------------------
# Subcommand: coverage
# ---------------------------------------------------------------------------

def run_coverage(args: argparse.Namespace) -> None:
    """Show feature-map coverage across scenario sets."""
    try:
        from reyn.dogfood.coverage import compute_coverage  # type: ignore[import]
        from reyn.dogfood.scenarios import load_scenario_set  # type: ignore[import]
    except ImportError as exc:
        print(
            f"Error: coverage module is not available ({exc}).\n"
            "Ensure F4 (coverage.py) is installed.",
            file=sys.stderr,
        )
        sys.exit(2)

    yaml_paths = [Path(p) for p in args.set_yamls]
    if not yaml_paths:
        # Default: all YAML files under dogfood/scenarios/
        default_dir = Path("dogfood") / "scenarios"
        if default_dir.exists():
            yaml_paths = sorted(default_dir.glob("*.yaml"))
        else:
            print("No scenario YAML files specified and dogfood/scenarios/ not found.",
                  file=sys.stderr)
            sys.exit(2)

    sets = [load_scenario_set(str(p)) for p in yaml_paths]
    feature_map_path = args.feature_map

    try:
        matrix = compute_coverage(sets, feature_map_path)
    except Exception as exc:
        print(f"Error computing coverage: {exc}", file=sys.stderr)
        sys.exit(2)

    if args.output_json:
        print(json.dumps(matrix.__dict__, ensure_ascii=False, indent=2, default=str))
    else:
        _print_coverage(matrix)


def _print_coverage(matrix) -> None:  # pragma: no cover
    """Print coverage matrix to stdout (human-readable)."""
    print(f"Coverage: {matrix.covered_count}/{matrix.total_count} features covered")
    if matrix.uncovered:
        print("\nUncovered features:")
        for feat in matrix.uncovered:
            print(f"  - {feat}")
    else:
        print("All features covered!")


# ---------------------------------------------------------------------------
# Subcommand: report
# ---------------------------------------------------------------------------

def run_report(args: argparse.Namespace) -> None:
    """Print 4-band breakdown + Brier score from a stored run."""
    run_dir = _resolve_run_dir(args.run_id)

    try:
        from reyn.dogfood.runner import load_run_result_from_storage
    except ImportError as exc:
        print(f"Error loading runner: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        result = load_run_result_from_storage(run_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    agg = result.aggregate()

    if args.output_json:
        report_data = {
            "run_id": result.run_id,
            "set_name": result.set_name,
            "started_at": result.started_at.isoformat(),
            "completed_at": (
                result.completed_at.isoformat()
                if result.completed_at else None
            ),
            **agg,
        }
        print(json.dumps(report_data, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"Run: {result.run_id}")
        print(f"Set: {result.set_name}")
        print(f"Started: {result.started_at.isoformat()}")
        if result.completed_at:
            print(f"Completed: {result.completed_at.isoformat()}")
        print()
        print(f"  verified    : {agg['verified']}")
        print(f"  inconclusive: {agg['inconclusive']}")
        print(f"  refuted     : {agg['refuted']}")
        print(f"  blocked     : {agg['blocked']}")
        print(f"  total       : {agg['total']}")
        print(f"  verified %  : {agg['verified_rate'] * 100:.1f}%")
        if agg.get("brier_score") is not None:
            print(f"  Brier       : {agg['brier_score']:.4f}")

        # Per-scenario breakdown
        print()
        print("Scenarios:")
        for sr in result.scenario_results:
            marker = {
                "verified": "✓",
                "inconclusive": "?",
                "refuted": "✗",
                "blocked": "!",
            }.get(sr.overall_outcome, "?")
            print(f"  {marker} {sr.scenario_id:<40}  {sr.overall_outcome}")


# ---------------------------------------------------------------------------
# Subcommand: compare
# ---------------------------------------------------------------------------

def run_compare(args: argparse.Namespace) -> None:
    """Regression diff between a baseline and a candidate run."""
    baseline_dir = _resolve_run_dir(args.baseline_run_id)
    candidate_dir = _resolve_run_dir(args.candidate_run_id)

    try:
        from reyn.dogfood.compare import compare_runs
        from reyn.dogfood.runner import load_run_result_from_storage
    except ImportError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        baseline_result = load_run_result_from_storage(baseline_dir)
        candidate_result = load_run_result_from_storage(candidate_dir)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)

    report = compare_runs(baseline_result, candidate_result, threshold=args.threshold)

    if args.output_json:
        data = {
            "baseline_run_id": baseline_result.run_id,
            "candidate_run_id": candidate_result.run_id,
            "baseline_verified_rate": report.baseline_verified_rate,
            "candidate_verified_rate": report.candidate_verified_rate,
            "verified_rate_delta": report.verified_rate_delta,
            "threshold": args.threshold,
            "regression": report.exceeds_threshold(args.threshold),
            "regressed_scenarios": report.regressed_scenarios,
            "improved_scenarios": report.improved_scenarios,
            "deltas": [
                {
                    "scenario_id": d.scenario_id,
                    "baseline_outcome": d.baseline_outcome,
                    "candidate_outcome": d.candidate_outcome,
                    "regressed": d.regressed,
                    "improved": d.improved,
                }
                for d in report.deltas
            ],
        }
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        _print_compare_report(report, baseline_result, candidate_result, args.threshold)

    if report.exceeds_threshold(args.threshold):
        sys.exit(1)


def _print_compare_report(report, baseline, candidate, threshold: float) -> None:
    """Print human-readable compare report."""
    delta_pp = report.verified_rate_delta * 100
    delta_str = f"{delta_pp:+.1f}pp"
    regression = report.exceeds_threshold(threshold)

    print(f"  Baseline:  {baseline.run_id}  ({report.baseline_verified_rate * 100:.1f}% verified)")
    print(f"  Candidate: {candidate.run_id}  ({report.candidate_verified_rate * 100:.1f}% verified)")
    print(f"  Delta:     {delta_str}  /  threshold={-threshold * 100:.1f}pp")
    print(f"  Result:    {'REGRESSION ALERT' if regression else 'OK — no regression'}")

    if report.regressed_scenarios:
        print(f"\nRegressed scenarios ({len(report.regressed_scenarios)}):")
        for sid in report.regressed_scenarios:
            delta = next(d for d in report.deltas if d.scenario_id == sid)
            print(f"  - {sid}: {delta.baseline_outcome} → {delta.candidate_outcome}")

    if report.improved_scenarios:
        print(f"\nImproved scenarios ({len(report.improved_scenarios)}):")
        for sid in report.improved_scenarios:
            delta = next(d for d in report.deltas if d.scenario_id == sid)
            print(f"  + {sid}: {delta.baseline_outcome} → {delta.candidate_outcome}")


# ---------------------------------------------------------------------------
# Subcommand: baseline
# ---------------------------------------------------------------------------

def run_baseline(args: argparse.Namespace) -> None:
    """Symlink a run as a named baseline."""
    run_dir = _resolve_run_dir(args.run_id)
    label = args.label or args.run_id

    baselines_dir = _baselines_dir()
    baselines_dir.mkdir(parents=True, exist_ok=True)

    target = baselines_dir / label

    if target.exists() or target.is_symlink():
        print(f"Warning: Baseline '{label}' already exists; overwriting.", file=sys.stderr)
        target.unlink()

    # Create relative symlink for portability
    target.symlink_to(run_dir.resolve())
    print(f"Baseline '{label}' → {run_dir.resolve()}")
    print(f"  stored at: {target}")


# ---------------------------------------------------------------------------
# Subcommand: publish
# ---------------------------------------------------------------------------

def run_publish(args: argparse.Namespace) -> None:
    """Create a GitHub Discussion thread from a stored run's summary.json."""
    try:
        from reyn.dogfood.publish import (
            _DEFAULT_TEMPLATE_PATH,
            DEFAULT_CATEGORY_SLUG,
            DEFAULT_REPO,
            PublishConfig,
            detect_repo_from_git,
            get_token,
            publish_run,
        )
    except ImportError as exc:
        print(f"Error loading publish module: {exc}", file=sys.stderr)
        sys.exit(2)

    run_dir = _resolve_run_dir(args.run_id)

    # Resolve --repo: explicit flag → git remote → hardcoded default
    repo = args.repo
    if not repo:
        repo = detect_repo_from_git()
    if not repo:
        repo = DEFAULT_REPO

    template_path = Path(args.template) if args.template else _DEFAULT_TEMPLATE_PATH
    if not template_path.exists():
        print(
            f"Error: Discussion template not found: {template_path}\n"
            "Pass --template <path> to point at a custom template.",
            file=sys.stderr,
        )
        sys.exit(2)

    token = get_token()
    if not token and not args.dry_run:
        print(
            "Error: No GitHub token found. Set GH_TOKEN or GITHUB_TOKEN and retry.",
            file=sys.stderr,
        )
        sys.exit(2)

    config = PublishConfig(
        repo=repo,
        category_slug=args.category,
        template_path=template_path,
        token=token,
    )

    scenario_set_path = (
        Path(args.scenario_set)
        if getattr(args, "scenario_set", None)
        else None
    )

    try:
        result = publish_run(
            args.run_id,
            config=config,
            storage_dir=run_dir,
            dry_run=args.dry_run,
            batch_id=args.batch_id,
            topic=args.topic,
            with_transcripts=getattr(args, "with_transcripts", False),
            scenario_set_path=scenario_set_path,
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"[dry-run] Title: {result['title']}")
        print()
        print("[dry-run] Body:")
        print(result["body"])
    else:
        print(f"Discussion created: {result['discussion_url']}")
        print(f"  Title  : {result['title']}")
        print(f"  Number : #{result['discussion_number']}")
