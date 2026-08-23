#!/usr/bin/env python3
"""#5177 — enforces that ``src/reyn/security/permissions/approval_ledger.py``
never imports a reyn-internal module.

``src/reyn/api/safe/file.py`` runs inside the python-harness SUBPROCESS and
deliberately stays self-contained (see that module's own
``_project_root_for_gate`` docstring — it does not depend on the rest of the
``reyn`` package being importable there). #5173 made it
``from reyn.security.permissions import approval_ledger`` for the shared
``RELATIVE_PATH`` constant, which is safe ONLY because ``approval_ledger.py``
itself has zero reyn-internal imports (stdlib only: ``json``, ``os``,
``tempfile``, ``time``, ``pathlib``, ``typing``, plus a deferred third-party
``import yaml`` inside one function).

lead-coder's question during the #5173 review: "who keeps that true
forever?" Answer at the time: nobody — a future edit adding a single
reyn-internal import to ``approval_ledger.py`` (a logger helper, a config
type, anything) would silently widen what the subprocess-safe
``api/safe/file.py`` pulls in, with nothing catching it until the sandboxed
subprocess is actually exercised somewhere more restrictive than a dev
machine. This gate closes that: a structural, low-FP AST check — the same
shape ``scripts/check_fastmcp_import_boundary.py`` (#3698) already
established for an analogous "this file/directory must not import X"
invariant — scanning ONE file, not a directory, since the constraint this
issue names is specific to ``approval_ledger.py`` itself (its sibling
``permissions.py`` legitimately imports reyn internals; only the module
``api/safe/file.py`` reaches into matters here).

## Why AST, not a substring search

A prose mention of ``reyn.security`` in this module's own docstring (this
file's history/rationale writing, or a future one in ``approval_ledger.py``
itself) must not trip a substring-based check — only a REAL ``import``/
``from ... import`` statement naming a ``reyn.*`` module counts. AST parsing
is what makes this a low-FP structural gate rather than a grep with the
usual comment/docstring false-positive risk (the same discriminator
``check_doc_drift.py``'s own comment/docstring redaction exists for, #5010).

## Scope: one file, both module-level and deferred imports

Both a module-level ``import reyn.foo`` and a deferred (function-local)
``import reyn.foo`` are in scope — a regression could reach for either
shape, and ``approval_ledger.py``'s own existing deferred ``import yaml``
(third-party, not reyn) shows deferred imports are already a real pattern
in this file, not a hypothetical one to leave uncovered.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_APPROVAL_LEDGER_PATH = (
    _ROOT / "src" / "reyn" / "security" / "permissions" / "approval_ledger.py"
)


def reyn_internal_imports(path: Path) -> "list[str]":
    """Every reyn-internal module name *path* imports (module-level or
    deferred, anywhere in its AST) — ``[]`` when the file has none, is
    absent, or fails to parse (never an error; a caller checks ``[]`` vs
    non-empty, the same "absent reads as compliant" shape the sibling
    boundary gates use).

    A name counts as reyn-internal when it is exactly ``reyn`` or starts
    with ``reyn.`` — the same exact-match-or-dotted-prefix rule
    ``check_fastmcp_import_boundary.py`` uses, so a hypothetical unrelated
    package merely starting with the same letters (not a real package,
    but the rule must not match on a bare substring) is never confused
    with a real ``reyn`` import."""
    if not path.is_file():
        return []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return []
    found: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "reyn" or alias.name.startswith("reyn."):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "reyn" or node.module.startswith("reyn.")):
                found.append(node.module)
    return found


def main(argv: "list[str] | None" = None) -> int:
    del argv  # no options -- a single-file scan against a baseline of zero
    offenders = reyn_internal_imports(_APPROVAL_LEDGER_PATH)

    if not offenders:
        print(
            "OK: approval_ledger.py imports no reyn-internal module "
            "(stdlib-only boundary intact)."
        )
        return 0

    print("approval-ledger-import-boundary gate FAILED:\n", file=sys.stderr)
    print(
        f"{_APPROVAL_LEDGER_PATH.relative_to(_ROOT)} imports "
        f"{len(offenders)} reyn-internal module(s) (#5177):",
        file=sys.stderr,
    )
    for name in offenders:
        print(f"  {name}", file=sys.stderr)
    print(
        "\nsrc/reyn/api/safe/file.py runs inside the python-harness "
        "SUBPROCESS and depends on this module staying stdlib-only "
        "(#5173) — it imports approval_ledger.py directly rather than "
        "the rest of the security.permissions package for exactly this "
        "reason. Move whatever needs a reyn-internal import out of "
        "approval_ledger.py, or reconsider whether api/safe/file.py can "
        "still safely import it.\n"
        "\nThis gate's own starting population is zero, so any hit here "
        "is a new regression, not inherited debt.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
