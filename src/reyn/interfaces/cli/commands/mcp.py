"""`reyn mcp` — MCP server lifecycle management + expose Reyn to outer LLM clients.

Subcommands
-----------
serve          Expose Reyn agents to outer LLM clients via MCP (inbound, existing).
search         Search the MCP registry for servers.
install        Install an MCP server (wraps the mcp_install skill).
list           List configured MCP servers with status.
remove         Remove an MCP server from configuration.
set-secret     Set a secret for an MCP server.
clear-secret   Clear a secret (or all secrets) for an MCP server.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from reyn.llm.llm import run_async

from ..common_args import add_common_args
from ..invocation_context import InvocationContext

# ---------------------------------------------------------------------------
# Scope tier helpers
# ---------------------------------------------------------------------------

_VALID_SCOPES = ("local", "project", "user")


def _scope_path(scope: str, project_root: Path | None) -> Path:
    """Resolve the yaml file path for a given scope tier."""
    if scope == "user":
        return Path.home() / ".reyn" / "config.yaml"
    if scope == "project":
        if project_root is None:
            print(
                "error: --scope project requires a project root with reyn.yaml. "
                "Run from inside a Reyn project or use --scope local/user.",
                file=sys.stderr,
            )
            sys.exit(1)
        return project_root / "reyn.yaml"
    # "local" (default)
    if project_root is None:
        print(
            "error: --scope local requires a project root with reyn.yaml. "
            "Run from inside a Reyn project or use --scope user.",
            file=sys.stderr,
        )
        sys.exit(1)
    return project_root / "reyn.local.yaml"


def _load_yaml_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return {}


def _write_yaml_file(path: Path, data: dict) -> None:
    import yaml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True, default_flow_style=False),
                    encoding="utf-8")


def _get_project_root() -> Path | None:
    from reyn.config import _find_project_root
    return _find_project_root(Path.cwd())


def _get_servers_from_scope(scope: str, project_root: Path | None) -> dict:
    """Return mcp.servers dict from a single scope file.

    Returns an empty dict when project_root is None and scope requires it.
    """
    if scope in ("project", "local") and project_root is None:
        return {}
    path = _scope_path(scope, project_root)
    data = _load_yaml_file(path)
    return (data.get("mcp") or {}).get("servers") or {}


def _all_servers_with_scope(project_root: Path | None) -> list[tuple[str, str, dict]]:
    """Return (name, scope, server_cfg) tuples from all scope tiers, deduplicated.

    Later (higher-priority) scopes override earlier ones for the same name.
    Priority: local > project > user.  Project/local scopes are skipped when
    project_root is None (i.e. invoked outside any project directory).
    """
    merged: dict[str, tuple[str, dict]] = {}
    for scope in ("user", "project", "local"):
        if scope in ("project", "local") and project_root is None:
            continue
        path = _scope_path(scope, project_root)
        data = _load_yaml_file(path)
        servers = (data.get("mcp") or {}).get("servers") or {}
        for name, cfg in servers.items():
            merged[name] = (scope, cfg if isinstance(cfg, dict) else {})
    return [(name, scope, cfg) for name, (scope, cfg) in merged.items()]


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(sub) -> None:
    p = sub.add_parser(
        "mcp",
        help="Model Context Protocol — manage MCP servers and expose Reyn to outer clients",
    )
    msub = p.add_subparsers(dest="mcp_command", metavar="<subcommand>")
    msub.required = True

    # ---- serve (existing, unchanged) ----
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

    # ---- search ----
    search = msub.add_parser(
        "search",
        help="Search the MCP server registry",
    )
    search.add_argument(
        "query",
        metavar="QUERY",
        help="Search query (e.g. 'github', 'filesystem')",
    )
    search.set_defaults(func=run_search)

    # ---- install ----
    install = msub.add_parser(
        "install",
        help="Install an MCP server into Reyn configuration",
    )
    install.add_argument(
        "server_id",
        nargs="?",
        default=None,
        metavar="SERVER_ID",
        help=(
            "Registry server identifier (e.g. 'io.github.foo/bar-mcp'). "
            "Mutually exclusive with --source."
        ),
    )
    install.add_argument(
        "--source",
        dest="source",
        default=None,
        metavar="SOURCE",
        help=(
            "Install directly from a source specifier, skipping the registry. "
            "Supported forms: "
            "npm:<package>[@version], "
            "pypi:<package>[==version], "
            "docker:<image>[:<tag>], "
            "https://github.com/<owner>/<repo>[/tree/<ref>/...]. "
            "Mutually exclusive with SERVER_ID."
        ),
    )
    install.add_argument(
        "--project", dest="project", default=None, metavar="PATH",
        help=(
            "Project root containing reyn.yaml. Defaults to the closest "
            "ancestor with a reyn.yaml, or the current directory. #1442: "
            "without this, install resolved the target cwd-only."
        ),
    )
    install.add_argument(
        "--scope",
        choices=_VALID_SCOPES,
        default="local",
        help="Config scope to write into (default: local)",
    )
    install.add_argument(
        "--env",
        dest="env",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help="Pre-supply environment variable (may be repeated)",
    )
    install.add_argument(
        "--args",
        dest="extra_args",
        default=None,
        metavar="ARGS",
        help=(
            "Extra arguments appended to the server command after install "
            "(shell-quoted string). "
            "Example: --args \"--server pyright --language python\""
        ),
    )
    install.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="Suppress interactive prompts (for CI use)",
    )
    install.set_defaults(func=run_install)

    # ---- list ----
    lst = msub.add_parser(
        "list",
        help="List configured MCP servers and their status",
    )
    lst.add_argument(
        "--probe",
        action="store_true",
        help="Handshake with each server to verify liveness (slow; incurs API calls)",
    )
    lst.set_defaults(func=run_list)

    # ---- remove ----
    remove = msub.add_parser(
        "remove",
        help="Remove an MCP server from configuration",
    )
    remove.add_argument(
        "name",
        metavar="NAME",
        help="Server name as declared in mcp.servers.*",
    )
    remove.add_argument(
        "--scope",
        choices=_VALID_SCOPES,
        default=None,
        help=(
            "Scope tier to remove from. If omitted, removes from whichever "
            "scope the server appears in (local first, then project, then user)."
        ),
    )
    remove.set_defaults(func=run_remove)

    # ---- set-secret ----
    ss = msub.add_parser(
        "set-secret",
        help="Set a secret for an MCP server (MCP-aware thin wrapper over 'reyn secret set')",
    )
    ss.add_argument(
        "server",
        metavar="SERVER",
        help="Server name as declared in mcp.servers.*",
    )
    ss.add_argument(
        "key_value",
        metavar="KEY[=VALUE]",
        help="Secret key or KEY=VALUE pair. Value is prompted if omitted.",
    )
    ss.set_defaults(func=run_set_secret)

    # ---- clear-secret ----
    cs = msub.add_parser(
        "clear-secret",
        help="Clear a secret (or all secrets) for an MCP server",
    )
    cs.add_argument(
        "server",
        metavar="SERVER",
        help="Server name as declared in mcp.servers.*",
    )
    cs.add_argument(
        "key",
        nargs="?",
        default=None,
        metavar="KEY",
        help="Secret key to clear. If omitted, clears all secrets declared for the server.",
    )
    cs.set_defaults(func=run_clear_secret)

    # ---- refresh ----
    refresh = msub.add_parser(
        "refresh",
        help=(
            "Re-probe all configured MCP servers and write results to the "
            "persistent cache file (.reyn/state/mcp_tools_cache.json). "
            "Active 'reyn chat' sessions pick up the new cache on their "
            "next turn boundary — no restart required."
        ),
    )
    refresh.add_argument(
        "--project",
        dest="project",
        default=None,
        metavar="PATH",
        help=(
            "Project root containing reyn.yaml. "
            "Defaults to the closest ancestor with a reyn.yaml."
        ),
    )
    refresh.set_defaults(func=run_refresh)


def run_serve(args: argparse.Namespace) -> None:
    from reyn.config import _find_project_root, load_project_context
    from reyn.core.events.state_log import StateLog
    from reyn.mcp.server import serve_stdio
    from reyn.runtime.budget.budget import BudgetTracker
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry
    from reyn.runtime.scoped_session_factory import build_scoped_chat_session
    from reyn.security.permissions.permissions import PermissionResolver

    session_cfg = InvocationContext.from_args(args)
    from reyn.interfaces.cli.credentials_check import verify_credentials_or_exit
    verify_credentials_or_exit(session_cfg, args)
    model, _ = session_cfg.model_for(args)
    output_language = session_cfg.output_language_for(args)
    safety = session_cfg.safety_for(args)

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
    # function; deeper code paths (Session, Workspace, AgentRegistry)
    # also use relative paths internally. Anchor the whole process at the
    # project root so the same code that works under `reyn chat` works
    # here unchanged.
    os.chdir(project_root)

    state_log = StateLog(project_root / ".reyn" / "state" / "wal.jsonl")
    budget_tracker = BudgetTracker(session_cfg.config.cost, safety=safety)
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
        unsafe_python_allowed=False,
    )

    project_context = load_project_context(session_cfg.config, project_root)

    def _session_factory(profile: AgentProfile):
        # #1827 S3: resolve the agent's topology capability_profile (None/∅ unbound).
        _ctx_perm, _profile_excluded = registry.resolved_profile_for(profile.name)
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
            registry=registry,
            allowed_skills=profile.allowed_skills,
            allowed_mcp=profile.allowed_mcp,
            events_config=session_cfg.config.events,
            state_log=state_log,
            budget_tracker=budget_tracker,
            # Same fix as web/deps.py: A2A / MCP-serve Session factory
            # was missing ``sandbox_config`` propagation. Without it, the
            # operator's reyn.yaml ``sandbox.backend`` selection (e.g.
            # ``noop``) never reaches the sandboxed_exec handler.
            sandbox_config=session_cfg.config.sandbox,
            multimodal_config=session_cfg.config.multimodal,
            tool_calls_op_loop_skills=session_cfg.config.tool_calls_op_loop_skills,
            action_retrieval_config=session_cfg.config.action_retrieval,
            chat_tool_use_scheme=session_cfg.config.tool_use.chat,  # #1593 PR-2
            embedding_config=session_cfg.config.embedding,
            router_config=session_cfg.config.llm.router,  # #1829 S3b
            # #1402: scoped capability surface, passed EXPLICITLY (required by
            # build_scoped_chat_session). The stdio-MCP factory's current
            # behaviour — defaults that document the gaps, NOT new capabilities
            # (behavior-preserving). Fill = 1-line follow-up when a consumer needs it.
            agent_id=None,
            exclude_tools=None,
            excluded_categories=_profile_excluded,  # #1667 (none here) + #1827 S3 profile view
            contextual_permission=_ctx_perm,  # #1827 S3: capability_profile enforcement → live tool gate
            router_max_iterations=session_cfg.config.safety.loop.max_router_iterations,
            non_interactive=False,  # #1439 Fix #1: stdio-MCP byte-identical (run-once-only fix)
            environment_backend=None,  # gap: MCP-serve lacks env-backend / container-rooting
            sandbox_backend=None,
            workspace_base_dir=None,
            workspace_state_dir=None,
            eager_embedding_build=False,
        )
        s.load_history()
        return s

    registry = AgentRegistry(
        project_root=project_root,
        session_factory=_session_factory,
        state_log=state_log,
        workspace_capture=session_cfg.config.time_travel.workspace_capture,  # #1582 opt-out
        act_turn_capture=session_cfg.config.time_travel.act_turn_capture,  # #1560 opt-in
    )

    timeout = float(getattr(args, "timeout", 60.0) or 60.0)

    async def _main() -> None:
        # Replay WAL into per-agent snapshots so any stranded in-flight skills
        # resume cleanly, the same as `reyn chat` startup. Schema mismatch
        # surfaces a clean stderr line and exits non-zero.
        from reyn.core.events.agent_snapshot import SchemaVersionError
        try:
            await registry.restore_all()
        except SchemaVersionError as e:
            print(f"Schema version mismatch: {e}", file=sys.stderr)
            sys.exit(1)
        await serve_stdio(registry, timeout=timeout)

    run_async(_main())


# ---------------------------------------------------------------------------
# run_search
# ---------------------------------------------------------------------------

def run_search(args: argparse.Namespace) -> None:
    """Search the MCP registry and display results as a rich table.

    This is a thin CLI wrapper over RegistryClient.search().  No LLM or skill
    invocation is required for the discovery step.
    """
    from reyn.core.registry.client import RegistryClient, RegistryError
    from reyn.llm.llm import run_async as _run_async

    query = args.query.strip()
    if not query:
        print("Error: QUERY must not be empty.", file=sys.stderr)
        sys.exit(1)

    print(f"Searching MCP registry for: {query!r} …")

    async def _do_search():
        async with RegistryClient() as client:
            return await client.search(query)

    try:
        results = _run_async(_do_search())
    except RegistryError as exc:
        print(f"Registry error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not results:
        print("No results found.")
        return

    # Table layout: NAME / RUNTIME / DESCRIPTION / REPO
    _MAX_DESC = 60
    _MAX_NAME = 42
    _MAX_REPO = 50

    def _trunc(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    header = (
        f"{'NAME':<{_MAX_NAME}}  {'RUNTIME':<8}  "
        f"{'DESCRIPTION':<{_MAX_DESC}}  REPO"
    )
    print()
    print(header)
    print("─" * len(header))
    for server in results:
        name = _trunc(server.name, _MAX_NAME)
        runtime = server.runtime_hint or "(unknown)"
        desc = _trunc(server.description, _MAX_DESC)
        repo_raw = server.repository_url if server.repository_url else "(no repo URL)"
        repo = _trunc(repo_raw, _MAX_REPO)
        print(
            f"{name:<{_MAX_NAME}}  {runtime:<8}  {desc:<{_MAX_DESC}}  {repo}"
        )
    print()
    print(f"{len(results)} result(s). Install with: reyn mcp install <NAME>")


# ---------------------------------------------------------------------------
# run_install
# ---------------------------------------------------------------------------

def _resolve_install_project_root(project_arg: str | None) -> Path:
    """#1442 Layer A: resolve the install target project root, fail loud.

    --project overrides; otherwise the closest reyn.yaml ancestor of cwd. When
    neither yields a project (no --project and no reyn.yaml from cwd), exit with
    an actionable message rather than silently writing into a non-project cwd
    (the #1442 defect). Mirrors the `reyn mcp refresh` / `serve` resolution.
    """
    from reyn.config import _find_project_root

    if project_arg:
        project_root = Path(project_arg).resolve()
    else:
        found = _find_project_root(Path.cwd())
        if found is None:
            print(
                "error: no reyn.yaml found from the current directory; pass "
                "--project <path-to-project-root>. (mcp install writes the "
                "server config under the project's .reyn/ — it will not guess "
                "a non-project cwd.)",
                file=sys.stderr,
            )
            sys.exit(1)
        project_root = found

    if not (project_root / "reyn.yaml").exists():
        print(
            f"error: {project_root}/reyn.yaml not found. "
            "Run `reyn init` there or pass a different --project path.",
            file=sys.stderr,
        )
        sys.exit(1)
    return project_root


def run_install(args: argparse.Namespace) -> None:
    """Install an MCP server — thin wrapper over the mcp_install skill.

    Two install modes:
      - Registry mode (positional SERVER_ID): fetch server.json from
        registry.modelcontextprotocol.io, then install.  This is the
        existing behaviour.
      - Source mode (``--source SPECIFIER``): skip the registry; resolve
        metadata from the specifier directly.  Useful for servers that are
        not yet listed in the registry (e.g. Anthropic official reference
        servers).

    ``SERVER_ID`` and ``--source`` are mutually exclusive; exactly one must
    be supplied.

    When ``--non-interactive`` is set the ``REYN_MCP_INSTALL_AUTO_APPROVE``
    environment variable is injected so the skill / IR op suppress interactive
    prompts.

    ``--env KEY=VALUE`` pairs are forwarded to the skill as pre-supplied
    environment overrides so the credential-prompt flow is skipped for those
    keys.

    ``--args ARGS`` is a shell-quoted string of extra arguments appended to
    the server's args list after installation (e.g. ``--args "--server pyright"``).
    """
    import shlex

    server_id_raw: str | None = getattr(args, "server_id", None)
    source_raw: str | None = getattr(args, "source", None)

    # ── Mutual exclusivity check ──────────────────────────────────────────────
    has_server_id = server_id_raw is not None and server_id_raw.strip()
    has_source = source_raw is not None and source_raw.strip()

    if has_server_id and has_source:
        print(
            "Error: SERVER_ID and --source are mutually exclusive. "
            "Provide one or the other, not both.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not has_server_id and not has_source:
        print(
            "Error: provide either a SERVER_ID (registry install) "
            "or --source <specifier> (direct install).",
            file=sys.stderr,
        )
        sys.exit(1)

    source: str | None = source_raw.strip() if has_source else None

    extra_args_raw: str | None = getattr(args, "extra_args", None)
    extra_args: list[str] | None = shlex.split(extra_args_raw) if extra_args_raw else None

    scope = getattr(args, "scope", "local")
    if scope not in _VALID_SCOPES:
        print(f"Error: invalid --scope '{scope}'. Choose from: {', '.join(_VALID_SCOPES)}",
              file=sys.stderr)
        sys.exit(1)

    env_pairs: list[str] = getattr(args, "env", []) or []
    non_interactive: bool = getattr(args, "non_interactive", False)

    # Build a pre-supplied env dict from --env KEY=VALUE pairs.
    pre_env: dict[str, str] = {}
    for pair in env_pairs:
        if "=" not in pair:
            print(f"Error: --env value must be KEY=VALUE, got: {pair!r}", file=sys.stderr)
            sys.exit(1)
        k, _, v = pair.partition("=")
        pre_env[k.strip()] = v

    if non_interactive:
        os.environ["REYN_MCP_INSTALL_AUTO_APPROVE"] = "1"

    # #1442 Layer A: resolve the project root ONCE (install was cwd-only +
    # lacked --project, asymmetric with serve/list/refresh). --project overrides;
    # else the closest reyn.yaml from cwd. Fail loud rather than silently writing
    # into a non-project cwd. Threaded to both install paths below.
    project_root = _resolve_install_project_root(getattr(args, "project", None))

    # ── Source mode: bypass skill, go direct to IR op handler ─────────────────
    if source:
        _run_install_from_source(
            source=source,
            scope=scope,
            pre_env=pre_env,
            non_interactive=non_interactive,
            extra_args=extra_args,
            project_root=project_root,
        )
        return

    # ── Registry mode (existing path) ─────────────────────────────────────────
    server_id = (server_id_raw or "").strip()

    # Forward to mcp_install skill via reyn run machinery.
    import json

    from reyn.config import load_config, load_project_context
    from reyn.llm.llm import run_async as _run_async
    from reyn.llm.model_resolver import ModelResolver
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.skill.skill_paths import SkillNotFoundError, is_stdlib_skill
    from reyn.skill.skill_paths import resolve_skill_path as _resolve_skill_path_raw
    from reyn.skill.skill_runtime import SkillRuntime
    from reyn.user_intervention import StdinInterventionBus

    from ..logger_factory import make_logger

    config = load_config()
    # #1442 Layer A: root the agent's workspace at the resolved project_root so
    # the mcp_install op handler writes there, not cwd. Mirrors `reyn mcp
    # refresh` (os.chdir to project_root); the agent's cwd-defaulted workspace
    # then base_dir-roots at it (paired with the handler base_dir fix, Layer B).
    os.chdir(project_root)

    try:
        skill_dir, skill_root = _resolve_skill_path_raw("mcp_install")
    except SkillNotFoundError:
        print(
            "error: mcp_install skill not found.\n"
            "Note: the mcp_install skill is implemented in a parallel wave. "
            "Verify it is available before using 'reyn mcp install'.",
            file=sys.stderr,
        )
        sys.exit(1)

    from reyn.core.compiler import load_dsl_skill
    skill = load_dsl_skill(str(skill_dir / "skill.md"), skill_root=str(skill_root))

    initial_input = {
        "type": "mcp_install_request",
        "data": {
            "server_id": server_id,
            "scope": scope,
            "env_overrides": pre_env,
            "non_interactive": non_interactive,
            "extra_args": extra_args,
        },
    }

    perm_config = getattr(config, "permissions", {}) or {}
    # Stdlib skills ship with the Reyn team's code — their unsafe python steps
    # are safe by construction. Auto-allow so users are not blocked by the
    # --allow-unsafe-python gate that applies only to user-supplied skills.
    auto_trust_python = is_stdlib_skill(skill_dir)
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=not non_interactive and sys.stdin.isatty(),
        unsafe_python_allowed=auto_trust_python,
    )
    project_context = load_project_context(config, project_root)

    if config.api_base:
        os.environ.setdefault("LITELLM_API_BASE", config.api_base)
    resolver = ModelResolver(
        config.models,
        default_class=config.model,
        purpose_classes=config.model_class_by_purpose,
    )
    logger = make_logger()
    # #997 dir2: config-derived permission/runtime bundle wired by from_config.
    agent = SkillRuntime.from_config(
        config,
        resolver=resolver,
        strict=False,
        subscribers=[logger],
        intervention_bus=StdinInterventionBus(),
        project_context=project_context,
        caller="direct",
    )

    print(f"Installing MCP server: {server_id}")
    print(f"Scope: {scope}")
    if pre_env:
        print(f"Pre-supplied env keys: {', '.join(pre_env.keys())}")
    print()

    try:
        result = _run_async(
            agent.run(skill, initial_input, output_language=None)
        )
    except Exception as exc:
        print(f"\nError during mcp_install: {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    if not result.ok:
        print(f"=== mcp_install ended with status '{result.status}' ===",
              file=sys.stderr)
        sys.exit(2)

    print(f"Server '{server_id}' installed successfully.")
    print(json.dumps(result.data, indent=2, ensure_ascii=False))


def _run_install_from_source(
    source: str,
    scope: str,
    pre_env: dict[str, str],
    non_interactive: bool,
    extra_args: list[str] | None = None,
    *,
    project_root: Path,
) -> None:
    """Install an MCP server directly from a ``--source`` specifier.

    Bypasses the mcp_install skill and the registry fetch, going directly
    to the IR op handler.  The permission gate, credential flow, and config
    write are all identical to the registry path (reusing the same handler).

    #1442 Layer A: ``project_root`` is the run_install-resolved root (--project
    or the closest reyn.yaml), no longer re-derived cwd-only here.
    """
    import asyncio
    import json

    from reyn.config import load_config
    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.mcp_install import handle as _mcp_install_handle
    from reyn.schemas.models import MCPInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.user_intervention import StdinInterventionBus

    config = load_config()

    perm_config = getattr(config, "permissions", {}) or {}
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=not non_interactive and sys.stdin.isatty(),
        unsafe_python_allowed=True,  # OS-level install path, no user skill code
    )

    events = EventLog()
    # #1442 Layer B: expose ``base_dir`` (the canonical Workspace attribute the
    # handler reads — op_runtime/file.py uses ctx.workspace.base_dir), not the
    # legacy ``root`` the handler special-cased. project_root is now always set.
    workspace = type("Workspace", (), {"base_dir": str(project_root)})()
    bus = StdinInterventionBus()

    # #571 collapse arc Phase 5: explicit list axes replace the
    # former mcp_install bool axis. CLI is the operator-trusted entry
    # point, so we session-approve the canonical config path up-front.
    canonical_config = ".reyn/mcp.yaml"
    perm_resolver.session_approve_path(
        canonical_config, "mcp_install_source", "file.write",
    )
    decl = PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        http_get=[{"host": "registry.modelcontextprotocol.io"}],
        # #571 Phase 6: wildcard authorises save_secret for the
        # registry's ``isSecret`` env vars (= determined at runtime).
        secret_write=["*"],
    )

    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=perm_resolver,
        skill_name="mcp_install_source",
        intervention_bus=bus,
    )

    # server_id is empty for source installs; the resolved short name is used
    # as the config key.  We pass a synthetic id for audit clarity.
    synthetic_id = source

    op = MCPInstallIROp(
        kind="mcp_install",
        server_id=synthetic_id,
        scope=scope,  # type: ignore[arg-type]
        env_overrides=pre_env or None,
        source=source,
        extra_args=extra_args or None,
    )

    print(f"Installing MCP server from source: {source}")
    print(f"Scope: {scope}")
    if pre_env:
        print(f"Pre-supplied env keys: {', '.join(pre_env.keys())}")
    # issue #320: warn when /tmp is passed as a server arg on macOS.
    # /tmp is a symlink to /private/tmp on Darwin; the filesystem MCP
    # server (and similar path-checking servers) resolve the configured
    # root to its canonical path but compare LITERAL strings against
    # tool-call arguments → 'Access denied - path outside allowed
    # directories'. Surfacing the gotcha at install time saves a debug
    # round later. Linux unaffected.
    import platform as _platform  # noqa: PLC0415
    if extra_args and _platform.system() == "Darwin":
        tmp_args = [
            a for a in extra_args
            if isinstance(a, str) and (a == "/tmp" or a.startswith("/tmp/"))
        ]
        if tmp_args:
            print(
                "Warning: detected /tmp path(s) in --args on macOS: "
                f"{tmp_args}. /tmp is a symlink to /private/tmp; some "
                "path-checking MCP servers (e.g. filesystem) compare "
                "literal paths and will deny tool calls that reference "
                "/tmp/... while the configured root resolved to "
                "/private/tmp/... — use a non-symlink path (e.g. ./.mcp-sandbox "
                "or ~/mcp-sandbox) to avoid this.",
                file=sys.stderr,
            )
    print()

    try:
        result = asyncio.run(_mcp_install_handle(op, ctx, "control_ir"))
    except PermissionError as exc:
        print(f"\nPermission denied: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"\nError during mcp_install (source): {exc}", file=sys.stderr)
        sys.exit(1)

    print()
    if result.get("status") != "ok":
        err = result.get("error", "(unknown error)")
        print(f"=== mcp_install failed: {err} ===", file=sys.stderr)
        sys.exit(2)

    server_name = result.get("server_name", "")
    print(f"Server '{server_name}' installed successfully (source: {source}).")
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# run_list
# ---------------------------------------------------------------------------

def run_list(args: argparse.Namespace) -> None:
    """List configured MCP servers.

    Default (cheap) mode: reads yaml files only, infers STATUS from env-var
    declarations vs os.environ — no subprocess launched.

    ``--probe``: send an MCP initialize handshake to every server to verify
    liveness.  This is an explicit opt-in because it consumes API quota and
    may have audit-log side effects.
    """
    probe: bool = getattr(args, "probe", False)
    project_root = _get_project_root()

    entries = _all_servers_with_scope(project_root)

    if not entries:
        print("No MCP servers configured.")
        print(
            "Add a server with: reyn mcp install <SERVER_ID>  "
            "or edit reyn.yaml manually."
        )
        return

    # Column widths
    _W_NAME = max((len(n) for n, _, _ in entries), default=4)
    _W_NAME = max(_W_NAME, 4)
    _W_TRANSPORT = 9
    _W_STATUS = 12
    _W_CREDS = 30
    _W_SCOPE = 7

    header = (
        f"{'NAME':<{_W_NAME}}  {'TRANSPORT':<{_W_TRANSPORT}}  "
        f"{'STATUS':<{_W_STATUS}}  {'CREDENTIALS':<{_W_CREDS}}  SCOPE"
    )
    print(header)
    print("─" * len(header))

    for name, scope, cfg in sorted(entries, key=lambda t: t[0]):
        transport = _infer_transport(cfg)
        creds_display = _infer_credentials(cfg)
        status = _infer_status(cfg) if not probe else _probe_status(name, cfg)
        print(
            f"{name:<{_W_NAME}}  {transport:<{_W_TRANSPORT}}  "
            f"{status:<{_W_STATUS}}  {creds_display:<{_W_CREDS}}  {scope}"
        )


def _infer_transport(cfg: dict) -> str:
    t = cfg.get("type") or cfg.get("transport") or ""
    if t:
        return str(t)
    if cfg.get("command"):
        return "stdio"
    if cfg.get("url"):
        return "http"
    return "(unknown)"


def _infer_credentials(cfg: dict) -> str:
    """Return a short credential status string based on the env declarations."""
    env_decl: dict = cfg.get("env") or {}
    if not env_decl:
        return "(none)"
    parts: list[str] = []
    for key in env_decl:
        present = key in os.environ
        mark = "✓" if present else "✗"
        parts.append(f"{key} {mark}")
    return ", ".join(parts)


def _infer_status(cfg: dict) -> str:
    """Cheap status: 'ready' if all declared env vars are set, else 'missing-cred'."""
    env_decl: dict = cfg.get("env") or {}
    if not env_decl:
        return "ready"
    for key in env_decl:
        if key not in os.environ:
            return "missing-cred"
    return "ready"


def _probe_status(name: str, cfg: dict) -> str:
    """Active probe: attempt an MCP initialize handshake.  Returns status string."""
    from reyn.llm.llm import run_async as _run_async

    async def _handshake() -> str:
        try:
            from reyn.mcp.client import MCPClient
            client = MCPClient(name, cfg)
            async with client:
                return "ready"
        except Exception as exc:
            return f"error: {exc}"

    try:
        return _run_async(_handshake())
    except Exception as exc:
        return f"error: {exc}"


# ---------------------------------------------------------------------------
# run_remove
# ---------------------------------------------------------------------------

def run_remove(args: argparse.Namespace) -> None:
    """Remove an MCP server from the configuration file for the given scope."""
    name = args.name.strip()
    if not name:
        print("Error: NAME must not be empty.", file=sys.stderr)
        sys.exit(1)

    scope: str | None = getattr(args, "scope", None)
    project_root = _get_project_root()

    # Auto-detect scope if not specified: local → project → user.
    if scope is None:
        for candidate in ("local", "project", "user"):
            servers = _get_servers_from_scope(candidate, project_root)
            if name in servers:
                scope = candidate
                break
        if scope is None:
            print(
                f"error: server '{name}' not found in any scope tier "
                "(local / project / user).",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        # Validate explicit scope.
        if scope not in _VALID_SCOPES:
            print(f"Error: invalid --scope '{scope}'. Choose from: {', '.join(_VALID_SCOPES)}",
                  file=sys.stderr)
            sys.exit(1)
        servers = _get_servers_from_scope(scope, project_root)
        if name not in servers:
            print(
                f"error: server '{name}' not found in scope '{scope}'.",
                file=sys.stderr,
            )
            sys.exit(1)

    path = _scope_path(scope, project_root)
    data = _load_yaml_file(path)
    data.setdefault("mcp", {}).setdefault("servers", {})
    if name in data["mcp"]["servers"]:
        del data["mcp"]["servers"][name]
    if not data["mcp"]["servers"]:
        del data["mcp"]["servers"]
    if not data["mcp"]:
        del data["mcp"]
    _write_yaml_file(path, data)

    print(f"Server '{name}' removed from {path}")
    print(
        "Note: any currently running subprocess for this server will "
        "continue until the next 'reyn chat' session restart."
    )
    print(
        "Secrets are not removed automatically. "
        "Use 'reyn mcp clear-secret " + name + "' to delete associated secrets."
    )


# ---------------------------------------------------------------------------
# run_set_secret
# ---------------------------------------------------------------------------

def run_set_secret(args: argparse.Namespace) -> None:
    """Set a secret for an MCP server (MCP-aware wrapper over 'reyn secret set').

    Reads the server's ``mcp.servers.<name>.env`` declaration to validate that
    the supplied KEY is expected.  If the KEY is not in the declaration a
    warning is printed but the operation proceeds (unknown-key warning, not
    error, so user can pre-set keys for servers not yet installed).

    After writing the secret, ensures that ``mcp.servers.<name>.env.<KEY>``
    in the local scope yaml has a ``${KEY}`` reference so the value is picked
    up at runtime.
    """
    from reyn.security.secrets.store import save_secret

    server_name = args.server.strip()
    raw_kv = args.key_value.strip()

    if not server_name:
        print("Error: SERVER must not be empty.", file=sys.stderr)
        sys.exit(1)

    # Parse key[=value]
    if "=" in raw_kv:
        key, _, value = raw_kv.partition("=")
        key = key.strip()
    else:
        key = raw_kv.strip()
        value = None

    if not key:
        print("Error: KEY must not be empty.", file=sys.stderr)
        sys.exit(1)

    project_root = _get_project_root()

    # Validate against server's env declaration (warn only).
    known_keys = _server_env_keys(server_name, project_root)
    if known_keys is not None and key not in known_keys:
        print(
            f"warning: '{key}' is not declared in mcp.servers.{server_name}.env. "
            "Setting it anyway."
        )

    # Prompt for value if not supplied.
    if value is None:
        try:
            value = getpass.getpass(f"Value for {key}: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            sys.exit(1)

    save_secret(key, value)
    print(f"Secret '{key}' saved to ~/.reyn/secrets.env")

    # Ensure ${KEY} reference exists in the local scope yaml for this server.
    _ensure_env_ref(server_name, key, project_root)


def _server_env_keys(server_name: str, project_root: Path | None) -> set[str] | None:
    """Return the set of env keys declared for a server across all scopes.

    Returns None if the server is not found anywhere (so caller can skip
    the validation altogether for yet-to-be-installed servers).
    """
    found = False
    keys: set[str] = set()
    for scope in ("user", "project", "local"):
        servers = _get_servers_from_scope(scope, project_root)
        if server_name in servers:
            found = True
            env_decl = servers[server_name].get("env") or {}
            keys.update(env_decl.keys())
    return keys if found else None


def _ensure_env_ref(server_name: str, key: str, project_root: Path | None) -> None:
    """Add ``${KEY}`` reference to local scope yaml if not already present."""
    # Work in the local scope (gitignored, safe to write credentials-adjacent config).
    path = _scope_path("local", project_root)
    data = _load_yaml_file(path)
    data.setdefault("mcp", {}).setdefault("servers", {}).setdefault(server_name, {})
    server_cfg = data["mcp"]["servers"][server_name]
    server_cfg.setdefault("env", {})
    if key not in server_cfg["env"]:
        server_cfg["env"][key] = f"${{{key}}}"
        _write_yaml_file(path, data)
        print(
            f"  → Added env.{key}: ${{{key}}} reference to {path}"
        )


# ---------------------------------------------------------------------------
# run_clear_secret
# ---------------------------------------------------------------------------

def run_clear_secret(args: argparse.Namespace) -> None:
    """Clear a secret (or all secrets) for an MCP server.

    If KEY is specified, clears that single secret.
    If KEY is omitted, clears all secrets declared for the server.
    The yaml side ``${KEY}`` reference is NOT touched — server config structure
    is preserved.
    """
    from reyn.security.secrets.store import clear_secret

    server_name = args.server.strip()
    key: str | None = getattr(args, "key", None)
    if key is not None:
        key = key.strip() or None

    if not server_name:
        print("Error: SERVER must not be empty.", file=sys.stderr)
        sys.exit(1)

    project_root = _get_project_root()

    if key is not None:
        # Clear a specific key.
        removed = clear_secret(key)
        if removed:
            print(f"Secret '{key}' removed from ~/.reyn/secrets.env")
        else:
            print(f"Secret '{key}' not found in ~/.reyn/secrets.env (nothing changed)")
    else:
        # Clear all secrets declared for this server.
        known_keys = _server_env_keys(server_name, project_root)
        if not known_keys:
            print(
                f"No env declarations found for server '{server_name}'. "
                "Nothing to clear."
            )
            return
        removed_any = False
        for k in sorted(known_keys):
            removed = clear_secret(k)
            if removed:
                print(f"Secret '{k}' removed from ~/.reyn/secrets.env")
                removed_any = True
            else:
                print(f"Secret '{k}' not found in ~/.reyn/secrets.env (skipped)")
        if not removed_any:
            print("No secrets were removed.")
        else:
            print(
                "Note: yaml ${VAR} references in reyn.yaml/reyn.local.yaml "
                "are NOT removed — server config structure is preserved."
            )


# ---------------------------------------------------------------------------
# run_refresh
# ---------------------------------------------------------------------------


async def _probe_server_tools(
    server_name: str, cfg: dict, *, per_server_timeout: float = 5.0,
) -> tuple[str, list[dict]]:
    """Probe a single MCP server's tool list with a per-server timeout.

    Returns ``(server_name, tools)`` where ``tools`` is empty on failure.
    This module-level helper is the single probe implementation shared by
    ``run_refresh`` (CLI) and ``RouterHostAdapter.ensure_mcp_tools_cached``
    (session), satisfying the FP-0037 S1 "avoid code duplication" requirement.
    """
    import asyncio

    from reyn.mcp.client import MCPClient

    try:
        async with asyncio.timeout(per_server_timeout):
            async with MCPClient(server_name, cfg) as client:
                raw = await client.list_tools()
    except (TimeoutError, asyncio.TimeoutError):
        return server_name, []
    except Exception:  # noqa: BLE001
        return server_name, []
    cleaned = [
        t for t in (raw or [])
        if isinstance(t, dict) and "error" not in t and t.get("name")
    ]
    return server_name, cleaned


def run_refresh(args: argparse.Namespace) -> None:
    """Re-probe all configured MCP servers and write the persistent cache.

    Synopsis: reyn mcp refresh [--project PATH]

    Reads the MCP server config from the 3-scope yaml cascade (user-global
    / project / project-local), probes every server's tool list in parallel
    with a per-server 5-second timeout, and writes the result atomically to
    ``.reyn/state/mcp_tools_cache.json``.  Per-server failures print a
    warning and write an empty list for that server (= same broken-server
    behavior as the session-side lazy probe).

    Active ``reyn chat`` sessions will pick up the new cache on their next
    turn boundary via ``maybe_reload_mcp_tools_cache_from_disk``.
    """
    import asyncio

    from reyn.config import _find_project_root
    from reyn.runtime.services.mcp_cache_file import cache_file_path, write_cache

    if args.project:
        project_root: Path | None = Path(args.project).resolve()
    else:
        project_root = _get_project_root()

    entries = _all_servers_with_scope(project_root)

    if not entries:
        # Still write an empty cache so sessions see a "refresh happened" mtime.
        state_dir = (
            project_root / ".reyn" / "state"
            if project_root is not None
            else Path(".reyn") / "state"
        )
        cache_path = cache_file_path(state_dir)
        write_cache(cache_path, {})
        print(f"Probed 0 servers; wrote 0 tool entries to {cache_path}")
        return

    # Build a flat {name: cfg} dict (local > project > user dedup already done).
    servers_flat = {name: cfg for name, _scope, cfg in entries}

    async def _probe_all() -> dict[str, list[dict]]:
        tasks = [
            _probe_server_tools(name, cfg)
            for name, cfg in servers_flat.items()
        ]
        results = await asyncio.gather(*tasks)
        return dict(results)

    results = asyncio.run(_probe_all())

    # Warn on failures (= empty result for servers that had content before).
    total_tools = 0
    warned = False
    for server_name, tools in results.items():
        if tools:
            total_tools += len(tools)
        else:
            # Only warn if the server has a non-trivial config (= might have tools).
            cfg = servers_flat.get(server_name, {})
            if cfg:
                print(
                    f"warning: {server_name}: probe failed or returned no tools "
                    "(timeout / connection error — writing empty list)",
                    file=sys.stderr,
                )
                warned = True

    state_dir = (
        project_root / ".reyn" / "state"
        if project_root is not None
        else Path(".reyn") / "state"
    )
    cache_path = cache_file_path(state_dir)
    write_cache(cache_path, results)

    n_servers = len(results)
    print(f"Probed {n_servers} server(s); wrote {total_tools} tool entries to {cache_path}")
    if warned:
        print(
            "One or more servers failed. Active sessions will see an empty tool list "
            "for those servers. Re-run 'reyn mcp refresh' after fixing the issue.",
            file=sys.stderr,
        )
