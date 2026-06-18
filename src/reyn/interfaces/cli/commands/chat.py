"""`reyn chat [name]` — interactive chat, optionally attaching to a named agent.

PR10: launches the AgentRegistry, attaches to the named agent (or `default`),
then hands off to `run_repl`. The registry holds all loaded Session
instances; switching agents mid-REPL via `/attach <name>` happens through it.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from reyn.interfaces.cli.env_backend import (
    build_environment_backend,
    register_env_backend_args,
)
from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..invocation_context import InvocationContext

# #187: send_to_agent_impl timeout for the one-shot (`reyn run-once`) drive. The
# autonomous SWE agent may iterate for many minutes; the external bound is the
# caller's process timeout (the SWE runner's subprocess timeout). On timeout the
# agent's in-container edits persist (partial reply), so the caller still extracts
# the model_patch via `git diff`.
_ONCE_SEND_TIMEOUT = 3600.0


async def _run_once(agent_registry, agent_name, *, instream=None, send=None) -> dict:
    """#187 one-shot drive: read the WHOLE *instream* (default stdin) as a SINGLE
    user message and drive the agent to completion via ``send_to_agent_impl``,
    returning the result dict (``reply`` + ``limit_stopped`` + …).

    This is the structural fix for the #1401 line-fragmentation bug: the WHOLE
    stdin becomes ONE message (one ``send`` call), NOT one message per line (the
    REPL's line-by-line ``readline``). ``instream`` / ``send`` are injectable so
    the whole-message-not-fragmented behavior is testable with a recording double
    (no mock); production uses ``sys.stdin`` + the real ``send_to_agent_impl``.

    #1649: returns the full result dict (not just the reply str) so the caller
    can detect ``limit_stopped`` and exit non-zero — a limit hit must never be a
    silent exit-0 stop for a non-TTY wrapper.
    """
    if instream is None:
        instream = sys.stdin
    if send is None:
        from reyn.mcp.server import send_to_agent_impl as send
    message = instream.read()
    result = await send(
        agent_registry, agent_name=agent_name, message=message,
        timeout=_ONCE_SEND_TIMEOUT,
    )
    return result if isinstance(result, dict) else {"reply": result or ""}


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
    # #187: scoped file grant for the agent, symmetric with `reyn run
    # --grant-file-write` (run.py:85). Grants file.read/file.write at the
    # resolver layer; the effective scope is bounded by the sandbox write_paths
    # ∩ (the env-backend's repo/workspace zone), so a non-interactive / scripted
    # agent can edit a working tree without a permission prompt but cannot escape
    # it. General capability (any chat session), not skill-specific — unlike a
    # skill run, a chat agent has no skill declaring `file.read`, so the flag
    # grants both read and write (mirrors the eval path, eval_benchmark.py:742).
    p.add_argument(
        "--grant-file-write",
        dest="grant_file_write",
        action="store_true",
        help=(
            "Grant file.read/file.write at the resolver layer for this session, "
            "scoped to the sandbox write zone. For non-interactive / scripted "
            "agent runs that edit a working tree without a permission prompt."
        ),
    )
    # #187: hide tools from the agent's LLM-visible catalog (general — any chat
    # session can scope out tools; uses the existing RouterLoop exclude_tools hook).
    # The faithful SWE-eval excludes web so the agent solves from the repo + issue,
    # not a web lookup of the gold solution.
    p.add_argument(
        "--exclude-tools", dest="exclude_tools", default=None, metavar="NAMES",
        help=(
            "Comma-separated tool names to hide from the agent's LLM-visible "
            "catalog (e.g. 'web__search,web__fetch'). The tools still exist; they "
            "are just not offered to the model this session."
        ),
    )
    # #1667: hide whole catalog CATEGORIES at the universal-catalog source
    # (orthogonal to --exclude-tools, which is top-level tool names). The
    # external-repo eval path (SWE-bench on /testbed) passes 'reyn_source' so
    # Reyn's own self-help surface doesn't compete with file__* for the weak
    # model. Empty (the interactive default) keeps every category.
    p.add_argument(
        "--exclude-categories", dest="exclude_categories", default=None,
        metavar="NAMES",
        help=(
            "Comma-separated catalog category names to hide from the agent's "
            "catalog at the source (e.g. 'reyn_source' for an external-repo task "
            "where Reyn's own source is irrelevant). Distinct from --exclude-tools "
            "(top-level tool names); this drops the whole category from "
            "list_actions + every scheme's action list + dispatch."
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
    # `reyn web` server. When set, the local Session / AgentRegistry
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
    # #1289: per-frontend container-chat — same --env-backend surface as `reyn run`.
    register_env_backend_args(p)
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

    Skips local Session / AgentRegistry / state restore. The TUI
    streams frames from ``ws://<host>[:port]/ws/chat/<agent_name>``;
    user input is forwarded to the server as ``user_message`` frames.

    Right-panel features that need local file access (events / memory
    / pending) surface "remote — limited" placeholders via each tab's
    existing remote-mode handling (e.g. PR #280 Pending tab
    ``remote_mode=True``). Phase C-(a) future iteration via REST
    will lift the limit; v1 takes the scoped-disable path per the
    #276 owner decision.
    """
    from reyn.interfaces.tui.app import run_tui
    from reyn.interfaces.tui.ws_client import connect as ws_connect
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


def _setup_pre_tui_logging(project_root: Path) -> None:
    """Route root-logger output to .reyn/logs/reyn.log before TUI launches.

    Textual owns the terminal during a TUI session. Any log record that reaches
    a StreamHandler(stderr) would be written directly to the raw terminal,
    corrupting the Textual display. This is called once, before load_project_context
    (which may emit WARNING-level records), so the file handler is in place before
    the first log call.
    """
    import logging
    log_dir = project_root / ".reyn" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "reyn.log"),
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # safe: TUI path has no prior logging setup
    )


def run(args: argparse.Namespace) -> None:
    # Issue #276 Phase A — TUI thin client mode short-circuits before
    # any local session / state setup. Bifurcates at the top of run()
    # so the local-mode block stays untouched (= backwards-compat 100%).
    connect_url = getattr(args, "connect", None)
    if connect_url:
        _run_connect_mode(args, connect_url)
        return

    from reyn.chat.profile import AgentProfile
    from reyn.chat.registry import DEFAULT_AGENT_NAME, AgentRegistry
    from reyn.chat.scoped_session_factory import build_scoped_chat_session
    from reyn.config import _find_project_root, load_project_context
    from reyn.core.events.state_log import StateLog
    from reyn.interfaces.repl.repl import run_repl
    from reyn.runtime.budget.budget import BudgetTracker
    from reyn.security.permissions.permissions import PermissionResolver

    session_cfg = InvocationContext.from_args(args)
    from reyn.interfaces.cli.credentials_check import verify_credentials_or_exit
    verify_credentials_or_exit(session_cfg, args)
    # ``model`` (= tier key like "standard" / "strong") drives Session's
    # ModelResolver. ``resolved_model`` (= the litellm string like
    # "openai/gemini-2.5-flash-lite") is what the header should surface so
    # the user can see which model their requests actually go to.
    model, resolved_model = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    safety = session_cfg.safety_for(args)

    project_root = _find_project_root(Path.cwd()) or Path.cwd()

    # Redirect root-logger to file before config load so pre-TUI log records
    # (e.g. _reconcile_embedding_class warnings) don't leak to the raw terminal.
    if not getattr(args, "cui", False) and sys.stdin.isatty():
        _setup_pre_tui_logging(project_root)

    # PR-resume-ux β U3: handle --reset before constructing state_log so
    # we don't open a freshly-written WAL just to delete it.
    if getattr(args, "reset", False):
        proceeded = _reset_project_state(project_root, confirm=True)
        if not proceeded:
            print("Reset aborted.", file=sys.stderr)
            sys.exit(0)
        print("State reset. Starting with empty session.", file=sys.stderr)

    # PR21: process-shared WAL for crash recovery. Owned by AgentRegistry,
    # injected into each Session at construction.
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
    # #187: --grant-file-write grants file.read/write at the resolver layer
    # (mirrors `reyn run` run.py:126 + the eval swe_bench path
    # eval_benchmark.py:742). The grant is bounded by the sandbox write_paths ∩
    # (env-backend repo zone), so the effective scope is the working tree, not
    # global. setdefault preserves any explicit operator setting.
    if getattr(args, "grant_file_write", False):
        perm_config.setdefault("file.read", "allow")
        perm_config.setdefault("file.write", "allow")
    unsafe_python = bool(getattr(args, "allow_unsafe_python", False))
    # #187: parse --exclude-tools (comma-separated tool names) → frozenset, threaded
    # to Session → the MAIN RouterLoop's exclude_tools (LLM-visible catalog filter).
    _exclude_tools = frozenset(
        t.strip() for t in (getattr(args, "exclude_tools", None) or "").split(",") if t.strip()
    )
    # #1667: parse --exclude-categories (comma-separated category names) → frozenset,
    # threaded to Session → RouterCallerState.excluded_categories → the universal
    # catalog skips them at the source. Empty (interactive default) keeps every category.
    _excluded_categories = frozenset(
        c.strip() for c in (getattr(args, "exclude_categories", None) or "").split(",") if c.strip()
    )
    # #1414: the single PermissionResolver is constructed BELOW, after
    # build_environment_backend, because it needs ``ws_base_dir`` for the
    # container file-zone anchor (file_zone_root). It isn't used before then.

    project_context = load_project_context(session_cfg.config, project_root)

    # #1289: build the agent-level EnvironmentBackend (host / docker attach|launch)
    # and pass the SAME instance to BOTH Session seams (FS environment_backend
    # + exec sandbox_backend) — the #1200 single-shared-sandbox invariant. A
    # launched container is torn down at process exit.
    env_backend, ws_base_dir, ws_state_dir, env_cleanup = build_environment_backend(args)
    if env_cleanup is not None:
        import atexit
        atexit.register(env_cleanup)

    # Single PermissionResolver shared across agents (per the PR10 decision:
    # `.reyn/approvals.yaml` is process-wide). #1414: the default file
    # read/write zone anchors on ``ws_base_dir`` (the container repo root under a
    # container backend) so a non-grant write into the container repo's own
    # `.reyn`/`reyn` default zone is permitted; approvals.yaml stays host-side
    # (``project_root``). ws_base_dir is None for a host backend → file_zone_root
    # defaults to project_root (host / interactive byte-identical).
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        file_zone_root=ws_base_dir,
        interactive=sys.stdin.isatty(),
        unsafe_python_allowed=unsafe_python,
    )

    def _session_factory(profile: AgentProfile):
        # Captured CLI defaults — registry doesn't need to know them.
        s = build_scoped_chat_session(
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
            reasoning_config=session_cfg.config.chat.reasoning,  # #1652
            registry=registry,  # back-reference for :agents / :attach + PR11 messaging
            allowed_skills=profile.allowed_skills,
            allowed_mcp=profile.allowed_mcp,
            events_config=session_cfg.config.events,
            state_log=state_log,
            budget_tracker=budget_tracker,
            sandbox_config=session_cfg.config.sandbox,
            multimodal_config=session_cfg.config.multimodal,
            tool_calls_op_loop_skills=session_cfg.config.tool_calls_op_loop_skills,
            action_retrieval_config=session_cfg.config.action_retrieval,
            chat_tool_use_scheme=session_cfg.config.tool_use.chat,  # #1593 PR-2
            embedding_config=session_cfg.config.embedding,
            eager_embedding_build=getattr(args, "eager_embedding_build", False),
            agent_id=session_cfg.config.agent.id,  # FP-0016 E
            exclude_tools=_exclude_tools,  # #187: hide tools (e.g. web) from the LLM catalog
            excluded_categories=_excluded_categories,  # #1667: hide categories (e.g. reyn_source) at the catalog source
            # #187: per-message tool-call budget. Interactive chat uses
            # safety.loop.max_router_iterations (default 5); the one-shot
            # autonomous path raises it via --max-iterations (CLI wins).
            router_max_iterations=int(
                getattr(args, "max_iterations", None)
                or session_cfg.config.safety.loop.max_router_iterations
            ),
            # #1439 Fix #1: run-once pipes stdin (no TTY) → the SP proceeds with an
            # assumption instead of asking a clarifying question nobody can answer
            # (13398). Interactive `reyn chat` (TTY) → False = byte-identical. Same
            # isatty() signal already feeds perm_resolver (interactive=) + use_tui.
            non_interactive=not sys.stdin.isatty(),
            # #1289: same backend instance to both seams (single-shared-sandbox).
            environment_backend=env_backend,
            sandbox_backend=env_backend,
            # #187: forward the env-backend's PARTNER container repo root + host-side
            # state dir to the chat OpContext Workspace, so file__read/grep/glob/edit
            # root on the container repo (e.g. /testbed) — not the host reyn cwd.
            # Without this the agent's file ops + the exec/diff seam disagree on the
            # FS (the #187 step-3 wrong-FS defect). None (host backend) → cwd default.
            workspace_base_dir=ws_base_dir,
            workspace_state_dir=ws_state_dir,
        )
        # #187 session-isolation: a fresh/stateless run (`reyn run-once`) does NOT
        # rehydrate the agent's persisted conversation history. `load_history()` is
        # the sole rehydration path (mcp_server.py:15-16); skipping it starts the
        # one-shot with an empty history. Otherwise a one-shot would inherit the
        # `default` agent's stale history (unrelated prior context → the agent
        # recalled an old skill + hallucinated a fix with 0 edits). Interactive
        # chat (no `fresh`) loads history as before. Scoping (env/exclude/grant) is
        # independent of history, so it is unaffected.
        if not getattr(args, "fresh", False):
            s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
        environment_backend=env_backend,   # #1544: container shadow-git runs via this
        workspace_state_dir=ws_state_dir,  # #1557 gap-#1: shadow git-dir under --state-dir
        workspace_capture=session_cfg.config.time_travel.workspace_capture,  # #1582 opt-out
        act_turn_capture=session_cfg.config.time_travel.act_turn_capture,  # #1560 opt-in
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
    from reyn.core.events.agent_snapshot import SchemaVersionError

    async def _safe_restore() -> bool:
        """Returns True on success, False if the operator should retry."""
        try:
            await registry.restore_all()
            return True
        except SchemaVersionError as e:
            print(f"\nSchema version mismatch: {e}\n", file=sys.stderr)
            return False

    if use_tui:
        from reyn.interfaces.tui.app import run_tui

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
        from reyn.interfaces.repl.repl import run_repl

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
            # #187: one-shot mode (`reyn run-once`). The scoped session built above
            # (grant / exclude_tools / env_backend / high router_max_iterations) is
            # now ATTACHED in the registry. Instead of the line-by-line REPL, read
            # the WHOLE stdin as a single user message and drive the agent to
            # completion via send_to_agent_impl — the same programmatic drive MCP /
            # A2A use (registry.get_or_load returns this attached scoped session, no
            # fresh unscoped build), then print the final reply and exit.
            if getattr(args, "once", False):
                _once_result = await _run_once(registry, name)
                sys.stdout.write((_once_result.get("reply", "") or "") + "\n")
                # #1649: a limit-abort must propagate a non-zero exit so a
                # non-TTY wrapper/CI detects the runaway-stop (vs a clean reply).
                # The decision-enabling message is already in the reply above
                # (never silent). exit(2) distinguishes it from arg/usage errors.
                if _once_result.get("limit_stopped"):
                    sys.exit(2)
                return
            await run_repl(registry, renderer=renderer)

        run_async(_main_cui())
