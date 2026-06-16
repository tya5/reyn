"""`reyn source` — manage indexed sources (ADR-0033 Phase 1).

Subcommands:
  list      — List all indexed sources with metadata
  describe  — Show detailed info about a single source
  rm        — Remove a source (destructive, prompts for confirmation)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Module-level import so tests can monkeypatch via
# "reyn.interfaces.cli.commands.source.get_source_manifest" and
# "reyn.interfaces.cli.commands.source._get_workspace_root".
from reyn.index.source_manifest import get_source_manifest  # noqa: E402


def register(sub: argparse._SubParsersAction) -> None:
    """Wire `reyn source ...` into the top-level CLI parser."""
    parser = sub.add_parser(
        "source",
        help="Manage indexed sources (ADR-0033 RAG)",
        description="Manage indexed sources for the recall tool.",
    )
    ssub = parser.add_subparsers(dest="source_action", metavar="<subcommand>")
    ssub.required = True
    parser.set_defaults(func=_no_subcommand)

    # ---- list ----
    list_p = ssub.add_parser("list", help="List all indexed sources")
    list_p.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Output as JSON",
    )
    list_p.set_defaults(func=cmd_list)

    # ---- describe ----
    describe_p = ssub.add_parser("describe", help="Show source details")
    describe_p.add_argument("name", help="Source name")
    describe_p.set_defaults(func=cmd_describe)

    # ---- rm ----
    rm_p = ssub.add_parser("rm", help="Remove an indexed source (destructive)")
    rm_p.add_argument("name", help="Source name to remove")
    rm_p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help=(
            "Skip CLI confirmation prompt AND pre-approve the index_drop "
            "permission gate. Use for scripted / piped invocations."
        ),
    )
    rm_p.set_defaults(func=cmd_rm)


def _no_subcommand(args: argparse.Namespace) -> None:  # pragma: no cover
    print(
        "Usage: reyn source <subcommand>  (list | describe | rm)",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> None:
    """`reyn source list` — show all indexed sources."""
    exit_code = asyncio.run(_cmd_list_async(args))
    if exit_code != 0:
        sys.exit(exit_code)


async def _cmd_list_async(args: argparse.Namespace) -> int:
    workspace = _get_workspace_root()
    manifest = get_source_manifest(workspace)
    sources = await manifest.get_all()

    if getattr(args, "as_json", False):
        import json
        payload = {name: entry.to_dict() for name, entry in sources.items()}
        print(json.dumps(payload, indent=2))
        return 0

    if not sources:
        print(
            "No indexed sources. Run:\n"
            '  reyn run index_docs \'{"source":"<name>","path":"<glob>",'
            '"description":"<text>"}\''
        )
        return 0

    for name, entry in sorted(sources.items()):
        chunk_str = f"{entry.chunk_count:>6} chunks"
        model_str = (entry.embedding_model or "?")[:32]
        last_str = entry.last_indexed or "?"
        print(f"{name:<24}  {chunk_str}  {model_str:<32}  {last_str}")
        print(f"  {entry.description}")
    return 0


# ---------------------------------------------------------------------------
# describe
# ---------------------------------------------------------------------------


def cmd_describe(args: argparse.Namespace) -> None:
    """`reyn source describe <name>` — show source details."""
    exit_code = asyncio.run(_cmd_describe_async(args))
    if exit_code != 0:
        sys.exit(exit_code)


async def _cmd_describe_async(args: argparse.Namespace) -> int:
    workspace = _get_workspace_root()
    manifest = get_source_manifest(workspace)
    entry = await manifest.get(args.name)

    if entry is None:
        print(f"Source '{args.name}' not found", file=sys.stderr)
        return 1

    print(f"Name:             {entry.name}")
    print(f"Description:      {entry.description}")
    print(f"Path:             {entry.path}")
    print(f"Backend:          {entry.backend}")
    print(f"Chunks indexed:   {entry.chunk_count}")
    print(f"Embedding model:  {entry.embedding_model or '(unset)'}")
    print(f"Last indexed:     {entry.last_indexed or '(never)'}")
    return 0


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def cmd_rm(args: argparse.Namespace) -> None:
    """`reyn source rm <name>` — remove an indexed source."""
    exit_code = asyncio.run(_cmd_rm_async(args))
    if exit_code != 0:
        sys.exit(exit_code)


async def _cmd_rm_async(args: argparse.Namespace) -> int:
    workspace_root = _get_workspace_root()
    manifest = get_source_manifest(workspace_root)
    entry = await manifest.get(args.name)

    if entry is None:
        print(f"Source '{args.name}' not found", file=sys.stderr)
        return 1

    # Confirm unless --yes / -y supplied.
    if not getattr(args, "yes", False):
        print(
            f"This will permanently delete source '{args.name}'"
            f" ({entry.chunk_count} chunks)."
        )
        try:
            resp = input("Continue? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", file=sys.stderr)
            return 1
        if resp != "y":
            print("Aborted.")
            return 1

    # Dispatch index_drop via op_runtime — permission gate, audit event (P6).
    # Mirrors _run_install_from_source in mcp.py: minimal OpContext for CLI.
    import reyn.op_runtime as _op_runtime
    from reyn.events.events import EventLog
    from reyn.op_runtime.context import OpContext
    from reyn.schemas.models import IndexDropIROp
    from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
    from reyn.user_intervention import StdinInterventionBus

    events = EventLog()
    workspace_obj = _make_cli_workspace(workspace_root)
    bus = StdinInterventionBus()

    try:
        from reyn.config import load_config
        config = load_config()
        perm_config = dict(getattr(config, "permissions", {}) or {})
    except Exception:
        perm_config = {}

    # When --yes / -y is supplied OR REYN_INDEX_DROP_AUTO_APPROVE=1 is set,
    # treat index_drop as pre-approved at the config level so the
    # permission resolver does not fall through to ask_user / interactive
    # prompt (= which auto-denies under non-TTY stdin and previously broke
    # `reyn source rm --yes` in scripted / piped use). The user has
    # explicitly opted in to the destructive op via the flag.
    import os as _os
    if getattr(args, "yes", False) or _os.environ.get(
        "REYN_INDEX_DROP_AUTO_APPROVE"
    ) == "1":
        perm_config["index_drop"] = "allow"

    perm_resolver = PermissionResolver(
        config_permissions=perm_config,
        project_root=_find_project_root_safe(workspace_root),
        interactive=sys.stdin.isatty(),
        unsafe_python_allowed=False,
    )

    # #571 collapse arc Phase 5: explicit list axis replaces the
    # former index_drop bool axis. CLI is the operator-trusted entry
    # point; session-approve the canonical manifest path up-front.
    canonical_manifest = ".reyn/index/sources.yaml"
    perm_resolver.session_approve_path(
        canonical_manifest, "reyn_source_rm", "file.write",
    )
    decl = PermissionDecl(
        file_write=[{"path": canonical_manifest, "scope": "just_path"}],
    )
    ctx = OpContext(
        workspace=workspace_obj,
        events=events,
        permission_decl=decl,
        permission_resolver=perm_resolver,
        skill_name="reyn_source_rm",
        intervention_bus=bus,
    )

    op = IndexDropIROp(kind="index_drop", source=args.name)
    result = await _op_runtime.execute_op(op, ctx, caller="control_ir")

    if result.get("status") in ("error", "denied"):
        err = result.get("error", "(unknown error)")
        print(f"Failed to remove source '{args.name}': {err}", file=sys.stderr)
        if result.get("status") == "denied":
            print(
                "Hint: pass --yes / -y to pre-approve the index_drop "
                "permission, or set permissions.index_drop: allow in "
                "reyn.yaml.",
                file=sys.stderr,
            )
        return 1

    chunks_dropped = result.get("chunks_dropped", 0)
    print(
        f"Removed: {chunks_dropped} chunks dropped from source '{args.name}'."
    )
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_workspace_root() -> Path:
    """Return the workspace root (closest ancestor with reyn.yaml, or cwd)."""
    try:
        from reyn.config import _find_project_root
        found = _find_project_root(Path.cwd())
        if found is not None:
            return found
    except Exception:
        pass
    return Path.cwd()


def _find_project_root_safe(fallback: Path) -> Path | None:
    """Return project root or None (never raises)."""
    try:
        from reyn.config import _find_project_root
        return _find_project_root(fallback)
    except Exception:
        return None


def _make_cli_workspace(workspace_root: Path):
    """Build the workspace object that OpContext expects.

    The index_drop handler accesses ``workspace.base_dir`` to locate the
    SQLite backend under ``.reyn/index/<source>/index.db``. We use the real
    Workspace class when possible, falling back to a minimal duck-typed
    object for hermetic test environments.
    """
    try:
        import os

        from reyn.workspace.workspace import Workspace

        old_cwd = Path.cwd()
        os.chdir(workspace_root)
        try:
            ws = Workspace()
        finally:
            os.chdir(old_cwd)
        return ws
    except Exception:
        # Minimal duck-typed fallback.
        class _MinimalWorkspace:
            def __init__(self, root: Path) -> None:
                self.base_dir = root

        return _MinimalWorkspace(workspace_root)
