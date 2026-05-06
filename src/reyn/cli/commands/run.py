"""`reyn run` — execute an app."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..logger_factory import make_logger
from ..session import Session
from ..skill_loader import load_skill_from_args
from ..summary import print_run_result


def register(sub) -> None:
    p = sub.add_parser("run", help="Run a skill")
    p.add_argument(
        "skill_name", nargs="?", default=None, metavar="SKILL",
        help=(
            "Skill name to resolve automatically. "
            "Search order: reyn/project/ → reyn/local/ → stdlib. "
            "Example: reyn run skill_builder 'describe your skill'"
        ),
    )
    p.add_argument(
        "--skill-path", default=None, dest="skill_path", metavar="DIR",
        help=(
            "Path to a skill directory containing skill.md "
            "(e.g. reyn/project/my_skill or reyn/local/my_skill). "
            "Use this to point to an explicit location instead of name resolution."
        ),
    )
    p.add_argument(
        "--module", default=None, metavar="MODULE",
        help="Python module path exposing a 'skill' object (e.g. examples.writing_skill.skill)",
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
    add_common_args(p)
    p.add_argument("--events", action="store_true",
                   help="Print the full event log after execution")
    p.add_argument("--strict", action="store_true",
                   help=(
                       "Enable strict schema validation: enforce required fields at every nesting depth. "
                       "Default (lenient) only enforces required at the top level of each artifact."
                   ))
    p.add_argument("--allow-shell", dest="allow_shell", action="store_true",
                   help=(
                       "Enable the 'shell' Control IR op, which allows the LLM to execute shell commands. "
                       "Required for meta-apps that invoke sub-processes (e.g. app_improver). "
                       "Off by default for safety."
                   ))
    p.add_argument("--allow-untrusted-python", dest="allow_untrusted_python",
                   action="store_true",
                   help=(
                       "Enable trusted-mode Python preprocessor steps (no AST sandboxing). "
                       "Pure-mode python steps run without this flag. Off by default."
                   ))
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    session = Session.from_args(args)
    loaded = load_skill_from_args(args)

    raw_input = _read_input(args)
    initial_input = _parse_cli_input(raw_input)

    model, resolved_model = session.model_for(args)
    # output_language is Optional[str]. None propagates all the way down
    # to the LLM prompt builders, which skip the language directive so
    # the LLM picks the reply language based on the user's input
    # naturally. Reyn explicitly does not fall back to a regional default
    # (= no silent "ja" default) — the project targets a global audience.
    output_language = session.output_language_for(args)
    shell_allowed = session.shell_allowed_for(args)
    limits = session.limits_for(args)

    trusted_python = bool(getattr(args, "allow_untrusted_python", False))
    perm_resolver = _build_permission_resolver(
        session.config, shell_allowed, trusted_python=trusted_python,
    )

    from reyn.agent import Agent
    from reyn.config import _find_project_root, load_project_context
    from reyn.user_intervention import StdinInterventionBus
    project_root = _find_project_root(Path.cwd())
    project_context = load_project_context(session.config, project_root)
    logger = make_logger()
    agent = Agent(
        model=model,
        strict=args.strict,
        subscribers=[logger],
        intervention_bus=StdinInterventionBus(),
        shell_allowed=shell_allowed,
        resolver=session.resolver,
        permission_resolver=perm_resolver,
        limits=limits,
        mcp_servers=session.config.mcp,
        python_allowed_modules=list(session.config.python.allowed_modules),
        prompt_cache_enabled=session.config.prompt_cache_enabled,
        project_context=project_context,
        caller="direct",
    )

    input_type = initial_input.get("type", "unknown")
    model_display = f"{model} → {resolved_model}" if resolved_model != model else model
    print(f"skill           : {loaded.skill.name}")
    print(f"model           : {model_display}")
    print(f"output_language : {output_language}")
    print(f"input type      : {input_type}")
    print(f"input           : {json.dumps(initial_input, ensure_ascii=False)}")
    print()

    try:
        result = run_async(
            agent.run(loaded.skill, initial_input, output_language=output_language)
        )
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


def _build_permission_resolver(config, shell_allowed: bool, trusted_python: bool = False):
    from reyn.config import _find_project_root
    from reyn.permissions.permissions import PermissionResolver
    project_root = _find_project_root(Path.cwd())
    perm_config = getattr(config, "permissions", {}) or {}
    if shell_allowed and "shell" not in perm_config:
        perm_config = dict(perm_config, shell="allow")
    return PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
        trusted_python_allowed=trusted_python,
    )


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))
