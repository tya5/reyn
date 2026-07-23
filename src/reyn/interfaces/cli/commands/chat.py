"""`reyn chat [name]` — interactive chat, optionally attaching to a named agent.

PR10: launches the AgentRegistry, attaches to the named agent (or `default`),
then hands off to `run_repl`. The registry holds all loaded Session
instances; switching agents mid-REPL via `/attach <name>` happens through it.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from reyn.interfaces.cli.env_backend import (
    build_environment_backend,
    register_env_backend_args,
)

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
            "Force plain console output (no inline CUI / ANSI). "
            "Useful for piping output, scripting, debugging, or headless "
            "environments. The interactive default is the inline CUI."
        ),
    )
    # ADR-0039 P3: attach to a REMOTE single-writer server instead of running a
    # local session. The same stream-consuming client drives a different
    # transport (AG-UI over HTTP+SSE) — local ≡ remote by construction (D2).
    p.add_argument(
        "--connect",
        default=None,
        metavar="URL",
        dest="connect",
        help=(
            "Attach to a remote reyn server over AG-UI (e.g. "
            "http://127.0.0.1:8080) instead of a local session. The server is "
            "started with `reyn web`. Answers, turns, and interventions round-trip "
            "over the wire; the server remains the sole writer."
        ),
    )
    p.add_argument(
        "--token",
        default=None,
        metavar="SECRET",
        dest="token",
        help=(
            "Bearer token for --connect (the secret `reyn web` prints on launch). "
            "Falls back to REYN_WEB_AUTH_TOKEN. A same-machine UDS / loopback "
            "server may need none."
        ),
    )
    p.add_argument(
        "--no-restore",
        action="store_true",
        default=False,
        help=(
            "Skip restoring in-flight agent state from disk this run. "
            "Useful for debugging or starting a clean session without "
            "discarding the persisted state (it will be loaded on next run)."
        ),
    )
    p.add_argument(
        "--reset",
        action="store_true",
        default=False,
        help=(
            "Wipe in-flight agent state (snapshots + WAL) before starting. "
            "Audit logs in .reyn/events/ are preserved. "
            "Asks for confirmation before deleting."
        ),
    )
    # #187: scoped file grant for the agent, symmetric with `reyn run
    # --grant-file-write` (run.py:85). Grants file.read/file.write at the
    # resolver layer; the effective scope is bounded by the sandbox write_paths
    # ∩ (the env-backend's repo/workspace zone), so a non-interactive / scripted
    # agent can edit a working tree without a permission prompt but cannot escape
    # it. General capability (any chat session), not domain-specific — the chat
    # agent has no explicit `file.read` permission declaration (unlike the old
    # skill-declared permission model), so the flag grants both read and write
    # (mirrors the eval path, eval_benchmark.py:742).
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
    # external-repo eval path (SWE-bench on /testbed) passes 'reyn_repo' so
    # Reyn's own self-help surface doesn't compete with file__* for the weak
    # model. Empty (the interactive default) keeps every category.
    p.add_argument(
        "--exclude-categories", dest="exclude_categories", default=None,
        metavar="NAMES",
        help=(
            "Comma-separated catalog category names to hide from the agent's "
            "catalog at the source (e.g. 'reyn_repo' for an external-repo task "
            "where Reyn's own source is irrelevant). Distinct from --exclude-tools "
            "(top-level tool names); this drops the whole category from "
            "list_actions + every scheme's action list + dispatch."
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
    # #1289: per-frontend container-chat — same --env-backend surface as `reyn run`.
    register_env_backend_args(p)
    add_common_args(p)
    p.set_defaults(func=run)


def _reset_project_state(project_root: Path, *, confirm: bool = True) -> bool:
    """Wipe in-flight state under ``project_root/.reyn/``.

    Removes:
      - ``.reyn/state/wal.jsonl`` (process WAL)
      - ``.reyn/agents/<name>/state/snapshot.json`` (per-agent snapshots)

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
                "This will delete all in-flight state "
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

    # Delete per-agent snapshots
    agents_dir = project_root / ".reyn" / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            state_dir = agent_dir / "state"
            if not state_dir.is_dir():
                continue
            (state_dir / "snapshot.json").unlink(missing_ok=True)

    return True


def _inline_interactive(*, cui: bool, stdin_isatty: bool, stdout_isatty: bool) -> bool:
    """Whether the inline CUI is the surface for this run.

    True only when not ``--cui`` and BOTH std streams are TTYs: the inline CUI
    reads keys from stdin and renders a live region to stdout, so a piped/
    redirected stdout (``reyn chat | tee``) must fall back to the plain renderer
    rather than write cursor/ANSI escapes into the pipe. The single source for
    both the log redirect and the renderer choice so they never diverge.
    """
    return not cui and stdin_isatty and stdout_isatty


def _setup_interactive_logging(project_root: Path) -> None:
    """Route root-logger output to .reyn/logs/reyn.log for the interactive CUI.

    The inline CUI owns the terminal; a log record reaching a StreamHandler
    (stderr) would print into the live chat region — at best noise (litellm
    warnings), at worst an alarming full traceback from a caught error. Sending
    logs to a file keeps the UI clean while preserving them for debugging. Called
    once, before load_project_context (which may emit WARNING records), so the
    file handler is in place before the first log call.

    Deliberately does NOT import litellm (perf: ``import litellm`` costs
    ~1.5s and this runs on the startup path, before the input box renders).
    litellm's own log routing + ``suppress_debug_info`` is applied lazily at
    the FIRST real litellm use — see ``reyn.llm.litellm_bootstrap.
    ensure_litellm_ready`` — which reads the file handler this function
    installs (so the routing still works whenever litellm is first touched).
    """
    log_dir = project_root / ".reyn" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_dir / "reyn.log"),
        level=logging.WARNING,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,  # safe: the interactive path has no prior logging setup
    )


def _run_remote(
    args: argparse.Namespace,
    *,
    run_remote=None,
    stdin_isatty: "bool | None" = None,
    stdout_isatty: "bool | None" = None,
) -> None:
    """ADR-0039 P3: attach to a remote server over AG-UI (`--connect <url>`).

    A thin transport-only path — no local Session / registry / workspace is built
    (the server is the sole writer). The same stream-consuming client renders the
    remote frame stream and routes input back over the wire.

    Renderer selection is UNIFIED with the local path (D2, local ≡ remote): the
    SAME ``_inline_interactive`` predicate + ``make_renderer`` seam picks the Claude
    Code-style inline CUI on an interactive TTY (with the frame-available status
    bar streamed over ``STATE_*``) and the plain console renderer for ``--cui`` /
    non-TTY / piped. Before P3 this hard-coded the plain renderer, so a remote
    attach on a TTY looked nothing like local — this wiring is the fix, so it is
    guarded directly (``test_agui_remote_inline_p3``).

    ``run_remote`` / ``stdin_isatty`` / ``stdout_isatty`` are injectable seams (same
    recording-double-not-mock pattern as ``_run_once``): a test drives THIS function
    with forced TTY flags and a recording ``run_remote`` to pin that the real
    call-site selects the inline renderer, so a revert to a hard-coded renderer goes
    RED. Production leaves them None → the real ``run_remote_repl`` + ``sys`` TTYs.
    """
    import os

    from reyn.llm.llm import run_async

    from ..logger_factory import make_renderer

    if run_remote is None:
        from reyn.interfaces.repl.remote_client import run_remote_repl
        run_remote = run_remote_repl
    if stdin_isatty is None:
        stdin_isatty = sys.stdin.isatty()
    if stdout_isatty is None:
        stdout_isatty = sys.stdout.isatty()

    agent_name = args.agent_name or "default"
    token = getattr(args, "token", None) or os.environ.get("REYN_WEB_AUTH_TOKEN")
    # Same predicate as the local path: inline CUI only when not --cui and BOTH
    # std streams are TTYs (the inline live region needs a TTY stdout).
    is_interactive = _inline_interactive(
        cui=getattr(args, "cui", False),
        stdin_isatty=stdin_isatty,
        stdout_isatty=stdout_isatty,
    )
    # The inline CUI owns the terminal; route library warnings / tracebacks to a
    # log file so they don't corrupt the live region (same rationale as local).
    if is_interactive:
        from reyn.config import _find_project_root
        _setup_interactive_logging(_find_project_root(Path.cwd()) or Path.cwd())
    renderer = make_renderer(is_interactive)
    run_async(
        run_remote(
            base_url=args.connect,
            agent_name=agent_name,
            token=token,
            renderer=renderer,
        )
    )


def run(args: argparse.Namespace) -> None:
    # ADR-0039 P3: `--connect <url>` short-circuits to the remote thin client
    # before any local session machinery is built (the remote server owns the
    # session; this process is pure I/O).
    if getattr(args, "connect", None):
        _run_remote(args)
        return

    from reyn.config import _find_project_root, load_project_context
    from reyn.interfaces.repl.repl import run_repl
    from reyn.llm.llm import run_async
    from reyn.runtime.factory_config import SessionFactoryConfig
    from reyn.runtime.presentation_consumer import OutboxPresentationConsumer
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import DEFAULT_AGENT_NAME, AgentRegistry
    from reyn.runtime.registry_bootstrap import build_budget_tracker, build_state_log
    from reyn.runtime.scoped_session_factory import build_scoped_chat_session
    from reyn.security.permissions.permissions import PermissionResolver

    # Compute the interactive-CUI gate and install the log redirect BEFORE any
    # config load, so config-time WARNING records (malformed reyn.yaml, embedding-
    # class reconciliation, …) are captured into the log file instead of leaking
    # into the chat UI — the same class as #2208, closing the remaining
    # pre-redirect window. project_root + is_interactive have no config dependency.
    project_root = _find_project_root(Path.cwd()) or Path.cwd()
    # The inline CUI owns the terminal as a live region, which needs BOTH a TTY
    # stdin (to read keys) AND a TTY stdout (to render the bottom live region).
    # With stdout piped/redirected (e.g. `reyn chat | tee`) the prompt_toolkit
    # Application would write cursor/ANSI escapes into the pipe, so fall back to
    # the plain renderer there. This single predicate gates BOTH the log redirect
    # and the renderer choice (below) so "inline CUI active ⟺ logging redirected"
    # stays invariant — they must not diverge.
    is_interactive = _inline_interactive(
        cui=getattr(args, "cui", False),
        stdin_isatty=sys.stdin.isatty(),
        stdout_isatty=sys.stdout.isatty(),
    )
    # Route the root logger to a file so library warnings and caught-exception
    # tracebacks (e.g. an LLM APIConnectionError that session.py logs via
    # logger.exception) don't leak into — and corrupt/alarm — the chat UI.
    # --cui / non-TTY keep logging on stderr (debuggable / pipeable). (Restores
    # the redirect the Textual TUI had; dropped in the inline-CUI cutover #2195.)
    if is_interactive:
        _setup_interactive_logging(project_root)

    session_cfg = InvocationContext.from_args(args)
    # #2708 P3.2b: the missing-cred pre-check moved OFF this per-surface startup
    # gate and ONTO the single LLM funnel (``recorded_acompletion``). It now
    # fires on the FIRST LLM call (early for any LLM run) and surfaces as a typed
    # ``MissingCredentialsError`` rendered by the CLI error boundary (main()).
    # ``model`` (= tier key like "standard" / "strong") drives Session's
    # ModelResolver. ``resolved_model`` (= the litellm string like
    # "openai/gemini-2.5-flash-lite") is what the header should surface so
    # the user can see which model their requests actually go to.
    model, resolved_model = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    safety = session_cfg.safety_for(args)

    # PR-resume-ux β U3: handle --reset before constructing state_log so
    # we don't open a freshly-written WAL just to delete it.
    if getattr(args, "reset", False):
        proceeded = _reset_project_state(project_root, confirm=True)
        if not proceeded:
            print("Reset aborted.", file=sys.stderr)
            sys.exit(0)
        print("State reset. Starting with empty session.", file=sys.stderr)

    # PR21: process-shared WAL for crash recovery. Owned by AgentRegistry,
    # injected into each Session at construction. Extracted to
    # registry_bootstrap.build_state_log (byte-identical) so reyn pipe run's
    # AgentRegistry construction can't silently drift from this one.
    state_log = build_state_log(project_root)
    # PR22: process-shared budget tracker. Defaults to all unlimited unless
    # `cost:` is configured. PR25: hydrated from the persistent ledger.
    # R-D8: in-memory counters (per-agent / per-sub-agent) restored from the
    # state snapshot written by the previous run — together with PR25 ledger
    # hydration, cap enforcement survives crash + restart across the
    # session's multi-turn lifetime. Extracted to
    # registry_bootstrap.build_budget_tracker (byte-identical, hydrate=True
    # is the default — same as before).
    budget_tracker = build_budget_tracker(session_cfg.config.cost, project_root)
    perm_config = getattr(session_cfg.config, "permissions", {}) or {}
    # #187: --grant-file-write grants file.read/write at the resolver layer
    # (mirrors `reyn run` run.py:126 + the eval swe_bench path
    # eval_benchmark.py:742). The grant is bounded by the sandbox write_paths ∩
    # (env-backend repo zone), so the effective scope is the working tree, not
    # global. setdefault preserves any explicit operator setting.
    if getattr(args, "grant_file_write", False):
        perm_config.setdefault("file.read", "allow")
        perm_config.setdefault("file.write", "allow")
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
    )

    def _session_factory(profile: AgentProfile, *, presentation_consumer=None, intervention_bridge=None):
        # Captured CLI defaults — registry doesn't need to know them.
        # #1827 S3: resolve the agent's topology capability_profile → contextual
        # narrowing (enforcement) + view exclusion. (None, ∅) when unbound = byte-identical.
        _ctx_perm, _profile_excluded = registry.resolved_profile_for(profile.name)
        s = build_scoped_chat_session(
            # #2708 P1: chat-CLI (inline/plain --cui / run-once) — the InlineChatRenderer
            # drains the outbox "presentation" message and renders it (renderer.py:148),
            # so the outbox-backed consumer is byte-identical to the pre-#2708 default.
            # #2708 P3.1: a spawn override (the attached pipeline driver's parent-bound
            # SpawnBridgePresentationConsumer) wins when supplied; None = this default.
            presentation_consumer=presentation_consumer or OutboxPresentationConsumer(),
            # #2708 P3.2a: forward the attached pipeline driver's intervention bridge
            # (SpawnBridgeInterventionListener) so a driver ask_user reaches this live
            # operator; None (default / non-spawn) = self-bound fail-closed, byte-identical.
            intervention_bridge=intervention_bridge,
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
            allowed_mcp=profile.allowed_mcp,
            events_config=session_cfg.config.events,
            cost_warn_config=session_cfg.config.cost_warn,  # #2230: wire the warn/block gate
            offload_config=session_cfg.config.offload,  # tool-result-schema-redesign §5
            render_template_config=session_cfg.config.render_template,  # #2679: render_template output bounds
            state_log=state_log,
            budget_tracker=budget_tracker,
            hooks_config=session_cfg.config.hooks,  # #1800 slice 5b (pass-through, not bundled)
            composers_config=session_cfg.config.composers,  # Hook-Event Redesign Phase 4b/5 (pass-through, not bundled)
            fs_watch_config=session_cfg.config.fs_watch,  # #2608 H4 (pass-through, not bundled)
            # #2093: the uniform reyn.yaml-derived per-session config bundle (sandbox /
            # multimodal / action_retrieval / embedding / router / retry /
            # tool-use-scheme) — one source point for all five sites.
            factory_config=SessionFactoryConfig.from_config(session_cfg.config, project_root),
            eager_embedding_build=getattr(args, "eager_embedding_build", False),
            agent_id=session_cfg.config.agent.id,  # FP-0016 E
            exclude_tools=_exclude_tools,  # #187: hide tools (e.g. web) from the LLM catalog
            excluded_categories=frozenset(_excluded_categories or ()) | _profile_excluded,  # #1667 + #1827 S3 profile view
            contextual_permission=_ctx_perm,  # #1827 S3: capability_profile enforcement → live tool gate
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
            # isatty() signal already feeds perm_resolver (interactive=) + is_interactive.
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
        # hallucinated a fix based on prior session context with 0 edits). Interactive
        # chat (no `fresh`) loads history as before. Scoping (env/exclude/grant) is
        # independent of history, so it is unaffected.
        if not getattr(args, "fresh", False):
            s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
        # #2093: the uniform reyn.yaml-derived registry config bundle
        # (delegation_capability_default) — one source point.
        factory_config=SessionFactoryConfig.from_config(session_cfg.config, project_root),
    )

    name = args.agent_name or DEFAULT_AGENT_NAME
    if not registry.exists(name):
        print(
            f"Error: agent {name!r} not found. "
            f"Run `reyn agent new {name}` to create it (or omit the name to use 'default').",
            file=sys.stderr,
        )
        sys.exit(1)

    # is_interactive computed once above (TTY stdin AND stdout, no --cui) so the
    # renderer choice and the log redirect can never diverge.
    skip_restore = getattr(args, "no_restore", False)
    if skip_restore:
        print(
            "⚠ --no-restore: agent state on disk is NOT loaded this run. "
            "Rerun without --no-restore to resume in-flight agent sessions.",
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

    from reyn.interfaces.repl.repl import run_repl

    from ..logger_factory import make_renderer

    # Interactive TTY (no --cui) → Claude Code-style inline CUI, the default
    # human-facing surface. --cui or a non-TTY (pipe / script / run-once) →
    # plain console output. Both drive the same run_repl loop; only the
    # renderer differs. The SAME make_renderer seam picks it on the remote path.
    renderer = make_renderer(is_interactive)

    async def _main_chat() -> None:
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
            # #2783: this branch used to return (or sys.exit) with NO teardown of
            # any kind — unlike run_repl (below), which always routes through
            # registry.shutdown() on /quit/EOF. That left MCP connections,
            # FsWatcher, StateLog and EventStore all unclosed on every
            # `reyn run-once` invocation. try/finally so a limit-abort's
            # sys.exit(2) still runs teardown before the process exits.
            try:
                _once_result = await _run_once(registry, name)
                sys.stdout.write((_once_result.get("reply", "") or "") + "\n")
                # #1649: a limit-abort must propagate a non-zero exit so a
                # non-TTY wrapper/CI detects the runaway-stop (vs a clean reply).
                # The decision-enabling message is already in the reply above
                # (never silent). exit(2) distinguishes it from arg/usage errors.
                if _once_result.get("limit_stopped"):
                    sys.exit(2)
            finally:
                await registry.shutdown()
            return
        await run_repl(registry, renderer=renderer, config=session_cfg.config)

    run_async(_main_chat())
