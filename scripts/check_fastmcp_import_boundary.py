#!/usr/bin/env python3
"""#3698 — the enforcement half of the fastmcp import boundary (P2/P3 were
the convention half).

P2 (#4053) introduced ``src/reyn/mcp/_fastmcp_boundary.py`` as the single
seam every reyn-side ``fastmcp`` import went through, but nothing stopped a
future direct ``import fastmcp`` anywhere else in ``src/reyn/mcp/`` — the PR
said so explicitly ("this boundary is a convention, not an enforcement"),
scoped to land once P3 (#4055) removed the one remaining inheritance-based
exception (``message_handler.py``'s subclass of ``fastmcp.client.tasks.
TaskNotificationHandler``, which genuinely needed a direct import at the
time). Post-P3, the boundary's own starting population was verified zero —
``_fastmcp_boundary.py`` was the ONLY file under ``src/reyn/mcp/`` that
imported ``fastmcp`` — so this gate had nothing to grandfather.

#4302: ``_fastmcp_boundary.py`` itself no longer exists — the MCP client
stack retired its last fastmcp dependency entirely (#4282/#4299/#3698 P3),
so the seam it existed to hold has nothing left to route through it. The
invariant this gate enforces tightened as a result: not "only the boundary
module may import fastmcp" (there is no longer a module for which that is
true) but "no file under ``src/reyn/mcp/`` imports fastmcp, period" — still
a real, live check (a regression reintroducing ``import fastmcp`` anywhere
in this directory still trips it; verified by a live strip-falsify: adding
a throwaway ``import fastmcp`` file here flips this script's exit code to 1,
confirming the AST scan itself, not just the docstring, is current). Kept
scanning the whole directory rather than narrowing to zero files so a
regression is still caught, not just documented as impossible.

## Scope: ``src/reyn/mcp/`` only, not the whole repo

``src/reyn/builtin/plugins/rag/scripts/{vector_store_server,chunker_server}.py``
also import ``fastmcp`` directly (``from fastmcp import FastMCP``) — but
that is reyn's own BUNDLED MCP *server* code (building a server with
fastmcp's server framework), the opposite direction from
``MCPConnectionService``'s *client* role this boundary exists for. #3698's
own scope (measured in P2's issue comment) was always the client stack —
gating the whole repo would incorrectly flag a legitimate, unrelated use of
the package.

## Why a whole-directory static scan, not a diff/base-ref check

Same reasoning as ``check_bare_tests_import_reference.py``/
``check_file_depth_reference.py``: whether a file imports ``fastmcp``
directly is fully determined by its CURRENT content, no move or diff
needed. A pure population scan against a real, verified-zero baseline.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_MCP_DIR = _ROOT / "src" / "reyn" / "mcp"


def _imports_fastmcp_directly(path: Path) -> bool:
    """Does *path* contain a top-level ``import fastmcp`` / ``from fastmcp
    import ...`` / ``from fastmcp.<sub> import ...`` anywhere in its AST
    (module-level or deferred inside a function — both are in scope, since
    the boundary module already covers both timing patterns; see its own
    module docstring)?"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "fastmcp" or alias.name.startswith("fastmcp.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "fastmcp" or node.module.startswith("fastmcp.")):
                return True
    return False


def offending_files(mcp_dir: Path = _MCP_DIR) -> "list[Path]":
    """Every ``.py`` file directly under *mcp_dir* that imports ``fastmcp``
    directly — the gate's entire decision, isolated from CLI/printing so it
    is directly testable.

    #4302: no filename is exempt anymore. ``_fastmcp_boundary.py`` (the one
    file this used to skip) no longer exists — the client stack's last
    fastmcp dependency was retired entirely, so there is no longer a
    legitimate place under this directory for a direct ``import fastmcp``
    to live."""
    return [
        path
        for path in sorted(mcp_dir.glob("*.py"))
        if _imports_fastmcp_directly(path)
    ]


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options — a whole-directory scan against a baseline of zero
    offenders = offending_files(_MCP_DIR)

    if not offenders:
        print("OK: no file under src/reyn/mcp/ imports fastmcp directly.")
        return 0

    print("fastmcp-import-boundary gate FAILED:\n", file=sys.stderr)
    print(
        f"{len(offenders)} file(s) under src/reyn/mcp/ import fastmcp directly "
        "(#3698 P2/P3, #4302):",
        file=sys.stderr,
    )
    for path in offenders:
        print(f"  {path.relative_to(_ROOT)}", file=sys.stderr)
    print(
        "\nThe MCP client stack has no fastmcp dependency left at all "
        "(#4282/#4299/#3698 P3) — reach for the official mcp SDK directly "
        "instead of reintroducing fastmcp here.\n"
        "\nThis gate's own starting population is zero, so any hit here is "
        "a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
