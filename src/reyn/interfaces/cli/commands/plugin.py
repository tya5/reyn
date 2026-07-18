"""`reyn plugin` — install/uninstall a self-contained plugin bundle (ADR 0064 §3.9, P3).

Subcommands
-----------
install builtin <NAME>   Install one of reyn's own shipped plugins (src/reyn/builtin/plugins/<NAME>/).
install local <PATH>     Promote/install a local plugin directory (the primary author→test→promote loop).
install git <URL>        Install a plugin from a remote git URL (highest RCE trust risk — §3.10 item 3).
uninstall <NAME>         Remove a previously installed plugin (registry entries + the ~/.reyn/plugins/ copy).

This is a THIN adapter over the SAME typed op the LLM tool / slash surfaces use
(ADR 0064 §3.9: "each a thin adapter over the same typed op — surfaces never
re-implement the logic"). It builds a real ``ToolContext`` and calls
``invoke_tool(get_default_registry(), "plugin_management__install"/"__uninstall", ...)``
— the SAME lookup+dispatch a live chat-router LLM tool call uses (mirrors
``RouterLoop._dispatch_registry_tool``) — so the composite permission decl
(``require_file_write`` on ``~/.reyn/plugins/`` + ``require_http_get``) and the
``{kind:git}`` run-code trust gate (``require_plugin_git_run_code_trust``,
fail-closed when non-interactive) live in exactly ONE place
(``tools/plugin_management_verbs.py``) rather than being re-derived here.

The typed ``kind`` discriminator (§3.8) is carried by the CLI SUBCOMMAND
(``install builtin|local|git``), never a form-sniffed string — mirrors how
``reyn mcp install`` distinguishes ``--source`` specifier forms structurally,
and how the LLM tool's ``source`` schema is a discriminated ``oneOf``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


def _resolve_project_root(project_arg: "str | None") -> Path:
    """Resolve the target project root: ``--project`` overrides; else the
    closest ``reyn.yaml`` ancestor of cwd. Fail loud rather than silently
    writing into (or reading a workspace rooted at) a non-project cwd —
    mirrors ``mcp.py``'s ``_resolve_install_project_root``."""
    from reyn.config import _find_project_root

    if project_arg:
        project_root = Path(project_arg).resolve()
    else:
        found = _find_project_root(Path.cwd())
        if found is None:
            print(
                "error: no reyn.yaml found from the current directory; pass "
                "--project <path-to-project-root>.",
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


def _build_plugin_cli_tool_context(project_root: Path, *, interactive: bool) -> Any:
    """Build a real, standalone ``ToolContext`` for the ``reyn plugin`` CLI —
    routed through the SAME seam a live chat-router LLM tool call uses
    (``invoke_tool`` → ``ToolDefinition.handler`` → ``build_legacy_op_context``),
    just without a live router loop behind it (mirrors
    ``pipe.py::_build_run_tool_context``).

    ``interactive`` controls BOTH the ``PermissionResolver``'s prompt
    capability AND whether a live ``StdinInterventionBus`` is wired into the
    ``router_state.op_context_factory``-built ``OpContext`` — when False
    (``--non-interactive`` or no tty), the factory hands the op handler
    ``intervention_bus=None``, so ``require_plugin_git_run_code_trust``
    (§3.10 item 3) fails closed for ``{kind:git}`` exactly as it would for
    any other non-interactive caller (bus is None OR resolver.interactive
    is False — either alone is enough to deny).
    """
    from reyn.config import load_config
    from reyn.core.events.events import EventLog
    from reyn.data.workspace.workspace import Workspace
    from reyn.security.permissions.permissions import PermissionResolver
    from reyn.tools.types import RouterCallerState, ToolContext
    from reyn.user_intervention import StdinInterventionBus

    try:
        config = load_config()
        perm_config = dict(getattr(config, "permissions", {}) or {})
    except Exception:
        perm_config = {}

    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=project_root,
        file_zone_root=project_root,
        interactive=interactive,
    )
    events = EventLog()
    workspace = Workspace(
        events=events,
        permission_resolver=perm_resolver,
        actor="plugin_management_cli",
        base_dir=project_root,
    )
    bus = StdinInterventionBus() if interactive else None

    def _op_context_factory() -> Any:
        from reyn.core.op_runtime.context import OpContext
        from reyn.security.permissions.permissions import PermissionDecl

        return OpContext(
            workspace=workspace,
            events=events,
            # Overwritten by the handler (plugin_management_verbs.py) with the
            # real composite decl before the op handler runs — this empty
            # default is never what actually gates the install/uninstall.
            permission_decl=PermissionDecl(),
            permission_resolver=perm_resolver,
            actor="plugin_management_cli",
            intervention_bus=bus,
        )

    router_state = RouterCallerState(op_context_factory=_op_context_factory)
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


async def _invoke_plugin_tool(name: str, args: dict, ctx: Any) -> dict:
    from reyn.tools import get_default_registry
    from reyn.tools.dispatch import invoke_tool

    return await invoke_tool(get_default_registry(), name, args, ctx)


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


def register(sub) -> None:
    p = sub.add_parser(
        "plugin",
        help="Install/uninstall a self-contained reyn plugin bundle (ADR 0064)",
    )
    psub = p.add_subparsers(dest="plugin_command", metavar="<subcommand>")
    psub.required = True

    # ---- install ----
    install = psub.add_parser(
        "install",
        help="Install/promote a plugin (typed source kind: builtin/local/git)",
    )
    isub = install.add_subparsers(dest="kind", metavar="<kind>")
    isub.required = True

    def _add_install_common(kind_parser) -> None:
        kind_parser.add_argument(
            "--name",
            dest="install_name",
            default=None,
            metavar="INSTALL_NAME",
            help=(
                "Override the install directory / registry-provenance name "
                "(default: the manifest's own declared name)."
            ),
        )
        kind_parser.add_argument(
            "--project", dest="project", default=None, metavar="PATH",
            help=(
                "Project root containing reyn.yaml. Defaults to the closest "
                "ancestor with a reyn.yaml, or the current directory."
            ),
        )
        kind_parser.add_argument(
            "--non-interactive",
            dest="non_interactive",
            action="store_true",
            help=(
                "Suppress interactive prompts (for CI use). A {kind:git} "
                "install ALWAYS fails closed non-interactively (ADR 0064 "
                "§3.10 item 3 — the run-code trust decision cannot be "
                "pre-made)."
            ),
        )
        kind_parser.set_defaults(func=run_install)

    builtin = isub.add_parser(
        "builtin", help="Install one of reyn's own shipped plugins",
    )
    builtin.add_argument(
        "source_name", metavar="NAME",
        help="reyn's own shipped plugin name (src/reyn/builtin/plugins/<NAME>/).",
    )
    _add_install_common(builtin)

    local = isub.add_parser(
        "local", help="Promote/install a local plugin directory you authored/tested",
    )
    local.add_argument(
        "source_name", metavar="PATH",
        help="Local plugin directory (the author→test→promote loop's working copy).",
    )
    _add_install_common(local)

    git = isub.add_parser(
        "git", help="Install a plugin from a remote git URL (highest RCE trust risk)",
    )
    git.add_argument(
        "source_name", metavar="URL",
        help="Remote git URL. Requires a live interactive run-code trust approval.",
    )
    _add_install_common(git)

    # ---- uninstall ----
    uninstall = psub.add_parser(
        "uninstall",
        help="Uninstall a plugin (drop registry entries + remove the ~/.reyn/plugins/ copy)",
    )
    uninstall.add_argument(
        "name", metavar="NAME",
        help="The plugin's install name (the name plugin install used/returned).",
    )
    uninstall.add_argument(
        "--project", dest="project", default=None, metavar="PATH",
        help=(
            "Project root containing reyn.yaml. Defaults to the closest "
            "ancestor with a reyn.yaml, or the current directory."
        ),
    )
    uninstall.set_defaults(func=run_uninstall)


# ---------------------------------------------------------------------------
# run_install / run_uninstall
# ---------------------------------------------------------------------------


def run_install(args: argparse.Namespace) -> None:
    kind = args.kind
    source_name: str = args.source_name.strip()
    if not source_name:
        print("error: source value must not be empty.", file=sys.stderr)
        sys.exit(1)

    if kind == "builtin":
        source: dict = {"kind": "builtin", "name": source_name}
    elif kind == "local":
        source = {"kind": "local", "path": source_name}
    elif kind == "git":
        source = {"kind": "git", "url": source_name}
    else:  # pragma: no cover — argparse restricts kind to the 3 subparsers above
        print(f"error: unknown source kind {kind!r}", file=sys.stderr)
        sys.exit(1)

    project_root = _resolve_project_root(getattr(args, "project", None))
    non_interactive: bool = getattr(args, "non_interactive", False)
    interactive = not non_interactive and sys.stdin.isatty()

    tool_args: dict = {"source": source}
    install_name = getattr(args, "install_name", None)
    if install_name:
        tool_args["name"] = install_name.strip()

    ctx = _build_plugin_cli_tool_context(project_root, interactive=interactive)

    print(f"Installing plugin (kind={kind}): {source_name}")
    try:
        result = asyncio.run(
            _invoke_plugin_tool("plugin_management__install", tool_args, ctx),
        )
    except PermissionError as exc:
        print(f"\nPermission denied: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"\nError during plugin install: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.get("status") != "ok":
        err = result.get("data", {}).get("error", result.get("error", "(unknown error)"))
        print(f"=== plugin install failed: {err} ===", file=sys.stderr)
        sys.exit(2)

    data = result.get("data", {})
    if isinstance(data, dict) and data.get("status") == "error":
        print(f"=== plugin install failed: {data.get('error', '(unknown error)')} ===",
              file=sys.stderr)
        sys.exit(2)

    print()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def run_uninstall(args: argparse.Namespace) -> None:
    name = args.name.strip()
    if not name:
        print("error: NAME must not be empty.", file=sys.stderr)
        sys.exit(1)

    project_root = _resolve_project_root(getattr(args, "project", None))
    # Uninstall never needs the git run-code trust gate (it only deletes),
    # so it is safe to run fully non-interactively — no --non-interactive
    # flag is exposed; the CLI is inherently a non-attended-safe path here.
    ctx = _build_plugin_cli_tool_context(project_root, interactive=sys.stdin.isatty())

    print(f"Uninstalling plugin: {name}")
    try:
        result = asyncio.run(
            _invoke_plugin_tool("plugin_management__uninstall", {"name": name}, ctx),
        )
    except PermissionError as exc:
        print(f"\nPermission denied: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"\nError during plugin uninstall: {exc}", file=sys.stderr)
        sys.exit(1)

    if result.get("status") != "ok":
        err = result.get("data", {}).get("error", result.get("error", "(unknown error)"))
        print(f"=== plugin uninstall failed: {err} ===", file=sys.stderr)
        sys.exit(2)

    data = result.get("data", {})
    if isinstance(data, dict) and data.get("status") == "error":
        print(f"=== plugin uninstall failed: {data.get('error', '(unknown error)')} ===",
              file=sys.stderr)
        sys.exit(2)

    print()
    print(json.dumps(result, indent=2, ensure_ascii=False))
