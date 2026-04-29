"""`reyn run` — execute an app."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from ..app_loader import load_app_from_args
from ..logger_factory import make_logger
from ..session import Session
from ..summary import print_run_result


def register(sub) -> None:
    p = sub.add_parser("run", help="Run an app")
    p.add_argument(
        "app_name", nargs="?", default=None, metavar="APP",
        help=(
            "App name to resolve automatically. "
            "Search order: reyn/project/ → reyn/local/ → stdlib. "
            "Example: reyn run app_builder 'describe your app'"
        ),
    )
    p.add_argument(
        "--app-path", default=None, dest="app_path", metavar="DIR",
        help=(
            "Path to an app directory containing app.md "
            "(e.g. reyn/project/my_app or reyn/local/my_app). "
            "Use this to point to an explicit location instead of name resolution."
        ),
    )
    p.add_argument(
        "--module", default=None, metavar="MODULE",
        help="Python module path exposing an 'app' object (e.g. examples.writing_app.app)",
    )
    p.add_argument(
        "--dsl-root", default=None, dest="dsl_root", metavar="DIR",
        help=(
            "Root of the DSL tree for shared artifact/phase resolution. "
            "Inferred automatically when using app name or --app-path. "
            "Override with this flag when the inferred root is wrong."
        ),
    )
    p.add_argument(
        "input", nargs="?", default=None, metavar="INPUT",
        help=(
            "Initial input: JSON artifact string or natural language "
            "(auto-wrapped as user_message). "
            "Reads from stdin if omitted (pipe or redirect)."
        ),
    )
    p.add_argument(
        "--model", default=None, metavar="MODEL",
        help=(
            "Model class name (light/standard/strong) or LiteLLM model string. "
            "Resolved via reyn.yaml models map. "
            "Default: from reyn.yaml 'model' key, or 'standard'."
        ),
    )
    p.add_argument(
        "--output-language", default=None, dest="output_language", metavar="LANG",
        help="Output language code (default: from reyn.yaml or ja)",
    )
    p.add_argument("--events", action="store_true",
                   help="Print the full event log after execution")
    p.add_argument("--strict", action="store_true",
                   help=(
                       "Enable strict schema validation: enforce required fields at every nesting depth. "
                       "Default (lenient) only enforces required at the top level of each artifact."
                   ))
    p.add_argument("--rich", action="store_true",
                   help="Use Rich-styled console output instead of plain text logging.")
    p.add_argument("--allow-shell", dest="allow_shell", action="store_true",
                   help=(
                       "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
                       "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
                       "Off by default for safety."
                   ))
    p.add_argument("--max-phase-visits", dest="max_phase_visits", type=int,
                   default=None, metavar="N",
                   help=(
                       "Maximum times any single phase may be visited per run (0 = unlimited). "
                       "Prevents infinite rollback/revision loops. Default: from reyn.yaml or 25."
                   ))
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    session = Session.from_args(args)
    loaded = load_app_from_args(args)

    raw_input = _read_input(args)
    initial_input = _parse_cli_input(raw_input)

    model, resolved_model = session.model_for(args)
    output_language = session.output_language_for(args)
    shell_allowed = session.shell_allowed_for(args)
    max_phase_visits = session.max_phase_visits_for(args)

    perm_resolver = _build_permission_resolver(session.config, shell_allowed)

    from reyn.agent import Agent
    logger = make_logger(rich=args.rich)
    agent = Agent(
        model=model,
        state_dir=session.config.state_dir,
        strict=args.strict,
        subscribers=[logger],
        shell_allowed=shell_allowed,
        resolver=session.resolver,
        permission_resolver=perm_resolver,
        max_phase_visits=max_phase_visits,
    )

    input_type = initial_input.get("type", "unknown")
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model
    print(f"app             : {loaded.app.name}")
    print(f"model           : {model_display}")
    print(f"output_language : {output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = agent.run(loaded.app, initial_input, output_language=output_language)
    except Exception as e:
        print(f"\nError during execution: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    if not result.ok:
        print(f"=== Warning: workflow ended with status '{result.status}' ===",
              file=sys.stderr)
    print("=== Final Output ===")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))
    print_run_result(result.token_usage, result.cost_usd)
    print(f"\nevents saved → {agent.events_path}")

    if args.events:
        print()
        _print_events(agent)

    if not result.ok:
        sys.exit(2)


def _read_input(args: argparse.Namespace) -> str:
    if args.input is not None:
        return args.input
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    print("Error: provide INPUT argument or pipe input via stdin.", file=sys.stderr)
    sys.exit(1)


def _parse_cli_input(raw: str) -> dict:
    """Accept JSON or natural language. Natural language → user_message artifact."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}


def _build_permission_resolver(config, shell_allowed: bool):
    from reyn.permissions import PermissionResolver
    from reyn.config import _find_project_root
    project_root = _find_project_root(Path.cwd())
    perm_config = getattr(config, "permissions", {}) or {}
    if shell_allowed and "shell" not in perm_config:
        perm_config = dict(perm_config, shell="allow")
    return PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
    )


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))
