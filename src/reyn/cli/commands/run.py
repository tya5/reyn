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
        "--skill-root", default=None, dest="skill_root", metavar="DIR",
        help=(
            "Root of the skill tree for shared artifact/phase resolution. "
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
    # FP-0014: --allow-untrusted-python renamed → --allow-unsafe-python.
    # Both flags target the same dest so legacy invocations keep working
    # during the Track A → B transition. New code should use the new flag.
    p.add_argument("--allow-unsafe-python", "--allow-untrusted-python",
                   dest="allow_unsafe_python",
                   action="store_true",
                   help=(
                       "Enable unsafe-mode Python preprocessor steps (no AST sandboxing). "
                       "Safe-mode python steps run without this flag. Off by default."
                   ))
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    session = Session.from_args(args)
    from reyn.cli.credentials_check import verify_credentials_or_exit
    verify_credentials_or_exit(session, args)
    loaded = load_skill_from_args(args)

    raw_input = _read_input(args)
    initial_input = _parse_cli_input(
        raw_input, default_type=_entry_input_type(loaded.skill),
    )

    model, resolved_model = session.model_for(args)
    # output_language is Optional[str]. None propagates all the way down
    # to the LLM prompt builders, which skip the language directive so
    # the LLM picks the reply language based on the user's input
    # naturally. Reyn explicitly does not fall back to a regional default
    # (= no silent "ja" default) — the project targets a global audience.
    output_language = session.output_language_for(args)
    shell_allowed = session.shell_allowed_for(args)
    safety = session.safety_for(args)

    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    # Stdlib skills ship with the Reyn team's code and are trusted by
    # construction — auto-allow their unsafe python steps so users don't need
    # --allow-unsafe-python when running e.g. `reyn run mcp_install`.
    if not unsafe_python and loaded.skill_md is not None:
        from reyn.skill.skill_paths import is_stdlib_skill
        unsafe_python = is_stdlib_skill(loaded.skill_md.parent)
    perm_resolver = _build_permission_resolver(
        session.config, shell_allowed, unsafe_python=unsafe_python,
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
        safety=safety,
        mcp_servers=session.config.mcp,
        python_allowed_modules=list(session.config.python.allowed_modules),
        prompt_cache_enabled=session.config.prompt_cache_enabled,
        project_context=project_context,
        caller="direct",
        sandbox_config=session.config.sandbox,
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


def _entry_input_type(skill) -> str | None:
    """Return the artifact type name to wrap a bare-data CLI input dict with,
    or None when the wrap isn't safe to apply automatically.

    Safe to wrap when the entry phase has exactly one input artifact (no
    ``anyOf`` union) and that artifact is declared ``wrapped: true`` (default)
    so its compiled schema carries ``{type, data}`` envelope properties.

    Skipped when:
      - entry phase missing (defensive)
      - input is an ``anyOf`` of multiple artifact types (= which type would
        we wrap with? — leave to the user)
      - artifact is declared ``wrapped: false`` (= no envelope; the bare dict
        IS the artifact, not its inner data)
    """
    entry = skill.phases.get(skill.entry_phase)
    if entry is None:
        return None
    schema = entry.input_schema or {}
    if "anyOf" in schema:
        return None
    props = schema.get("properties") or {}
    if "type" not in props or "data" not in props:
        return None
    return entry.input_schema_name


def _parse_cli_input(raw: str, *, default_type: str | None = None) -> dict:
    """Accept JSON or natural language and return a Reyn artifact dict.

    Cases:
      - **Non-JSON text** → wrapped as ``{type: user_message, data: {text}}``.
      - **JSON dict already in envelope form** (= has a ``type`` key) →
        passed through unchanged.
      - **JSON dict in bare-data form + ``default_type`` provided** →
        auto-wrapped as ``{type: default_type, data: <parsed>}``. This is
        the path that lets ``reyn run index_docs '{"source":..., "path":...}'``
        (README's RAG demo) work without forcing users to know the artifact
        envelope; the skill loader supplies the target type via
        ``_entry_input_type``.
      - **Anything else** (JSON list, string, number, dict with no
        ``default_type``) → passed through unchanged; downstream artifact
        validation reports the mismatch.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "user_message", "data": {"text": raw}}

    if (
        isinstance(parsed, dict)
        and "type" not in parsed
        and default_type is not None
    ):
        return {"type": default_type, "data": parsed}
    return parsed


def _build_permission_resolver(config, shell_allowed: bool, unsafe_python: bool = False):
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
        unsafe_python_allowed=unsafe_python,
    )


def _print_events(agent) -> None:
    print("=== Event Log ===")
    for event in agent.get_events_json():
        print(json.dumps(event, ensure_ascii=False, default=str))
