import argparse
import importlib
import json
import sys
from pathlib import Path


def _load_config():
    from reyn.config import load_config
    return load_config()


def _apply_config_env(config) -> None:
    """Set env vars from config so all litellm calls pick them up automatically.
    API keys are intentionally excluded — set OPENAI_API_KEY / ANTHROPIC_API_KEY etc. in your shell.
    """
    import os
    if config.api_base:
        os.environ.setdefault("LITELLM_API_BASE", config.api_base)


def _make_resolver(config):
    from reyn.model_resolver import ModelResolver
    return ModelResolver(config.models)


def _parse_cli_input(raw: str) -> dict:
    """Accept JSON or natural language. Natural language → user_message artifact."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}


def _resolve_app_name(name: str) -> tuple[Path, Path | None]:
    """Resolve a short app name to (app_dir, dsl_root).

    Search order: reyn/project/ → reyn/local/ → stdlib.
    Returns (app_dir, dsl_root) where dsl_root is None when it cannot be inferred.
    Exits with an error message if not found.
    """
    stdlib_root = Path(__file__).parent.parent / "stdlib"
    candidates: list[tuple[Path, Path]] = [
        (Path("reyn") / "project" / name, Path("reyn")),
        (Path("reyn") / "local" / name,   Path("reyn")),
        (stdlib_root / "apps" / name,      stdlib_root),
    ]
    for app_dir, dsl_root in candidates:
        if (app_dir / "app.md").exists():
            return app_dir, dsl_root
    checked = "\n  ".join(str(d / "app.md") for d, _ in candidates)
    print(f"Error: app '{name}' not found. Looked in:\n  {checked}", file=sys.stderr)
    sys.exit(1)


def cmd_run(args: argparse.Namespace) -> None:
    config = _load_config()
    _apply_config_env(config)
    resolver = _make_resolver(config)

    if args.app_path:
        app_dir = Path(args.app_path)
        app_md = app_dir / "app.md"
        dsl_root = args.dsl_root
        try:
            from reyn.compiler import load_dsl_app
            app = load_dsl_app(str(app_md), dsl_root=dsl_root)
        except Exception as e:
            print(f"Error: failed to compile DSL '{app_md}': {e}", file=sys.stderr)
            sys.exit(1)
    elif args.app_name:
        app_dir, inferred_root = _resolve_app_name(args.app_name)
        dsl_root = args.dsl_root or str(inferred_root)
        app_md = app_dir / "app.md"
        print(f"resolved        : {app_md}  (dsl-root: {dsl_root})")
        try:
            from reyn.compiler import load_dsl_app
            app = load_dsl_app(str(app_md), dsl_root=dsl_root)
        except Exception as e:
            print(f"Error: failed to compile DSL '{app_md}': {e}", file=sys.stderr)
            sys.exit(1)
    elif args.module:
        try:
            module = importlib.import_module(args.module)
        except ModuleNotFoundError as e:
            print(f"Error: cannot import module '{args.module}': {e}", file=sys.stderr)
            sys.exit(1)
        if not hasattr(module, "app"):
            print(f"Error: module '{args.module}' has no 'app' attribute.", file=sys.stderr)
            sys.exit(1)
        app = module.app
    else:
        print("Error: provide an app name (positional), --app-path DIR, or --module.", file=sys.stderr)
        sys.exit(1)

    if args.input is not None:
        raw_input = args.input
    elif not sys.stdin.isatty():
        raw_input = sys.stdin.read().strip()
    else:
        print("Error: provide INPUT argument or pipe input via stdin.", file=sys.stderr)
        sys.exit(1)

    initial_input = _parse_cli_input(raw_input)

    model = args.model or config.model
    output_language = args.output_language or config.output_language
    shell_allowed = args.allow_shell or config.shell_allowed
    max_phase_visits = args.max_phase_visits if args.max_phase_visits is not None else config.max_phase_visits

    resolved_model = resolver.resolve(model)

    from reyn.agent import Agent
    from reyn.permissions import PermissionResolver
    from reyn.config import _find_project_root
    project_root = _find_project_root(Path.cwd())
    perm_config = getattr(config, "permissions", {}) or {}
    # backward compat: if global shell_allowed, pre-approve shell in permissions
    if shell_allowed and "shell" not in perm_config:
        perm_config = dict(perm_config, shell="allow")
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
    )

    if args.rich:
        from reyn.reporters.rich import RichLogger
        logger = RichLogger()
    else:
        from reyn.reporters.console import ConsoleLogger
        logger = ConsoleLogger()

    agent = Agent(
        model=model,
        state_dir=config.state_dir,
        strict=args.strict,
        subscribers=[logger],
        shell_allowed=shell_allowed,
        resolver=resolver,
        permission_resolver=perm_resolver,
        max_phase_visits=max_phase_visits,
    )

    input_type = initial_input.get("type", "unknown")
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model
    print(f"app             : {app.name}")
    print(f"model           : {model_display}")
    print(f"output_language : {output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = agent.run(app, initial_input, output_language=output_language)
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


def cmd_apps(args: argparse.Namespace) -> None:
    import yaml
    from .compiler.parser import _split_frontmatter

    stdlib_root = Path(__file__).parent.parent / "stdlib"

    search_roots: list[tuple[str, Path]] = [
        ("project", Path("reyn") / "project"),
        ("local",   Path("reyn") / "local"),
        ("stdlib",  stdlib_root / "apps"),
    ]

    def _read_app_md(app_md: Path) -> tuple[dict, str]:
        try:
            fm, body = _split_frontmatter(app_md.read_text(encoding="utf-8"))
            return fm, body
        except Exception:
            return {}, ""

    def _find_app(name: str) -> Path | None:
        for _, apps_dir in search_roots:
            candidate = apps_dir / name / "app.md"
            if candidate.exists():
                return candidate
        return None

    # Detail view: reyn apps <name>
    if getattr(args, "app_name", None):
        app_md = _find_app(args.app_name)
        if app_md is None:
            print(f"App '{args.app_name}' not found.")
            return
        fm, body = _read_app_md(app_md)
        print(f"\n{fm.get('name', args.app_name)}")
        if fm.get("description"):
            print(f"{fm['description']}\n")
        if body.strip():
            print(body.strip())
        else:
            print("(no documentation)")
        print()
        return

    # List view: reyn apps
    found_any = False
    seen: set[str] = set()

    for label, apps_dir in search_roots:
        if not apps_dir.exists():
            continue
        entries = sorted(p for p in apps_dir.iterdir() if p.is_dir() and (p / "app.md").exists())
        if not entries:
            continue
        print(f"\n{label}  ({apps_dir})")
        for app_dir in entries:
            name = app_dir.name
            fm, _ = _read_app_md(app_dir / "app.md")
            desc = (fm.get("description") or "").strip().splitlines()[0] if fm.get("description") else ""
            shadowed = " [shadowed]" if name in seen else ""
            desc_str = f"  — {desc}" if desc else ""
            print(f"  {name}{desc_str}{shadowed}")
            seen.add(name)
        found_any = True

    if not found_any:
        print("No apps found.")
    print()
    print("Run 'reyn apps <name>' for usage details.")


def cmd_events(args: argparse.Namespace) -> None:
    import json
    from pathlib import Path
    from reyn.models import Event

    path = Path(args.path)
    if not path.exists():
        print(f"Error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    conversation: bool = args.conversation
    if args.rich:
        from reyn.reporters.rich import RichLogger
        logger = RichLogger(conversation=conversation)
    else:
        from reyn.reporters.console import ConsoleLogger
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
    from reyn.compiler.eval_loader import load_eval_spec
    from reyn.compiler import load_dsl_app
    from reyn.eval.runner import EvalRunner
    from reyn.eval.models import EvalRunResult

    config = _load_config()
    _apply_config_env(config)
    resolver = _make_resolver(config)

    try:
        spec = load_eval_spec(args.spec)
    except Exception as e:
        print(f"Error loading eval spec: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve app: bare name → search path; path/URL → use directly
    app_ref = spec.app_dsl_path
    if "/" not in app_ref and not app_ref.endswith(".md"):
        app_dir, inferred_root = _resolve_app_name(app_ref)
        dsl_root = args.dsl_root or str(inferred_root)
        app_md = str(app_dir / "app.md")
        print(f"resolved        : {app_md}  (dsl-root: {dsl_root})")
    else:
        app_md = app_ref
        dsl_root = spec.dsl_root or args.dsl_root

    try:
        app = load_dsl_app(app_md, dsl_root=dsl_root)
    except Exception as e:
        print(f"Error loading app '{app_md}': {e}", file=sys.stderr)
        sys.exit(1)

    raw_model = args.model or spec.model or config.model
    raw_judge = args.judge_model or spec.judge_model or raw_model
    model = resolver.resolve(raw_model)
    judge_model = resolver.resolve(raw_judge)
    repeat = max(1, args.repeat)

    if args.verbose:
        if args.rich:
            from reyn.reporters.rich import RichLogger
            app_logger = RichLogger()
        else:
            from reyn.reporters.console import ConsoleLogger
            app_logger = ConsoleLogger()
        app_subscribers = [app_logger]
    else:
        app_subscribers = []

    repeat_label = f"  ×{repeat}" if repeat > 1 else ""
    model_display = f"{raw_model} → {model}" if model != raw_model else model
    judge_display = f"{raw_judge} → {judge_model}" if judge_model != raw_judge else judge_model
    print(f"=== Eval: {app.name}  [{len(spec.cases)} case(s){repeat_label}] ===")
    print(f"    model={model_display}  judge={judge_display}")
    print()

    runner = EvalRunner(
        spec=spec, app=app, model=model, judge_model=judge_model,
        state_dir=str(Path(config.state_dir) / "eval_runs"),
        output_language=args.output_language,
        app_subscribers=app_subscribers,
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

    eval_dir = Path(config.state_dir) / "evals"
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
    from reyn.compiler.linter import lint_app_dir

    app_dir, _ = _resolve_app_name(args.app)
    issues = lint_app_dir(app_dir)

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
    from reyn.compiler.formatter import format_dsl

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


_REYN_YAML_TEMPLATE = """\
# Reyn project configuration — commit this file.
# Local overrides belong in .reyn/config.yaml (gitignored) — never commit secrets here.

# Default model class when --model is not specified.
model: standard

# Model class → LiteLLM model string.
# Three standard tiers. Edit to match your provider.
models:
  light:    openai/gpt-4o-mini
  standard: openai/gpt-4o
  strong:   openai/gpt-4o

# output_language: en          # en | ja | zh | ...
# shell_allowed: false         # allow 'shell' Control IR op (meta-apps only)
"""

_REYN_LOCAL_CONFIG_TEMPLATE = """\
# Local environment overrides — gitignored, never commit.

# LiteLLM proxy base URL (omit if calling providers directly)
# api_base: http://localhost:4000

# API keys must be set as environment variables, not here:
#   export OPENAI_API_KEY=sk-...
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export GEMINI_API_KEY=...

# Override model mappings for your local setup (optional)
# models:
#   light:    openai/gemini-2.5-flash-lite
#   standard: openai/gemini-2.5-flash-lite
#   strong:   openai/gemini-2.5-flash-lite
"""


def cmd_init(args: argparse.Namespace) -> None:
    cwd = Path.cwd()
    created: list[str] = []
    skipped: list[str] = []

    # reyn.yaml
    project_cfg = cwd / "reyn.yaml"
    if project_cfg.exists():
        skipped.append("reyn.yaml")
    else:
        project_cfg.write_text(_REYN_YAML_TEMPLATE, encoding="utf-8")
        created.append("reyn.yaml")

    # .reyn/config.yaml
    reyn_dir = cwd / ".reyn"
    reyn_dir.mkdir(exist_ok=True)
    local_cfg = reyn_dir / "config.yaml"
    if local_cfg.exists():
        skipped.append(".reyn/config.yaml")
    else:
        local_cfg.write_text(_REYN_LOCAL_CONFIG_TEMPLATE, encoding="utf-8")
        created.append(".reyn/config.yaml")

    # .gitignore — add .reyn/ if not already present
    gitignore = cwd / ".gitignore"
    gitignore_note = ""
    if gitignore.exists():
        content = gitignore.read_text(encoding="utf-8")
        if ".reyn/" not in content:
            gitignore.write_text(content.rstrip() + "\n.reyn/\n", encoding="utf-8")
            gitignore_note = "  (.gitignore updated)"
    else:
        gitignore.write_text(".reyn/\n", encoding="utf-8")
        gitignore_note = "  (.gitignore created)"

    for name in created:
        suffix = gitignore_note if name == ".reyn/config.yaml" else ""
        print(f"  Created   {name}{suffix}")
    for name in skipped:
        print(f"  Exists    {name}  (skipped)")

    print()
    print("Next steps:")
    print("  1. Edit reyn.yaml         — set model mappings for your provider")
    print("  2. Edit .reyn/config.yaml — set api_base if using a proxy")
    print("  3. Export your API key:")
    print("       export OPENAI_API_KEY=sk-...")
    print("       export ANTHROPIC_API_KEY=sk-ant-...")
    print("  4. Run an app:")
    print('       reyn run app_builder "describe the app you want to build"')


_CONFIG_FIELDS = [
    {
        "key":     "model",
        "default": "standard",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Default model class used when a phase has no model_class.",
        "values":  "light | standard | strong  (resolved via models map)",
        "example": "model: standard",
    },
    {
        "key":     "models",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Map of model class names to LiteLLM model strings.",
        "values":  "dict: class_name → litellm_model_string",
        "example": "models:\n  light:    openai/gpt-4o-mini\n  standard: openai/gpt-4o\n  strong:   openai/o3",
    },
    {
        "key":     "api_base",
        "default": "(none)",
        "scope":   ".reyn/config.yaml  (keep out of git)",
        "desc":    "LiteLLM proxy base URL. Set this if you route requests through a local proxy.",
        "values":  "URL string",
        "example": "api_base: http://localhost:4000",
    },
    {
        "key":     "output_language",
        "default": "ja",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Language code injected into the context frame for all LLM outputs.",
        "values":  "BCP-47 language tag (e.g. en, ja, zh)",
        "example": "output_language: en",
    },
    {
        "key":     "state_dir",
        "default": ".reyn",
        "scope":   "reyn.yaml",
        "desc":    "Directory for internal state: artifacts, event logs, eval runs.",
        "values":  "relative or absolute path",
        "example": "state_dir: .reyn",
    },
    {
        "key":     "shell_allowed",
        "default": "false",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Allow the shell Control IR op globally. Equivalent to --allow-shell on every run.",
        "values":  "true | false",
        "example": "shell_allowed: false",
    },
    {
        "key":     "permissions",
        "default": "{}",
        "scope":   "reyn.yaml / .reyn/config.yaml",
        "desc":    "Pre-approve specific Control IR ops without interactive prompts.",
        "values":  "dict: op_kind → 'allow'",
        "example": "permissions:\n  shell: allow",
    },
]


def cmd_config(args: argparse.Namespace) -> None:
    sub = getattr(args, "config_cmd", None)
    if sub == "fields":
        _cmd_config_fields()
    elif sub == "show":
        _cmd_config_show()
    elif sub == "get":
        _cmd_config_get(args.key)
    elif sub == "set":
        _cmd_config_set(args.key, args.value)
    else:
        # Default: show
        _cmd_config_show()


def _cmd_config_fields() -> None:
    W_KEY, W_DEF, W_SCOPE = 18, 10, 34
    header = f"{'Field':<{W_KEY}}  {'Default':<{W_DEF}}  {'Scope':<{W_SCOPE}}  Description"
    print(header)
    print("─" * len(header))
    for f in _CONFIG_FIELDS:
        print(f"{f['key']:<{W_KEY}}  {f['default']:<{W_DEF}}  {f['scope']:<{W_SCOPE}}  {f['desc']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Values:  {f['values']}")
        print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}  Example: {f['example'].splitlines()[0]}")
        for extra_line in f['example'].splitlines()[1:]:
            print(f"{'':>{W_KEY}}  {'':>{W_DEF}}  {'':>{W_SCOPE}}           {extra_line}")
        print()


def _cmd_config_show() -> None:
    import yaml
    config = _load_config()
    effective = {
        "model":           config.model,
        "models":          config.models,
        "api_base":        config.api_base or "(not set)",
        "output_language": config.output_language,
        "state_dir":       config.state_dir,
        "shell_allowed":   config.shell_allowed,
        "permissions":     config.permissions,
    }
    print("# Effective config (merged from all sources)")
    print(yaml.dump(effective, allow_unicode=True, default_flow_style=False), end="")


def _cmd_config_get(key: str) -> None:
    import yaml
    config = _load_config()
    value = getattr(config, key, None)
    if value is None:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print(f"Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)
    if isinstance(value, (dict, list)):
        print(yaml.dump(value, allow_unicode=True, default_flow_style=False), end="")
    else:
        print(value)


def _cmd_config_set(key: str, value: str) -> None:
    import yaml
    valid_keys = {f["key"] for f in _CONFIG_FIELDS}
    check_key = key.split(".")[0] if "." in key else key
    if check_key not in valid_keys:
        print(f"Error: unknown config key '{key}'", file=sys.stderr)
        print(f"Run 'reyn config fields' to see available keys.", file=sys.stderr)
        sys.exit(1)

    local_cfg = Path(".reyn") / "config.yaml"
    local_cfg.parent.mkdir(exist_ok=True)
    current: dict = {}
    if local_cfg.exists():
        current = yaml.safe_load(local_cfg.read_text(encoding="utf-8")) or {}

    # Parse value: try YAML first so booleans/numbers work
    try:
        parsed = yaml.safe_load(value)
    except Exception:
        parsed = value

    # Nested key support: models.standard → models: {standard: ...}
    if "." in key:
        parent, child = key.split(".", 1)
        current.setdefault(parent, {})[child] = parsed
    else:
        current[key] = parsed

    local_cfg.write_text(yaml.dump(current, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    print(f"Set {key} = {parsed!r}  →  {local_cfg}")


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reyn",
        description="Agent OS MVP — LLM-driven phase execution",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    init_p = sub.add_parser("init", help="Create reyn.yaml and .reyn/config.yaml in the current directory")
    init_p.set_defaults(func=cmd_init)

    config_p = sub.add_parser("config", help="View and edit reyn configuration")
    config_sub = config_p.add_subparsers(dest="config_cmd", metavar="<subcommand>")
    config_p.set_defaults(func=cmd_config)

    config_sub.add_parser("show", help="Show current effective config (merged from all sources)")
    config_sub.add_parser("fields", help="List all config fields with descriptions and examples")

    config_get_p = config_sub.add_parser("get", help="Get a single config value")
    config_get_p.add_argument("key", metavar="KEY", help="Config key (e.g. model, api_base)")

    config_set_p = config_sub.add_parser("set", help="Set a config value in .reyn/config.yaml")
    config_set_p.add_argument("key", metavar="KEY",
                              help="Config key (e.g. api_base, models.standard). Run 'reyn config fields' for the full list.")
    config_set_p.add_argument("value", metavar="VALUE", help="Value to set (YAML syntax accepted)")

    apps_p = sub.add_parser("apps", help="List available apps, or show usage details for one app")
    apps_p.add_argument("app_name", nargs="?", default=None, metavar="APP",
                        help="App name to show details for (omit to list all)")
    apps_p.set_defaults(func=cmd_apps)

    run_p = sub.add_parser("run", help="Run an app")
    run_p.add_argument(
        "app_name",
        nargs="?",
        default=None,
        metavar="APP",
        help=(
            "App name to resolve automatically. "
            "Search order: reyn/project/ → reyn/local/ → stdlib. "
            "Example: reyn run app_builder 'describe your app'"
        ),
    )
    run_p.add_argument(
        "--app-path",
        default=None,
        dest="app_path",
        metavar="DIR",
        help=(
            "Path to an app directory containing app.md "
            "(e.g. reyn/project/my_app or reyn/local/my_app). "
            "Use this to point to an explicit location instead of name resolution."
        ),
    )
    run_p.add_argument(
        "--module",
        default=None,
        metavar="MODULE",
        help="Python module path exposing an 'app' object (e.g. examples.writing_app.app)",
    )
    run_p.add_argument(
        "--dsl-root",
        default=None,
        dest="dsl_root",
        metavar="DIR",
        help=(
            "Root of the DSL tree for shared artifact/phase resolution. "
            "Inferred automatically when using app name or --app-path. "
            "Override with this flag when the inferred root is wrong."
        ),
    )
    run_p.add_argument(
        "input",
        nargs="?",
        default=None,
        metavar="INPUT",
        help=(
            "Initial input: JSON artifact string or natural language "
            "(auto-wrapped as user_message). "
            "Reads from stdin if omitted (pipe or redirect)."
        ),
    )
    run_p.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "Model class name (light/standard/strong) or LiteLLM model string. "
            "Resolved via reyn.yaml models map. "
            "Default: from reyn.yaml 'model' key, or 'standard'."
        ),
    )
    run_p.add_argument(
        "--output-language",
        default=None,
        dest="output_language",
        metavar="LANG",
        help="Output language code (default: from reyn.yaml or ja)",
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
        "--allow-shell",
        dest="allow_shell",
        action="store_true",
        help=(
            "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
            "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
            "Off by default for safety."
        ),
    )
    run_p.add_argument(
        "--max-phase-visits",
        dest="max_phase_visits",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum times any single phase may be visited per run (0 = unlimited). "
            "Prevents infinite rollback/revision loops. Default: from reyn.yaml or 25."
        ),
    )
    run_p.set_defaults(func=cmd_run)

    # ── eval ──────────────────────────────────────────────────────────────────
    eval_p = sub.add_parser("eval", help="Run an eval spec against an app")
    eval_p.add_argument("--spec", required=True, metavar="FILE",
                        help="Path to the eval.md spec file")
    eval_p.add_argument("--model", default=None, metavar="MODEL",
                        help="Model class name or LiteLLM string for running the app (default: from spec or 'standard')")
    eval_p.add_argument("--judge-model", dest="judge_model", default=None, metavar="MODEL",
                        help="Model class name or LiteLLM string for LLM-as-judge (default: same as --model)")
    eval_p.add_argument("--dsl-root", dest="dsl_root", default=None, metavar="DIR",
                        help="DSL root override (default: from spec frontmatter)")
    eval_p.add_argument("--output-language", default="ja", dest="output_language", metavar="LANG")
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

    lint_p = sub.add_parser("lint", help="Lint a DSL app for issues")
    lint_p.add_argument(
        "--app",
        required=True,
        metavar="APP",
        help="App name to lint (same resolution as `reyn run <app>`)",
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
