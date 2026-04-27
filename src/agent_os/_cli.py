import argparse
import importlib
import json
import sys


def _parse_cli_input(raw: str) -> dict:
    """Accept JSON or natural language. Natural language → user_message artifact."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}


def cmd_run(args: argparse.Namespace) -> None:
    if not args.app and not args.app_dsl:
        print("Error: one of --app or --app-dsl is required.", file=sys.stderr)
        sys.exit(1)

    if args.app_dsl:
        try:
            from agent_os.compiler import load_dsl_app
            app = load_dsl_app(args.app_dsl, dsl_root=args.dsl_root)
        except Exception as e:
            print(f"Error: failed to compile DSL '{args.app_dsl}': {e}", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            module = importlib.import_module(args.app)
        except ModuleNotFoundError as e:
            print(f"Error: cannot import app '{args.app}': {e}", file=sys.stderr)
            sys.exit(1)
        if not hasattr(module, "app"):
            print(f"Error: module '{args.app}' has no 'app' attribute.", file=sys.stderr)
            sys.exit(1)
        app = module.app

    initial_input = _parse_cli_input(args.input)

    from agent_os.agent import Agent
    if args.rich:
        from agent_os.reporters.rich import RichLogger
        logger = RichLogger()
    else:
        from agent_os.reporters.console import ConsoleLogger
        logger = ConsoleLogger()

    agent = Agent(
        model=args.model,
        workspace_dir=args.workspace,
        strict=args.strict,
        subscribers=[logger],
        extra_read_roots=args.read_allow,
        shell_allowed=args.allow_shell,
    )

    input_type = initial_input.get("type", "unknown")
    print(f"app             : {app.name}")
    print(f"model           : {args.model}")
    print(f"output_language : {args.output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = agent.run(app, initial_input, output_language=args.output_language)
    except Exception as e:
        print(f"\nError during execution: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    if not result.ok:
        print(f"=== Warning: workflow ended with status '{result.status}' ===",
              file=sys.stderr)
    print("=== Final Output ===")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))
    if result.token_usage:
        u = result.token_usage
        print(f"\ntokens: {u.prompt_tokens:,} prompt + {u.completion_tokens:,} completion"
              f" = {u.total_tokens:,} total")
    print(f"\nevents saved → {agent.events_path}")

    if args.events:
        print()
        _print_events(agent)

    if not result.ok:
        sys.exit(2)


def cmd_events(args: argparse.Namespace) -> None:
    import json
    from pathlib import Path
    from agent_os.models import Event

    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    conversation: bool = args.conversation
    if args.rich:
        from agent_os.reporters.rich import RichLogger
        logger = RichLogger(conversation=conversation)
    else:
        from agent_os.reporters.console import ConsoleLogger
        logger = ConsoleLogger(conversation=conversation)

    filter_types: set[str] = set(args.filter_types)
    skip_types: set[str] = set(args.skip_types)
    if conversation:
        # Show only LLM conversation events; ignore explicit filter/skip when --conversation given
        filter_types = {"context_built", "llm_response_received"}

    count = 0
    with path.open(encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as e:
                print(f"[line {lineno}] JSON parse error: {e}", file=sys.stderr)
                continue

            event_type = raw.get("type", "")
            if filter_types and event_type not in filter_types:
                continue
            if event_type in skip_types:
                continue

            try:
                event = Event.model_validate(raw)
            except Exception as e:
                print(f"[line {lineno}] Event parse error: {e}", file=sys.stderr)
                continue

            logger(event)
            count += 1

    print(f"\n({count} events replayed from {path})")


def _score_bar(score: float, width: int = 20) -> str:
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def _schema_line(s, indent: str = "    ") -> None:
    mark = "✓" if s.passed else "✗"
    print(f"{indent}[S] {mark}  {s.assertion.raw}")
    if not s.passed:
        print(f"{indent}         → {s.reason}")


def _criterion_line(c, indent: str = "    ") -> None:
    mark = "✓" if c.passed else "✗"
    badge = "[A]" if getattr(c, "tag", "required") == "aspirational" else "[Q]"
    print(f"{indent}{badge} {mark} {c.score:.2f}  {c.criterion}")
    if not c.passed or c.score < 0.85:
        print(f"{indent}           → {c.reason}")


def _cross_phase_line(r, indent: str = "    ") -> None:
    mark = "✓" if r.passed else "✗"
    print(f"{indent}[X] {mark}  {r.assertion.raw}")
    if not r.passed:
        print(f"{indent}         → {r.reason}")


def _print_phase_result(pr, indent: str = "  ") -> None:
    label = f"phase: {pr.phase}" if pr.phase != "final" else "final"
    visit_str = f"  [visit×{pr.visit}]" if pr.visit > 1 else ""
    score_bar = _score_bar(pr.score, width=12)
    print(f"{indent}┌─ {label}{visit_str}  {score_bar} {pr.score:.2f} ({pr.passed}/{pr.total})")
    for s in pr.schema_results:
        _schema_line(s, indent=indent + "│  ")
    for c in pr.criteria:
        _criterion_line(c, indent=indent + "│  ")
    print(f"{indent}└{'─' * 50}")


def _print_case_result(cr) -> None:
    status_sym = "✓" if cr.run_status == "finished" else "⚠" if cr.run_status == "loop_limit_exceeded" else "✗"
    print(f"\n  run: {status_sym} {cr.run_status}")
    if cr.error:
        print(f"  error: {cr.error}")
        return
    for pr in cr.phase_results:
        _print_phase_result(pr)
    if cr.final_result:
        _print_phase_result(cr.final_result)
    if cr.cross_phase_results:
        print(f"  ┌─ cross-phase  {'─' * 40}")
        for r in cr.cross_phase_results:
            _cross_phase_line(r, indent="  │  ")
        print(f"  └{'─' * 50}")
    bar = _score_bar(cr.score)
    asp = cr.aspirational_total
    asp_str = f"  aspirational: {cr.aspirational_passed}/{asp}" if asp > 0 else ""
    print(f"\n  case score: {bar} {cr.score:.2f}  ({cr.passed}/{cr.total} required){asp_str}")


def _print_repeat_summary(case_name: str, results: list) -> None:
    """Print aggregated stats across N repeated runs of the same case."""
    import statistics as _stats

    n = len(results)
    print(f"\n  ━━━ {case_name} [{n} runs summary] ━━━")

    first = results[0]
    phase_names = [pr.phase for pr in first.all_phase_results]

    for pname in phase_names:
        phase_runs = [
            next((pr for pr in r.all_phase_results if pr.phase == pname), None)
            for r in results
        ]
        phase_runs = [pr for pr in phase_runs if pr is not None]
        if not phase_runs:
            continue

        label = "final" if pname == "final" else f"phase: {pname}"
        print(f"    ┌─ {label}")

        ref = phase_runs[0]

        for s_idx, sr in enumerate(ref.schema_results):
            marks = "".join(
                ("✓" if pr.schema_results[s_idx].passed else "✗")
                for pr in phase_runs
                if s_idx < len(pr.schema_results)
            )
            print(f"    │  [S] {marks}  {sr.assertion.raw}")

        for c_idx, cr0 in enumerate(ref.criteria):
            scores = [
                pr.criteria[c_idx].score
                for pr in phase_runs
                if c_idx < len(pr.criteria)
            ]
            if not scores:
                continue
            mean = _stats.mean(scores)
            std = _stats.stdev(scores) if len(scores) > 1 else 0.0
            badge = "[A]" if cr0.tag == "aspirational" else "[Q]"
            if std >= 0.2:
                stability = "  ⚠ unstable"
            elif mean < 0.7:
                stability = "  ⛔ stable-low"
            else:
                stability = ""
            score_str = " / ".join(f"{s:.2f}" for s in scores)
            print(f"    │  {badge} {mean:.2f}±{std:.2f}{stability}  {cr0.criterion[:60]}")
            if len(scores) > 1:
                print(f"    │       → {score_str}")

        print(f"    └{'─' * 50}")

    if first.cross_phase_results:
        print(f"    ┌─ cross-phase")
        for x_idx, xr0 in enumerate(first.cross_phase_results):
            marks = "".join(
                ("✓" if r.cross_phase_results[x_idx].passed else "✗")
                for r in results
                if x_idx < len(r.cross_phase_results)
            )
            print(f"    │  [X] {marks}  {xr0.assertion.raw}")
        print(f"    └{'─' * 50}")

    required_scores = []
    for r in results:
        items = [
            x for pr in r.all_phase_results
            for x in ([s.score for s in pr.schema_results]
                      + [c.score for c in pr.criteria if c.tag == "required"])
        ] + [xr.score for xr in r.cross_phase_results]
        if items:
            required_scores.append(sum(items) / len(items))

    if required_scores:
        mean_s = _stats.mean(required_scores)
        std_s = _stats.stdev(required_scores) if len(required_scores) > 1 else 0.0
        print(f"\n    mean score (required): {_score_bar(mean_s)} {mean_s:.2f} ± {std_s:.2f}")


def _print_cost_summary(cs) -> None:
    if cs is None:
        return
    a, j, t = cs.app_tokens, cs.judge_tokens, cs.total_tokens
    print(f" {'─' * 53}")
    print(f" Tokens  app  : {a.prompt_tokens:>8,} prompt + {a.completion_tokens:>7,} completion"
          f" = {a.total_tokens:>8,}")
    print(f"         judge: {j.prompt_tokens:>8,} prompt + {j.completion_tokens:>7,} completion"
          f" = {j.total_tokens:>8,}")
    print(f"         total: {t.prompt_tokens:>8,} prompt + {t.completion_tokens:>7,} completion"
          f" = {t.total_tokens:>8,}")
    if cs.estimated_cost_usd is not None:
        snap = cs.pricing_snapshot or {}
        ver = snap.get("litellm_version", "?")
        print(f" Est. cost: ${cs.estimated_cost_usd:.4f}  (litellm {ver} pricing)")
    else:
        print(" Est. cost: unknown model — not in litellm pricing DB")


def _run_eval(runner, spec, repeat: int) -> dict[str, list]:
    """Run all eval cases across all repeats. Returns runs_per_case[name] = list of CaseResult."""
    runs_per_case: dict[str, list] = {case.name: [] for case in spec.cases}
    for run_idx in range(repeat):
        if repeat > 1:
            print(f"\n{'─' * 55}")
            print(f" Run {run_idx + 1}/{repeat}")
            print(f"{'─' * 55}")
        for case in spec.cases:
            print(f"━━━ case: {case.name} ━━━")
            print(f"  input: {case.input[:120]}")
            cr = runner.run_case(case)
            runs_per_case[case.name].append(cr)
            _print_case_result(cr)
    return runs_per_case


def cmd_eval(args: argparse.Namespace) -> None:
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from agent_os.compiler.eval_loader import load_eval_spec
    from agent_os.compiler import load_dsl_app
    from agent_os.eval.runner import EvalRunner
    from agent_os.eval.models import EvalRunResult

    try:
        spec = load_eval_spec(args.spec)
    except Exception as e:
        print(f"Error loading eval spec: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        app = load_dsl_app(spec.app_dsl_path, dsl_root=spec.dsl_root or args.dsl_root)
    except Exception as e:
        print(f"Error loading app '{spec.app_dsl_path}': {e}", file=sys.stderr)
        sys.exit(1)

    model = args.model or spec.model or "gpt-4o"
    judge_model = args.judge_model or spec.judge_model or model
    repeat = max(1, args.repeat)

    if args.verbose:
        if args.rich:
            from agent_os.reporters.rich import RichLogger
            app_logger = RichLogger()
        else:
            from agent_os.reporters.console import ConsoleLogger
            app_logger = ConsoleLogger()
        app_subscribers = [app_logger]
    else:
        app_subscribers = []

    repeat_label = f"  ×{repeat}" if repeat > 1 else ""
    print(f"=== Eval: {app.name}  [{len(spec.cases)} case(s){repeat_label}] ===")
    print(f"    model={model}  judge={judge_model}")
    print()

    runner = EvalRunner(
        spec=spec, app=app, model=model, judge_model=judge_model,
        workspace_dir=args.workspace, output_language=args.output_language,
        app_subscribers=app_subscribers, extra_read_roots=args.read_allow,
    )

    runs_per_case = _run_eval(runner, spec, repeat)

    if repeat > 1:
        print(f"\n{'═' * 55}  REPEAT SUMMARY  {'═' * 55}")
        for case in spec.cases:
            _print_repeat_summary(case.name, runs_per_case[case.name])

    case_results = [runs_per_case[case.name][-1] for case in spec.cases]
    cost_summary = runner.build_cost_summary()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_result = EvalRunResult(
        spec_path=args.spec, app_name=app.name, model=model, judge_model=judge_model,
        timestamp=ts, case_results=case_results, cost_summary=cost_summary,
    )

    bar = _score_bar(run_result.overall_score)
    weak = run_result.weakest_phase()
    print(f"\n{'═' * 55}")
    print(f" Overall: {bar} {run_result.overall_score:.2f}"
          f"  ({run_result.overall_passed}/{run_result.overall_total})")
    if weak:
        print(f" Weakest phase: {weak}")
    _print_cost_summary(cost_summary)

    eval_dir = Path(args.workspace) / "evals"
    eval_dir.mkdir(parents=True, exist_ok=True)
    result_path = eval_dir / f"{ts}_{app.name}.json"
    result_path.write_text(
        json.dumps(run_result.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f" Results → {result_path}")
    print(f"{'═' * 55}")

    if run_result.overall_score < 0.6:
        sys.exit(2)


def cmd_eval_compare(args: argparse.Namespace) -> None:
    import json
    from pathlib import Path

    def load_result(p: str) -> dict:
        path = Path(p)
        if not path.exists():
            print(f"Error: file not found: {path}", file=sys.stderr)
            sys.exit(1)
        return json.loads(path.read_text(encoding="utf-8"))

    a = load_result(args.baseline)
    b = load_result(args.candidate)

    print(f"=== Eval Compare ===")
    print(f"  baseline:  {args.baseline}  ({a['timestamp']}  model={a['model']})")
    print(f"  candidate: {args.candidate}  ({b['timestamp']}  model={b['model']})")
    print()

    # Index b cases by name
    b_cases = {c["name"]: c for c in b.get("cases", [])}

    any_regression = False
    for a_case in a.get("cases", []):
        name = a_case["name"]
        b_case = b_cases.get(name)
        if not b_case:
            print(f"  case '{name}': not found in candidate — skipped")
            continue

        print(f"  ━━━ case: {name} ━━━")
        # Index b phases by name
        b_phases = {p["phase"]: p for p in b_case.get("phases", [])}

        for a_phase in a_case.get("phases", []):
            pname = a_phase["phase"]
            b_phase = b_phases.get(pname)
            if not b_phase:
                print(f"    phase {pname}: not in candidate")
                continue

            print(f"    phase: {pname}")
            # Schema (deterministic) — track pass/fail changes
            b_schema = {s["path"]: s for s in b_phase.get("schema", [])}
            for a_s in a_phase.get("schema", []):
                path = a_s["path"]
                b_s = b_schema.get(path)
                if not b_s:
                    continue
                if a_s["passed"] != b_s["passed"]:
                    trend = "↑" if b_s["passed"] else "↓"
                    if not b_s["passed"]:
                        any_regression = True
                    print(f"      [S] {trend}  {'pass' if a_s['passed'] else 'fail'} → {'pass' if b_s['passed'] else 'fail'}  {path}")

            # Quality (LLM-judged) — track score changes
            b_crit = {c["criterion"]: c for c in b_phase.get("criteria", [])}
            for a_c in a_phase.get("criteria", []):
                crit = a_c["criterion"]
                b_c = b_crit.get(crit)
                if not b_c:
                    continue
                delta = b_c["score"] - a_c["score"]
                arrow = f"+{delta:+.2f}" if delta >= 0 else f"{delta:.2f}"
                trend = "↑" if delta > 0.05 else "↓" if delta < -0.05 else "→"
                if delta < -0.1:
                    any_regression = True
                print(f"      [Q] {trend} {a_c['score']:.2f} → {b_c['score']:.2f} ({arrow})  {crit[:60]}")

        # Cross-phase — track pass/fail changes
        b_cross = {r["raw"]: r for r in b_case.get("cross_phase", [])}
        for a_r in a_case.get("cross_phase", []):
            raw = a_r["raw"]
            b_r = b_cross.get(raw)
            if not b_r:
                continue
            if a_r["passed"] != b_r["passed"]:
                trend = "↑" if b_r["passed"] else "↓"
                if not b_r["passed"]:
                    any_regression = True
                print(f"      [X] {trend}  {'pass' if a_r['passed'] else 'fail'} → {'pass' if b_r['passed'] else 'fail'}  {raw}")

    delta_overall = b.get("overall_score", 0) - a.get("overall_score", 0)
    arrow = f"+{delta_overall:+.2f}" if delta_overall >= 0 else f"{delta_overall:.2f}"
    print(f"\n  Overall: {a['overall_score']:.2f} → {b['overall_score']:.2f}  ({arrow})")

    if any_regression:
        print("  ⚠ Regressions detected (>0.10 drop)")
        sys.exit(2)


def cmd_lint(args: argparse.Namespace) -> None:
    from pathlib import Path
    from agent_os.compiler.linter import lint_dsl

    dsl_root = Path(args.dsl)
    issues = lint_dsl(dsl_root)

    if not issues:
        print("No issues found.")
        return

    errors = [i for i in issues if i.severity == "error"]
    warnings = [i for i in issues if i.severity == "warning"]

    for issue in issues:
        print(issue)

    print()
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")

    if errors:
        sys.exit(1)


def cmd_format(args: argparse.Namespace) -> None:
    from pathlib import Path
    from agent_os.compiler.formatter import format_dsl

    dsl_root = Path(args.dsl)
    check_only = args.check

    changed = format_dsl(dsl_root, write=not check_only)

    if not changed:
        print("All files are already formatted.")
        return

    verb = "Would reformat" if check_only else "Reformatted"
    for p in changed:
        print(f"{verb}: {p}")

    if check_only:
        print(f"\n{len(changed)} file(s) would be reformatted.")
        sys.exit(1)
    else:
        print(f"\n{len(changed)} file(s) reformatted.")


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent_os",
        description="Agent OS MVP — LLM-driven phase execution",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    run_p = sub.add_parser("run", help="Run an app")
    run_p.add_argument(
        "--app",
        default=None,
        metavar="MODULE",
        help="Python module path exposing an 'app' object (e.g. examples.writing_app.app)",
    )
    run_p.add_argument(
        "--app-dsl",
        default=None,
        dest="app_dsl",
        metavar="PATH",
        help="Path to a Markdown App DSL file (e.g. dsl/apps/writing_review_app.md)",
    )
    run_p.add_argument(
        "--dsl-root",
        default=None,
        dest="dsl_root",
        metavar="DIR",
        help=(
            "Root of the DSL tree for shared artifact/phase resolution "
            "(default: auto-detected as <app_dir>/../..). "
            "Use this when running an app from outside the project dsl/ tree, "
            "e.g. --app-dsl workspace/dsl/apps/my_app/app.md --dsl-root dsl/"
        ),
    )
    run_p.add_argument(
        "--input",
        required=True,
        metavar="TEXT",
        help=(
            "Initial input: JSON artifact string, or natural language "
            "(auto-wrapped as user_message artifact)"
        ),
    )
    run_p.add_argument(
        "--model",
        default="gpt-4o",
        metavar="MODEL",
        help="LiteLLM model name (default: gpt-4o)",
    )
    run_p.add_argument(
        "--workspace",
        default="./workspace",
        metavar="DIR",
        help="Workspace directory (default: ./workspace)",
    )
    run_p.add_argument(
        "--output-language",
        default="ja",
        dest="output_language",
        metavar="LANG",
        help="Output language code (default: ja)",
    )
    run_p.add_argument(
        "--events",
        action="store_true",
        help="Print the full event log after execution",
    )
    run_p.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Enable strict schema validation: enforce required fields at every nesting depth. "
            "Default (lenient) only enforces required at the top level of each artifact."
        ),
    )
    run_p.add_argument(
        "--rich",
        action="store_true",
        help="Use Rich-styled console output instead of plain text logging.",
    )
    run_p.add_argument(
        "--read-allow",
        dest="read_allow",
        metavar="DIR",
        action="append",
        default=[],
        help=(
            "Allow reading files from DIR (absolute path). "
            "Can be specified multiple times. "
            "Writes always remain restricted to the workspace."
        ),
    )
    run_p.add_argument(
        "--allow-shell",
        dest="allow_shell",
        action="store_true",
        help=(
            "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
            "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
            "Off by default for safety."
        ),
    )
    run_p.set_defaults(func=cmd_run)

    # ── eval ──────────────────────────────────────────────────────────────────
    eval_p = sub.add_parser("eval", help="Run an eval spec against an app")
    eval_p.add_argument("--spec", required=True, metavar="FILE",
                        help="Path to the eval.md spec file")
    eval_p.add_argument("--model", default=None, metavar="MODEL",
                        help="Override model for running the app (default: from spec or gpt-4o)")
    eval_p.add_argument("--judge-model", dest="judge_model", default=None, metavar="MODEL",
                        help="Override model for LLM-as-judge (default: same as --model)")
    eval_p.add_argument("--dsl-root", dest="dsl_root", default=None, metavar="DIR",
                        help="DSL root override (default: from spec frontmatter)")
    eval_p.add_argument("--workspace", default="./workspace", metavar="DIR",
                        help="Workspace directory (default: ./workspace)")
    eval_p.add_argument("--output-language", default="ja", dest="output_language", metavar="LANG")
    eval_p.add_argument(
        "--read-allow",
        dest="read_allow",
        action="append",
        default=[],
        metavar="DIR",
        help="Allow reading files from DIR (absolute path). Can be specified multiple times.",
    )
    eval_p.add_argument(
        "--repeat", type=int, default=1, metavar="N",
        help=(
            "Run each case N times and show per-criterion mean/std to distinguish "
            "capability limits (stable-low) from fixable bugs (unstable). Default: 1"
        ),
    )
    eval_p.add_argument("--verbose", action="store_true",
                        help="Show per-event app run log during execution")
    eval_p.add_argument("--rich", action="store_true",
                        help="Use Rich-styled output (applies to --verbose app log)")
    eval_p.set_defaults(func=cmd_eval)

    eval_cmp_p = sub.add_parser("eval-compare",
                                help="Compare two eval result JSON files (regression check)")
    eval_cmp_p.add_argument("baseline", metavar="BASELINE.json",
                            help="Baseline eval result JSON")
    eval_cmp_p.add_argument("candidate", metavar="CANDIDATE.json",
                            help="Candidate eval result JSON to compare against baseline")
    eval_cmp_p.set_defaults(func=cmd_eval_compare)

    lint_p = sub.add_parser("lint", help="Lint DSL files for issues")
    lint_p.add_argument(
        "--dsl",
        required=True,
        metavar="DIR",
        help="Root directory of the DSL tree (e.g. dsl/)",
    )
    lint_p.set_defaults(func=cmd_lint)

    fmt_p = sub.add_parser("format", help="Format DSL files into canonical form")
    fmt_p.add_argument(
        "--dsl",
        required=True,
        metavar="DIR",
        help="Root directory of the DSL tree (e.g. dsl/)",
    )
    fmt_p.add_argument(
        "--check",
        action="store_true",
        help="Dry-run: report files that would change without writing them",
    )
    fmt_p.set_defaults(func=cmd_format)

    ev_p = sub.add_parser("events", help="Replay a saved event JSONL file to the console")
    ev_p.add_argument(
        "path",
        metavar="FILE",
        help="Path to the .jsonl event file (e.g. workspace/runs/20260426T…_app_builder.jsonl)",
    )
    ev_p.add_argument(
        "--rich",
        action="store_true",
        help="Use Rich-styled output instead of plain text",
    )
    ev_p.add_argument(
        "--filter",
        metavar="TYPE",
        action="append",
        dest="filter_types",
        default=[],
        help="Only show events of this type (repeatable, e.g. --filter phase_started --filter phase_completed)",
    )
    ev_p.add_argument(
        "--skip",
        metavar="TYPE",
        action="append",
        dest="skip_types",
        default=[],
        help="Skip events of this type (repeatable)",
    )
    ev_p.add_argument(
        "--conversation",
        action="store_true",
        help=(
            "Show LLM conversation history: display context frames sent to the LLM "
            "and the raw responses received, in order. "
            "Overrides --filter and --skip."
        ),
    )
    ev_p.set_defaults(func=cmd_events)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
