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
            from compiler import load_dsl_app
            app = load_dsl_app(args.app_dsl)
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

    agent = Agent(model=args.model, workspace_dir=args.workspace)

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
    print("=== Final Output ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if args.events:
        print()
        _print_events(agent)


def cmd_lint(args: argparse.Namespace) -> None:
    from pathlib import Path
    from compiler.linter import lint_dsl

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
    from compiler.formatter import format_dsl

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
    run_p.set_defaults(func=cmd_run)

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

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
