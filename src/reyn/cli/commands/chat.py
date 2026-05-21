"""`reyn chat [name]` — interactive chat, optionally attaching to a named agent.

PR10: launches the AgentRegistry, attaches to the named agent (or `default`),
then hands off to `run_repl`. The registry holds all loaded ChatSession
instances; switching agents mid-REPL via `/attach <name>` happens through it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..session import Session


def register(sub) -> None:
    p = sub.add_parser("chat", help="Start an interactive chat session")
    p.add_argument(
        "agent_name", nargs="?", default=None,
        help="Agent to attach to (default: 'default'). "
             "Use `reyn agent new <name>` to create a new agent.",
    )
    p.add_argument(
        "--cui",
        action="store_true",
        default=False,
        help=(
            "Use plain console output (no TUI). "
            "Useful for piping output, debugging, or headless environments."
        ),
    )
    p.add_argument(
        "--no-restore",
        action="store_true",
        default=False,
        help=(
            "Skip restoring in-flight skill state from disk this run. "
            "Useful for debugging or starting a clean session without "
            "discarding the persisted state (it will be loaded on next run)."
        ),
    )
    p.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help=(
            "Wipe in-flight skill state (snapshots + WAL) before starting. "
            "Audit logs in .reyn/events/ are preserved. "
            "Asks for confirmation before deleting."
        ),
    )
    # FP-0014: --allow-untrusted-python renamed → --allow-unsafe-python.
    # Both flags target the same dest so legacy invocations keep working
    # during the Track A → B transition.
    p.add_argument(
        "--allow-unsafe-python", "--allow-untrusted-python",
        dest="allow_unsafe_python",
        action="store_true",
        help=(
            "Enable unsafe-mode Python preprocessor steps (no AST sandboxing). "
            "Safe-mode python steps run without this flag. Off by default."
        ),
    )
    p.add_argument(
        "--banner",
        action="store_true",
        default=False,
        help=(
            "Show the ASCII-art startup banner (gradient REYN logo + agent / "
            "model info, neofetch style). Off by default for instant input "
            "focus on daily use."
        ),
    )
    # B25-S5-1: eager embedding-index build flag.
    p.add_argument(
        "--eager-embedding-build",
        action="store_true",
        default=False,
        help=(
            "Await the action embedding index build synchronously on the "
            "first turn (pays ~2-5s once) so search_actions is visible to "
            "the LLM from the very first call. Default lazy background "
            "build leaves search_actions hidden until Turn 2. Recommended "
            "for dogfood / scripted runs against fresh .reyn/ workspaces."
        ),
    )
    # Issue #276 Phase A — TUI thin client mode connecting to a remote
    # `reyn web` server. When set, the local ChatSession / AgentRegistry
    # / state restore are skipped; the TUI streams from
    # ``ws://<host>[:port]/ws/chat/<agent>``. Right panel features that
    # depend on local files (events / memory / pending) render
    # "remote — limited" placeholders per #276 Phase C-(b) scoped
    # disable; full parity via REST is future work (Phase C-(a)).
    p.add_argument(
        "--connect",
        metavar="WS_URL",
        default=None,
        help=(
            "Connect to a remote `reyn web` server over WebSocket "
            "(e.g. --connect ws://localhost:8080). The positional "
            "agent_name selects which agent on the server. Requires "
            "`pip install reyn[web]`. Right panel features that need "
            "local file access render in 'remote — limited' v1 mode."
        ),
    )
    add_common_args(p)
    p.set_defaults(func=run)


def _reset_project_state(project_root: Path, *, confirm: bool = True) -> bool:
    """Wipe in-flight skill state under ``project_root/.reyn/``.

    Removes:
      - ``.reyn/state/wal.jsonl`` (process WAL)
      - ``.reyn/agents/<name>/state/snapshot.json`` (per-agent snapshots)
      - ``.reyn/agents/<name>/state/skills/`` (per-skill snapshots)

    Preserves:
      - ``.reyn/events/`` (audit log, P6 truth — must not be wiped)
      - profile.yaml, MEMORY.md, etc. (non-runtime state)

    Returns:
      ``True`` if the reset proceeded (or no state existed); ``False`` if
      the user declined the confirmation prompt.
    """
    if confirm:
        try:
            answer = input(
                "This will delete all in-flight skill state "
                "(snapshots + WAL). Audit logs are preserved.\n"
                "Continue? [yes/no]: "
            ).strip().lower()
        except EOFError:
            answer = "no"
        if answer not in ("yes", "y"):
            return False

    # Delete WAL
    wal_path = project_root / ".reyn" / "state" / "wal.jsonl"
    wal_path.unlink(missing_ok=True)

    # Delete per-agent snapshots + per-skill snapshots dir
    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            state_dir = agent_dir / "state"
            if not state_dir.is_dir():
                continue
            (state_dir / "snapshot.json").unlink(missing_ok=True)
            skills_dir = state_dir / "skills"
            if skills_dir.is_dir():
                shutil.rmtree(skills_dir, ignore_errors=True)

    return True


def _run_connect_mode(args: argparse.Namespace, base_url: str) -> None:
    """Issue #276 Phase A — connect to a remote `reyn web` server.

    Skips local ChatSession / AgentRegistry / state restore. The TUI
    streams frames from ``ws://<host>[:port]/ws/chat/<agent_name>``;
    user input is forwarded to the server as ``user_message`` frames.

    Right-panel features that need local file access (events / memory
    / pending) surface "remote — limited" placeholders via each tab's
    existing remote-mode handling (e.g. PR #280 Pending tab
    ``remote_mode=True``). Phase C-(a) future iteration via REST
    will lift the limit; v1 takes the scoped-disable path per the
    #276 owner decision.
    """
    from reyn.chat.tui.app import run_tui
    from reyn.chat.tui.ws_client import connect as ws_connect
    from reyn.llm.llm import run_async

    agent_name = args.agent_name or "default"
    # No project root probe — remote mode doesn't read local .reyn/
    # state. Use cwd so any path-shaped attribute access on the
    # registry resolves to *something* sensible.
    project_root = Path.cwd()

    async def _main() -> None:
        try:
            registry = await ws_connect(
                base_url, agent_name, project_root=project_root,
            )
        except ImportError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(1)
        except ConnectionError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            sys.exit(2)
        try:
            await run_tui(
                registry,
                agent_name=agent_name,
                # Model / budget unknown in remote mode — the server
                # owns them. Show empty fields rather than guessing.
                model="",
                budget_tracker=None,
                banner=getattr(args, "banner", False),
            )
        finally:
            await registry.shutdown()

    run_async(_main())


def run(args: argparse.Namespace) -> None:
    # Issue #276 Phase A — TUI thin client mode short-circuits before
    # any local session / state setup. Bifurcates at the top of run()
    # so the local-mode block stays untouched (= backwards-compat 100%).
    connect_url = getattr(args, "connect", None)
    if connect_url:
        _run_connect_mode(args, connect_url)
        return

    from reyn.budget.budget import BudgetTracker
    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import DEFAULT_AGENT_NAME, AgentRegistry
    from reyn.chat.repl import run_repl
    from reyn.chat.session import ChatSession
    from reyn.config import _find_project_root, load_project_context
    from reyn.events.state_log import StateLog
    from reyn.permissions.permissions import PermissionResolver

    session_cfg = Session.from_args(args)
    from reyn.cli.credentials_check import verify_credentials_or_exit
    verify_credentials_or_exit(session_cfg, args)
    # ``model`` (= tier key like "standard" / "strong") drives ChatSession's
    # ModelResolver. ``resolved_model`` (= the litellm string like
    # "openai/gemini-2.5-flash-lite") is what the header should surface so
    # the user can see which model their requests actually go to.
    model, resolved_model = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    safety = session_cfg.safety_for(args)

    project_root = _find_project_root(Path.cwd()) or Path.cwd()

    # PR-resume-ux β U3: handle --reset before constructing state_log so
    # we don't open a freshly-written WAL just to delete it.
    if getattr(args, "reset", False):
        proceeded = _reset_project_state(project_root, confirm=True)
        if not proceeded:
            print("Reset aborted.", file=sys.stderr)
            sys.exit(0)
        print("State reset. Starting with empty session.", file=sys.stderr)

    # PR21: process-shared WAL for crash recovery. Owned by AgentRegistry,
    # injected into each ChatSession at construction.
    state_log = StateLog(project_root / ".reyn" / "state" / "wal.jsonl")
    # PR22: process-shared budget tracker. Defaults to all unlimited unless
    # `cost:` is configured.
    budget_tracker = BudgetTracker(session_cfg.config.cost, safety=safety)
    # PR25: hydrate daily / monthly counters from the persistent ledger.
    budget_tracker.hydrate(project_root / ".reyn" / "state" / "budget_ledger.jsonl")
    # R-D8: restore in-memory counters (per-agent / per-chain-skill) from
    # the state snapshot written by the previous run. Together with PR25
    # ledger hydration, this makes cap enforcement survive crash + restart.
    budget_state_path = project_root / ".reyn" / "state" / "budget_state.json"
    budget_tracker.load_state(budget_state_path)
    budget_tracker.set_state_path(budget_state_path)
    perm_config = getattr(session_cfg.config, "permissions", {}) or {}
    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    # Single PermissionResolver shared across agents (per the PR10 decision:
    # `.reyn/approvals.yaml` is process-wide).
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=sys.stdin.isatty(),
        unsafe_python_allowed=unsafe_python,
    )

    project_context = load_project_context(session_cfg.config, project_root)

    def _session_factory(profile: AgentProfile):
        # Captured CLI defaults — registry doesn't need to know them.
        s = ChatSession(
            agent_name=profile.name,
            model=model,
            resolver=session_cfg.resolver,
            permission_resolver=perm_resolver,
            safety=safety,
            mcp_servers=session_cfg.config.mcp,
            output_language=output_language,
            prompt_cache_enabled=session_cfg.config.prompt_cache_enabled,
            project_context=project_context,
            agent_role=profile.role,
            compaction_config=session_cfg.config.chat.compaction,
            registry=registry,  # back-reference for :agents / :attach + PR11 messaging
            allowed_skills=profile.allowed_skills,
            allowed_mcp=profile.allowed_mcp,
            events_config=session_cfg.config.events,
            state_log=state_log,
            budget_tracker=budget_tracker,
            sandbox_config=session_cfg.config.sandbox,
            action_retrieval_config=session_cfg.config.action_retrieval,
            embedding_config=session_cfg.config.embedding,
            eager_embedding_build=getattr(args, "eager_embedding_build", False),
            agent_id=session_cfg.config.agent.id,  # FP-0016 E
        )
        s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
    )

    name = args.agent_name or DEFAULT_AGENT_NAME
    if not registry.exists(name):
        print(
            f"Error: agent {name!r} not found. "
            f"Run `reyn agent new {name}` to create it (or omit the name to use 'default').",
            file=sys.stderr,
        )
        sys.exit(1)

    use_tui = not getattr(args, "cui", False) and sys.stdin.isatty()
    skip_restore = getattr(args, "no_restore", False)
    if skip_restore:
        print(
            "⚠ --no-restore: skill state on disk is NOT loaded this run. "
            "Rerun without --no-restore to resume in-flight skills.",
            file=sys.stderr,
        )

    # PR-resume-ux β U4: catch schema mismatch surfaced from restore_all
    # to give the operator a clean error rather than a stack trace.
    from reyn.events.agent_snapshot import SchemaVersionError

    async def _safe_restore() -> bool:
        """Returns True on success, False if the operator should retry."""
        try:
            await registry.restore_all()
            return True
        except SchemaVersionError as e:
            print(f"\nSchema version mismatch: {e}\n", file=sys.stderr)
            return False

    if use_tui:
        from reyn.chat.tui.app import run_tui

        async def _main_tui() -> None:
            if not skip_restore:
                if not await _safe_restore():
                    sys.exit(1)
            await registry.attach(name)
            await run_tui(
                registry,
                agent_name=name,
                model=resolved_model,
                budget_tracker=budget_tracker,
                banner=getattr(args, "banner", False),
                no_restore=skip_restore,
            )

        run_async(_main_tui())
    else:
        from reyn.chat.repl import run_repl

        from ..logger_factory import make_chat_renderer

        renderer = make_chat_renderer()

        async def _main_cui() -> None:
            # PR21: replay WAL into per-agent snapshots before any new state
            # changes happen. Agents with restored state get their inbox /
            # pending_chains repopulated and their main loop started here.
            # PR-resume-ux β U3: --no-restore skips this for debugging.
            # PR-resume-ux β U4: clean exit on schema version mismatch.
            if not skip_restore:
                if not await _safe_restore():
                    sys.exit(1)
            await registry.attach(name)
            await run_repl(registry, renderer=renderer)

        run_async(_main_cui())
