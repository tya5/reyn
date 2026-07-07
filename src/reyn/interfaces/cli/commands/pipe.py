"""`reyn pipe` — manage and run registered pipelines directly, outside a live
chat session.

Subcommands
-----------
list       List configured pipelines (``pipelines.entries``) with LOAD STATUS.
install    Install a pipeline (local *.yaml or git/URL source) into config.
run        Execute a registered pipeline to completion, print the result.

Mirrors ``reyn.interfaces.cli.commands.mcp``'s conventions closely: the same
``register(sub)``/``run_*(args)`` shape, the same ``--project`` root
resolution, and (for ``install``) the same PermissionResolver/EventLog/
StdinInterventionBus OpContext-bridging pattern ``mcp install`` uses — just
targeting ``pipeline_install.handle`` instead of ``mcp_install.handle`` and
the canonical ``.reyn/config/pipelines.yaml`` path instead of
``.reyn/config/mcp.yaml``.

``run`` scope decision (see ``run_run``'s docstring for the full rationale):
a CLI invocation is one-shot/foreground/single-process, so it uses
``PipelineExecutor().run(...)`` directly rather than the live-session
``run_pipeline`` tool's crash-recoverable driver-session/MessageBus-attach
machinery (IS-6) — that machinery exists to let a run survive a PROCESS crash
and resume from another turn; a CLI command that dies mid-run is just "the
command failed", the same as any other CLI tool, with no recovery
expectation.

Corrected scope (was: ``tool``/``agent`` steps refused outright — see #2643's
PR history): a pipeline ``ToolStep``'s real dispatch only needs a real
``ToolContext`` (``reyn.tools.types.ToolContext`` — a dataclass whose
``router_state``/``resolver``/``hot_reloader``/``state_log`` fields are
documented to gracefully degrade to ``None``, NOT an all-or-nothing live-
session requirement), and an ``AgentStep`` only needs a real ``AgentRegistry``
capable of ``spawn_session_recorded(mode="ephemeral")`` + one ``MessageBus``
turn (``reyn.runtime.session_api.run_agent_step`` — the lightweight
ephemeral-session-spawn primitive, NOT a live chat session or router loop).
Both are constructible standalone: ``reyn.runtime.registry_bootstrap.
build_agent_registry_from_project`` extracts the reusable core of ``reyn
chat``'s own ``AgentRegistry`` construction for exactly this. So ``run_run``
now builds a real (host-backend, non-interactive) ``ToolContext`` +
``AgentRegistry`` and wires both into the executor — a pipeline built from
ANY step kind (``transform``/``tool``/``agent``/``call``/``match``/``fold``/
``for_each``/``parallel``) runs standalone. Permissions are **fail-closed by
default** (byte-identical to ``reyn chat``'s own no-flag posture) — a
``--grant-file-write`` flag, same name/semantics as ``reyn chat``'s, opts a
SPECIFIC invocation into file.read/file.write; ``http.get`` is never
blanket-granted (see ``_build_run_tool_context``'s docstring). This matters
because a pipeline may be installed from an untrusted source (``reyn pipe
install --source``) — it must not silently gain broad file/network access
merely by being RUN.

Bug fix (was: ``router_state=None`` unconditionally): a bare ``ToolContext``
with no ``router_state`` silently drops every resource-backed universal-
catalog category — ``mcp``, ``agents``, ``available_skills``, ``rag_corpus``,
``sandbox_backend`` (documented as "caveat-1" in ``runtime/router_loop.py``'s
``UniversalCategoryScheme.catalog_entries``) — so a ``tool:`` step calling
``mcp__<server>__<tool>`` could never resolve. ``run_run`` now builds
``router_state`` via ``reyn.tools.types.build_resource_caller_state``, fed
the ``default`` identity's own Session's ``RouterHostAdapter`` (every real
``Session`` builds one unconditionally in ``__init__``, live chat loop or
not) — the exact same machinery the async pipeline driver-session's tool-step
dispatch already uses (``runtime/services/pipeline_executor_driver.py``).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_MAX_DESC = 50


def _get_project_root() -> "Path | None":
    from reyn.config import _find_project_root
    return _find_project_root(Path.cwd())


def _resolve_install_project_root(project_arg: "str | None") -> Path:
    """Resolve the install target project root, fail loud.

    Mirrors ``mcp.py``'s ``_resolve_install_project_root``: ``--project``
    overrides; otherwise the closest ``reyn.yaml`` ancestor of cwd. Exits
    with an actionable message when neither yields a project root, rather
    than silently writing into a non-project cwd.
    """
    from reyn.config import _find_project_root

    if project_arg:
        project_root = Path(project_arg).resolve()
    else:
        found = _find_project_root(Path.cwd())
        if found is None:
            print(
                "error: no reyn.yaml found from the current directory; pass "
                "--project <path-to-project-root>. (pipe install writes the "
                "pipeline config under the project's .reyn/ — it will not "
                "guess a non-project cwd.)",
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


def _trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(sub) -> None:
    p = sub.add_parser(
        "pipe",
        help="Manage and run registered pipelines directly (outside a live chat session)",
    )
    psub = p.add_subparsers(dest="pipe_command", metavar="<subcommand>")
    psub.required = True

    # ---- list ----
    lst = psub.add_parser(
        "list",
        help="List configured pipelines and their load status",
    )
    lst.set_defaults(func=run_list)

    # ---- install ----
    install = psub.add_parser(
        "install",
        help="Install a pipeline into Reyn configuration",
    )
    install.add_argument(
        "--path",
        dest="path",
        default=None,
        metavar="PATH",
        help=(
            "Local pipeline DSL *.yaml file. Required when --source is not "
            "given; when --source IS given, selects the DSL file inside the "
            "cloned repo (only needed if the repo/subdir has more than one "
            "*.yaml candidate)."
        ),
    )
    install.add_argument(
        "--source",
        dest="source",
        default=None,
        metavar="SOURCE",
        help=(
            "Install from a git/GitHub URL, cloned to .reyn/pipelines/<name>/. "
            "Supports a '//' subdir suffix, e.g. "
            "'https://github.com/user/repo//pipelines/my-pipeline'."
        ),
    )
    install.add_argument(
        "--name",
        dest="name",
        default=None,
        metavar="NAME",
        help=(
            "Optional override — must match the DSL's declared 'pipeline:' "
            "name exactly, or the install is refused (the declared name is "
            "always the identity a call/match step resolves against)."
        ),
    )
    install.add_argument(
        "--project", dest="project", default=None, metavar="PATH",
        help=(
            "Project root containing reyn.yaml. Defaults to the closest "
            "ancestor with a reyn.yaml, or the current directory."
        ),
    )
    install.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="Suppress interactive prompts (for CI use)",
    )
    install.set_defaults(func=run_install)

    # ---- run ----
    run_p = psub.add_parser(
        "run",
        help="Run a registered pipeline to completion and print its result",
    )
    run_p.add_argument(
        "name",
        metavar="NAME",
        help="Registered pipeline name (its declared 'pipeline:' name)",
    )
    run_p.add_argument(
        "--input",
        dest="input",
        default="{}",
        metavar="JSON",
        help="A JSON object string seeding the run's named stores (ctx.*). Default: {}",
    )
    run_p.add_argument(
        "--project", dest="project", default=None, metavar="PATH",
        help=(
            "Project root containing reyn.yaml. Defaults to the closest "
            "ancestor with a reyn.yaml, or the current directory."
        ),
    )
    run_p.add_argument(
        "--async",
        dest="async_",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    # Same flag name/semantics as `reyn chat --grant-file-write` (chat.py):
    # OFF by default (fail-closed — a pipeline installed from an untrusted
    # source must not silently gain file.read/file.write merely by being
    # RUN); the operator opts in per invocation to trust THIS run.
    run_p.add_argument(
        "--grant-file-write",
        dest="grant_file_write",
        action="store_true",
        help=(
            "Grant file.read/file.write at the resolver layer for this run, "
            "scoped to the project root. Off by default — a tool:/agent: "
            "step that touches the filesystem without this flag is denied, "
            "the same fail-closed posture 'reyn chat' has without its own "
            "--grant-file-write."
        ),
    )
    run_p.set_defaults(func=run_run)


# ---------------------------------------------------------------------------
# run_list
# ---------------------------------------------------------------------------


def run_list(args: argparse.Namespace) -> None:
    """List configured pipelines with a LOAD STATUS column.

    Unlike ``mcp list`` there is no live-server handshake concept for
    pipelines — loading IS the check, so this always does the "expensive"
    thing: it builds a real ``PipelineRegistry`` from the SAME merged
    ``pipelines.entries`` cascade every session uses
    (``reyn.config.load_config`` already implements the identical
    user-global/project/project-local/dynamic-``.reyn/config/pipelines.yaml``
    union-merge invariant ``mcp.py``'s ``_all_servers_with_scope`` hand-rolls
    for ``mcp.servers`` — see ``loader.py``'s ``pipelines`` merge branch —
    so this reuses ``load_config()`` rather than re-deriving that cascade a
    second time) and checks, per ENABLED entry, whether its declared name
    landed in the registry. An entry that is ``enabled: true`` but did NOT
    load (malformed DSL, unreadable path, config-key / declared-name
    mismatch, or a duplicate declared name — #2641's per-entry-isolation
    posture) shows FAILED here, so ``reyn pipe list`` is a first-class way
    to SEE load failures without digging through dogfood_trace/logs.
    """
    from reyn.config import load_config
    from reyn.data.pipelines.registry import build_pipeline_registry

    project_root = _get_project_root()
    if project_root is None:
        print(
            "No reyn.yaml found from the current directory — nothing to list. "
            "Run from inside a Reyn project, or pass a project root via --project "
            "to other 'reyn pipe' subcommands."
        )
        return

    config = load_config()
    raw_entries = (config.pipelines or {}).get("entries") or {}

    if not raw_entries:
        print("No pipelines configured.")
        print(
            "Add one with: reyn pipe install --path <file.yaml>  "
            "or edit reyn.yaml manually."
        )
        return

    registry = build_pipeline_registry(config.pipelines, project_root, strict=False)
    loaded_names = set(registry.names())

    rows: list[tuple[str, str, str, str, str]] = []
    for key, raw in sorted(raw_entries.items()):
        if not isinstance(raw, dict):
            rows.append((key, "(malformed entry)", "", "?", "FAILED"))
            continue
        path = str(raw.get("path") or "")
        description = str(raw.get("description") or "")
        enabled = bool(raw.get("enabled", True))
        if not enabled:
            status = "disabled"
        elif key in loaded_names:
            status = "loaded"
        else:
            status = "FAILED"
        rows.append((key, path, description, "yes" if enabled else "no", status))

    _W_NAME = max((len(r[0]) for r in rows), default=4)
    _W_NAME = max(_W_NAME, 4)
    _W_PATH = max((len(r[1]) for r in rows), default=4)
    _W_PATH = min(max(_W_PATH, 4), 60)
    _W_ENABLED = 7
    _W_STATUS = 12

    header = (
        f"{'NAME':<{_W_NAME}}  {'PATH':<{_W_PATH}}  "
        f"{'DESCRIPTION':<{_MAX_DESC}}  {'ENABLED':<{_W_ENABLED}}  LOAD STATUS"
    )
    print(header)
    print("─" * len(header))
    for name, path, description, enabled, status in rows:
        print(
            f"{name:<{_W_NAME}}  {_trunc(path, _W_PATH):<{_W_PATH}}  "
            f"{_trunc(description, _MAX_DESC):<{_MAX_DESC}}  "
            f"{enabled:<{_W_ENABLED}}  {status}"
        )


# ---------------------------------------------------------------------------
# run_install
# ---------------------------------------------------------------------------


def run_install(args: argparse.Namespace) -> None:
    """Install a pipeline from ``--path`` (local) or ``--source`` (git/URL).

    Bridges into ``pipeline_install.handle`` via the exact same
    PermissionResolver/PermissionDecl/EventLog/StdinInterventionBus/synthetic-
    workspace OpContext pattern ``mcp.py``'s ``_run_install_from_source``
    uses — the CLI is an operator-trusted entry point (a human explicitly
    running a local command, not an LLM-driven turn), so it session-approves
    the canonical config path up-front exactly like ``mcp install`` does for
    ``.reyn/config/mcp.yaml``, just targeting ``.reyn/config/pipelines.yaml``.
    """
    path: "str | None" = getattr(args, "path", None)
    source: "str | None" = getattr(args, "source", None)
    name: "str | None" = getattr(args, "name", None)
    non_interactive: bool = getattr(args, "non_interactive", False)

    if not path and not source:
        print(
            "Error: provide --path <local pipeline DSL file> or "
            "--source <git/URL specifier> (or both — --source clones a repo, "
            "--path then selects the DSL file inside it).",
            file=sys.stderr,
        )
        sys.exit(1)

    project_root = _resolve_install_project_root(getattr(args, "project", None))

    import asyncio as _asyncio

    from reyn.core.events.events import EventLog
    from reyn.core.op_runtime.context import OpContext
    from reyn.core.op_runtime.pipeline_install import handle as _pipeline_install_handle
    from reyn.core.op_runtime.skill_install import _parse_source_spec, _source_host
    from reyn.schemas.models import PipelineInstallIROp
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.user_intervention import StdinInterventionBus

    perm_config: dict = {}
    try:
        from reyn.config import load_config
        perm_config = getattr(load_config(), "permissions", {}) or {}
    except Exception:
        perm_config = {}

    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        interactive=not non_interactive and sys.stdin.isatty(),
    )

    events = EventLog()
    workspace = type("Workspace", (), {"base_dir": str(project_root)})()
    bus = StdinInterventionBus()

    canonical_config = ".reyn/config/pipelines.yaml"
    perm_resolver.session_approve_path(
        canonical_config, "pipeline_install_cli", "file.write",
    )

    host: "str | None" = None
    if source:
        git_url, _subdir = _parse_source_spec(source)
        host = _source_host(git_url)

    decl = PermissionDecl(
        file_write=[{"path": canonical_config, "scope": "just_path"}],
        http_get=[{"host": host}] if host else [],
    )

    ctx = OpContext(
        workspace=workspace,
        events=events,
        permission_decl=decl,
        permission_resolver=perm_resolver,
        actor="pipeline_install_cli",
        intervention_bus=bus,
    )

    op = PipelineInstallIROp(
        kind="pipeline_install",
        path=path or "",
        name=name,
        source=source,
    )

    print(f"Installing pipeline from {'source: ' + source if source else 'path: ' + str(path)}")
    print()

    try:
        result = _asyncio.run(_pipeline_install_handle(op, ctx))
    except PermissionError as exc:
        print(f"\nPermission denied: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"\nError during pipeline_install: {exc}", file=sys.stderr)
        sys.exit(1)

    status = result.get("status")
    if status != "installed":
        err = result.get("error", "(unknown error)")
        print(f"=== pipeline_install {status}: {err} ===", file=sys.stderr)
        sys.exit(2)

    print(f"Pipeline '{result.get('name')}' installed successfully.")
    print(f"Config written to: {result.get('config_path')}")
    print(
        "Hot-reload requested: the installed pipeline will go live in any "
        "running 'reyn chat'/'reyn web' session at its next turn boundary "
        "(no restart required for a live session; a brand-new 'reyn chat' "
        "or 'reyn pipe run' picks it up immediately)."
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# run_run
# ---------------------------------------------------------------------------


def _build_run_tool_context(
    project_root: Path, router_state: "Any | None", *, grant_file_write: bool = False,
):
    """Build a real, standalone ``ToolContext`` for ``reyn pipe run``'s
    ``tool:`` step dispatch — routed through the SAME seam a live agent
    session's ``ToolStep`` uses (``_make_tool_dispatch`` /
    ``resolve_invoke_action`` / the unified ``ToolRegistry``), just without a
    live router loop behind it.

    Field-by-field:
      - ``events``: a real ``EventLog`` (mirrors ``reyn pipe install``).
      - ``permission_resolver``: **fail-closed by default** — ``perm_config``
        is exactly ``reyn.yaml``'s own ``permissions:`` section, byte-
        identical to ``reyn chat``'s own no-flag posture. ``grant_file_write``
        (``--grant-file-write``, off by default) mirrors ``reyn chat
        --grant-file-write`` exactly (``file.read``/``file.write`` only).
        ``http.get`` is NEVER blanket-granted — ``reyn chat`` doesn't either
        (it relies on ``require_http_get``'s interactive JIT prompt; a
        non-interactive caller with no prompt to answer is correctly
        denied, same as a non-interactive ``reyn chat``). A pipeline
        installed from an untrusted source (``reyn pipe install --source``)
        must not silently gain broad file/network access merely by being
        RUN — the operator opts in per invocation.
      - ``workspace``: a real ``reyn.data.workspace.Workspace`` anchored on
        ``project_root`` (host backend) — a real tool handler (``read_file``,
        ``write_file``, …) calls real methods on it (``read_file_bytes`` etc.),
        so a synthetic ``base_dir``-only stand-in (fine for ``pipeline_install``,
        which never dispatches an arbitrary tool) is not enough here.
      - ``caller_kind="router"``: the type's ONLY literal value — an audit
        taxonomy label forwarded verbatim into ``tool_called``/
        ``tool_returned`` events (``core/dispatch/dispatcher.py``), not a
        claim that a live router loop is driving this call. Every existing
        caller (including the non-interactive pipeline driver-session,
        ``services/pipeline_executor_driver.py``) already sets this same
        literal.
      - ``router_state``: the caller-supplied ``RouterCallerState`` (built via
        ``reyn.tools.types.build_resource_caller_state`` from a real Session's
        ``RouterHostAdapter`` — see ``run_run``) so ``mcp``/``agents``/
        ``available_skills``/``rag_corpus``/``sandbox_backend`` catalog
        categories resolve exactly like a live ``reyn chat`` turn's. A tool
        handler that specifically needs loop-local fields this state does NOT
        carry (chain_id, budget, dispatch callbacks — e.g. ``run_pipeline``'s
        own tool, structurally denied inside a pipeline step anyway — R6 S3)
        raises its own clear error — an honest, narrow limitation, not a
        silent gap.
      - ``resolver``/``hot_reloader``/``state_log``: ``None`` — each is
        documented (``tools/types.py``) to gracefully degrade when absent.
    """
    from reyn.core.events.events import EventLog
    from reyn.data.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools.types import ToolContext

    perm_config: dict = {}
    try:
        from reyn.config import load_config
        perm_config = dict(getattr(load_config(), "permissions", {}) or {})
    except Exception:
        perm_config = {}
    if grant_file_write:
        perm_config.setdefault("file.read", "allow")
        perm_config.setdefault("file.write", "allow")
    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        file_zone_root=project_root,
        interactive=False,
    )
    events = EventLog()
    workspace = Workspace(
        events=events,
        permission_resolver=perm_resolver,
        actor="pipeline_run_cli",
        base_dir=project_root,
    )
    return ToolContext(
        events=events,
        permission_resolver=perm_resolver,
        workspace=workspace,
        caller_kind="router",
        router_state=router_state,
        resolver=None,
        hot_reloader=None,
        state_log=None,
    )


def run_run(args: argparse.Namespace) -> None:
    """Run a registered pipeline to completion via ``PipelineExecutor().run()``
    directly, and print its final result as JSON.

    This is deliberately NOT the live-session ``run_pipeline`` tool's
    crash-recoverable driver-session/MessageBus-attach path (IS-6) — that
    machinery exists so a pipeline run can survive a live multi-session
    RUNTIME process crashing mid-run and be resumed/delivered on a later
    turn. A ``reyn pipe run`` invocation is a one-shot, foreground, single
    -process CLI command: if it dies mid-run, that is exactly like any other
    CLI command dying mid-run — the user's terminal command failed, with
    no "resume on the next chat turn" expectation to honor. So this loads
    the config, builds a real ``PipelineRegistry``, and calls the executor
    directly with ``state_log=None`` (an established, already-used pattern —
    see e.g. ``tests/test_2575_pipeline_disk_registration.py`` — meaning no
    R4 recovery snapshot is written; a killed ``reyn pipe run`` simply is not
    resumable, matching the "just a CLI command" trust model).

    ``--input`` (default ``"{}"``) is a JSON object string that seeds the
    run's named stores (``PipelineExecutor.run``'s ``initial_context``
    param) — the FIRST step's ``ctx.*`` sees these keys, exactly like
    ``run_pipeline``'s ``input`` argument.

    Scope: every step kind (``transform``/``tool``/``agent``/``call``/
    ``match``/``fold``/``for_each``/``parallel``) runs standalone — see the
    module docstring for the corrected ``tool:``/``agent:`` scope decision.
    A ``tool:`` step dispatches through a real, standalone ``ToolContext``
    (:func:`_build_run_tool_context`); an ``agent:`` step spawns a real
    ephemeral session under the ``default`` agent identity via a real,
    standalone ``AgentRegistry`` (``registry_bootstrap.
    build_agent_registry_from_project``).

    ``router_state`` (fixed — was a hardcoded ``None`` landmine, see
    ``runtime/router_loop.py``'s "caveat-1" comment): the resource-backed
    catalog categories (``mcp``, ``agents``, ``available_skills``,
    ``rag_corpus``, ``sandbox_backend``) are populated via
    ``reyn.tools.types.build_resource_caller_state``, fed the ``default``
    identity's own ``RouterHostAdapter`` (``AgentRegistry.get_or_load(
    DEFAULT_AGENT_NAME).router_host`` — every real ``Session`` builds one
    unconditionally in ``__init__``). This is the SAME machinery the async
    pipeline driver-session's tool-step dispatch already uses
    (``runtime/services/pipeline_executor_driver.py::_make_dispatch``) —
    not a new mechanism, just reused for this standalone caller too.
    """
    from reyn.core.pipeline.executor import PipelineExecutionError, PipelineExecutor
    from reyn.core.pipeline.registry import PipelineNotFoundError
    from reyn.data.pipelines.registry import build_pipeline_registry
    from reyn.runtime.registry import DEFAULT_AGENT_NAME
    from reyn.runtime.registry_bootstrap import build_agent_registry_from_project
    from reyn.tools.pipeline_verbs import _make_tool_dispatch
    from reyn.tools.types import build_resource_caller_state

    if getattr(args, "async_", False):
        print(
            "error: --async is not supported for 'reyn pipe run' — a CLI "
            "invocation is a one-shot foreground command with no "
            "fire-and-forget semantics. Omit --async and wait for the run "
            "to complete (use --input to seed it).",
            file=sys.stderr,
        )
        sys.exit(1)

    name: str = args.name.strip()
    if not name:
        print("Error: NAME must not be empty.", file=sys.stderr)
        sys.exit(1)

    raw_input: str = getattr(args, "input", "{}") or "{}"
    try:
        seed_ctx = json.loads(raw_input)
    except json.JSONDecodeError as exc:
        print(f"Error: --input is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(seed_ctx, dict):
        print("Error: --input must be a JSON object.", file=sys.stderr)
        sys.exit(1)

    project_arg = getattr(args, "project", None)
    if project_arg:
        project_root = Path(project_arg).resolve()
    else:
        project_root = _get_project_root()
    if project_root is None:
        print(
            "error: no reyn.yaml found from the current directory; pass "
            "--project <path-to-project-root>.",
            file=sys.stderr,
        )
        sys.exit(1)

    from reyn.config import load_config
    config = load_config()
    pipeline_registry = build_pipeline_registry(config.pipelines, project_root, strict=False)

    try:
        pipeline = pipeline_registry.get(name)
        schema_registry = pipeline_registry.get_schema_registry(name)
    except PipelineNotFoundError:
        print(
            f"error: pipeline '{name}' is not registered. "
            "Run 'reyn pipe list' to see available pipelines.",
            file=sys.stderr,
        )
        sys.exit(1)

    grant_file_write = bool(getattr(args, "grant_file_write", False))
    # A real, standalone AgentRegistry (registry_bootstrap) so an
    # AgentStep can genuinely spawn+run an ephemeral session — see the module
    # docstring for the corrected tool:/agent: scope decision. It also doubles
    # as the source of a real Session (the `default` identity's own) whose
    # RouterHostAdapter feeds the tool-step ToolContext's router_state below —
    # every real Session builds one unconditionally, live chat session or not.
    agent_registry = build_agent_registry_from_project(
        project_root, config, non_interactive=True, grant_file_write=grant_file_write,
    )

    run_id = f"cli-{uuid.uuid4().hex}"
    executor = PipelineExecutor()

    async def _run() -> Any:
        try:
            # A pipeline that never reaches a 'tool:' step should not pay the
            # cost of constructing a real Session (LLM client / resolver
            # setup) just to source a router_state nobody will read — so the
            # `default` identity's Session (and the #2567 build_resource_
            # caller_state call it feeds) is resolved LAZILY, on the FIRST
            # actual tool dispatch, not unconditionally up front.
            _router_state_cache: "dict[str, Any]" = {}

            async def _resolve_router_state() -> Any:
                if "state" not in _router_state_cache:
                    # #2567's pattern, reused: build the host-derived
                    # RouterCallerState subset from a real Session's
                    # RouterHostAdapter, so mcp/agents/available_skills/
                    # rag_corpus/sandbox_backend resolve exactly like a live
                    # router turn's — NOT the hardcoded router_state=None gap
                    # documented as "caveat-1" in runtime/router_loop.py.
                    source_session = agent_registry.get_or_load(DEFAULT_AGENT_NAME)
                    _router_state_cache["state"] = await build_resource_caller_state(
                        source_session.router_host,
                    )
                return _router_state_cache["state"]

            tool_ctx = _build_run_tool_context(
                project_root, None, grant_file_write=grant_file_write,
            )
            base_dispatch = _make_tool_dispatch(tool_ctx)

            async def tool_dispatch(tool_name: str, resolved_args: dict) -> Any:
                if tool_ctx.router_state is None:
                    tool_ctx.router_state = await _resolve_router_state()
                return await base_dispatch(tool_name, resolved_args)

            return await executor.run(
                pipeline,
                seed_ctx or None,
                tool_dispatch=tool_dispatch,
                state_log=None,
                run_id=run_id,
                schema_registry=schema_registry,
                registry=agent_registry,
                default_identity=DEFAULT_AGENT_NAME,
                pipeline_registry=pipeline_registry,
            )
        finally:
            try:
                await agent_registry.shutdown()
            except Exception:  # noqa: BLE001 — best-effort teardown only
                pass

    try:
        result = asyncio.run(_run())
    except PipelineExecutionError as exc:
        print(f"error: pipeline '{name}' failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        json.dumps(
            {"pipe_data": result.pipe_data, "named_stores": result.named_stores},
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )
