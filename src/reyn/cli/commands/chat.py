"""`reyn chat [name]` — interactive chat, optionally attaching to a named agent.

PR10: launches the AgentRegistry, attaches to the named agent (or `default`),
then hands off to `run_repl`. The registry holds all loaded ChatSession
instances; switching agents mid-REPL via `:attach <name>` happens through it.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

from ...llm import run_async
from ..common_args import add_common_args
from ..session import Session


def register(sub) -> None:
    p = sub.add_parser("chat", help="Start an interactive chat session")
    p.add_argument(
        "agent_name", nargs="?", default=None,
        help="Agent to attach to (default: 'default'). "
             "Use `reyn agent new <name>` to create a new agent.",
    )
    p.add_argument("--rich", action="store_true",
                   help="Use Rich-styled console output instead of plain text.")
    add_common_args(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> None:
    from reyn.chat.session import ChatSession
    from reyn.chat.registry import AgentRegistry, DEFAULT_AGENT_NAME
    from reyn.chat.profile import AgentProfile
    from reyn.chat.repl import run_repl
    from reyn.config import _find_project_root, load_project_context
    from reyn.permissions import PermissionResolver

    session_cfg = Session.from_args(args)
    model, _ = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    limits = session_cfg.limits_for(args)

    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    perm_config = getattr(session_cfg.config, "permissions", {}) or {}
    # Single PermissionResolver shared across agents (per the PR10 decision:
    # `.reyn/approvals.yaml` is process-wide).
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
    )

    project_context = load_project_context(session_cfg.config, project_root)

    def _session_factory(profile: AgentProfile):
        # Captured CLI defaults — registry doesn't need to know them.
        s = ChatSession(
            agent_name=profile.name,
            model=model,
            resolver=session_cfg.resolver,
            permission_resolver=perm_resolver,
            limits=limits,
            mcp_servers=session_cfg.config.mcp,
            output_language=output_language,
            prompt_cache_enabled=session_cfg.config.prompt_cache_enabled,
            project_context=project_context,
            agent_role=profile.role,
            compaction_config=session_cfg.config.chat.compaction,
            registry=registry,  # back-reference for :agents / :attach + PR11 messaging
            max_hop_depth=session_cfg.config.multi_agent.max_hop_depth,
            chain_timeout_seconds=session_cfg.config.multi_agent.chain_timeout_seconds,
            allowed_skills=profile.allowed_skills,
            events_config=session_cfg.config.events,
        )
        s.load_history()
        return s

    registry = AgentRegistry(project_root=project_root, session_factory=_session_factory)

    name = args.agent_name or DEFAULT_AGENT_NAME
    if not registry.exists(name):
        print(
            f"Error: agent {name!r} not found. "
            f"Run `reyn agent new {name}` to create it (or omit the name to use 'default').",
            file=sys.stderr,
        )
        sys.exit(1)

    from ..logger_factory import make_chat_renderer
    renderer = make_chat_renderer(rich=args.rich)

    async def _main() -> None:
        await registry.attach(name)
        await run_repl(registry, renderer=renderer)

    run_async(_main())
