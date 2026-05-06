"""`reyn mcp serve` — expose Reyn agents to outer LLM clients via MCP.

This is the inverse of `reyn chat`'s usual flow: instead of an interactive
TUI/CUI driving the AgentRegistry, we hand the registry to an MCP server
that speaks JSON-RPC over stdio. Outer clients (Claude Code, Cursor,
OpenAI Agents SDK with MCP enabled, …) then talk INTO Reyn with the two
tools defined in :mod:`reyn.mcp_server`.

Wiring mirrors `chat.py` (model resolver, permission resolver, state log,
budget tracker, project context) — minus the TUI / REPL launch.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..session import Session


def register(sub) -> None:
    p = sub.add_parser(
        "mcp",
        help="Model Context Protocol — expose Reyn agents to outer LLM clients",
    )
    msub = p.add_subparsers(dest="mcp_command", metavar="<subcommand>")
    msub.required = True

    serve = msub.add_parser(
        "serve",
        help="Run an MCP server (stdio) so external clients can chat with agents",
    )
    serve.add_argument(
        "--project", dest="project", default=None, metavar="PATH",
        help=(
            "Project root containing reyn.yaml. Defaults to the closest "
            "ancestor with a reyn.yaml, or the current directory."
        ),
    )
    serve.add_argument(
        "--timeout", dest="timeout", type=float, default=60.0, metavar="SECONDS",
        help=(
            "Maximum blocking time per send_to_agent call (default: 60). "
            "On timeout the call returns whatever reply has accumulated; the "
            "agent keeps working in the background."
        ),
    )
    add_common_args(serve)
    serve.set_defaults(func=run_serve)


def run_serve(args: argparse.Namespace) -> None:
    from reyn.budget.budget import BudgetTracker
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import AgentRegistry
    from reyn.chat.session import ChatSession
    from reyn.config import _find_project_root, load_project_context
    from reyn.events.state_log import StateLog
    from reyn.mcp_server import serve_stdio
    from reyn.permissions.permissions import PermissionResolver

    session_cfg = Session.from_args(args)
    model, _ = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    limits = session_cfg.limits_for(args)

    if args.project:
        project_root = Path(args.project).resolve()
    else:
        # Note: MCP clients (Claude Desktop, Cursor, …) typically don't
        # honour a `cwd` field in their server config, so the spawned
        # process can land in `/`. If no --project was given and no
        # reyn.yaml is reachable from cwd, fail loudly rather than
        # silently creating `/.reyn/` (read-only on macOS) or worse.
        found = _find_project_root(Path.cwd())
        if found is None:
            print(
                "error: no reyn.yaml found from cwd; pass --project "
                "<path-to-project-root>. MCP clients typically ignore "
                "the `cwd` field in their config — set --project in "
                "the args list instead.",
                file=sys.stderr,
            )
            sys.exit(1)
        project_root = found

    if not (project_root / "reyn.yaml").exists():
        print(
            f"error: {project_root}/reyn.yaml not found. "
            f"Run `reyn init` there or pass a different --project path.",
            file=sys.stderr,
        )
        sys.exit(1)

    # MCP clients (Claude Desktop, Cursor, …) spawn the server with cwd=`/`,
    # which causes any code that uses relative `.reyn/...` paths to try to
    # write under the filesystem root and crash with a read-only-fs error.
    # `--project` only fixes the explicit StateLog/budget paths in this
    # function; deeper code paths (ChatSession, Workspace, AgentRegistry)
    # also use relative paths internally. Anchor the whole process at the
    # project root so the same code that works under `reyn chat` works
    # here unchanged.
    os.chdir(project_root)

    state_log = StateLog(project_root / ".reyn" / "state" / "wal.jsonl")
    budget_tracker = BudgetTracker(session_cfg.config.cost)
    budget_tracker.hydrate(project_root / ".reyn" / "state" / "budget_ledger.jsonl")
    budget_state_path = project_root / ".reyn" / "state" / "budget_state.json"
    budget_tracker.load_state(budget_state_path)
    budget_tracker.set_state_path(budget_state_path)

    perm_config = getattr(session_cfg.config, "permissions", {}) or {}
    # MCP serve runs non-interactively (no human at this stdin — that's the
    # MCP client's transport), so the resolver should never block on prompts.
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=False,
        trusted_python_allowed=False,
    )

    project_context = load_project_context(session_cfg.config, project_root)

    def _session_factory(profile: AgentProfile):
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
            registry=registry,
            max_hop_depth=session_cfg.config.multi_agent.max_hop_depth,
            chain_timeout_seconds=session_cfg.config.multi_agent.chain_timeout_seconds,
            allowed_skills=profile.allowed_skills,
            allowed_mcp=profile.allowed_mcp,
            events_config=session_cfg.config.events,
            state_log=state_log,
            budget_tracker=budget_tracker,
        )
        s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
    )

    timeout = float(getattr(args, "timeout", 60.0) or 60.0)

    async def _main() -> None:
        # Replay WAL into per-agent snapshots so any stranded in-flight skills
        # resume cleanly, the same as `reyn chat` startup. Schema mismatch
        # surfaces a clean stderr line and exits non-zero.
        from reyn.events.agent_snapshot import SchemaVersionError
        try:
            await registry.restore_all()
        except SchemaVersionError as e:
            print(f"Schema version mismatch: {e}", file=sys.stderr)
            sys.exit(1)
        await serve_stdio(registry, timeout=timeout)

    run_async(_main())
