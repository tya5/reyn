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

## Scope: one file, both module-level and deferred imports — never
## ``TYPE_CHECKING``-guarded ones

Both a module-level ``import reyn.foo`` and a deferred (function-local)
``import reyn.foo`` are in scope — a regression could reach for either
shape, and ``approval_ledger.py``'s own existing deferred ``import yaml``
(third-party, not reyn) shows deferred imports are already a real pattern
in this file, not a hypothetical one to leave uncovered.

An import inside ``if TYPE_CHECKING:`` is deliberately EXCLUDED (architect
co-vet, #5183 issuecomment-5384441986): this gate's whole purpose is what
actually gets pulled into the python-harness SUBPROCESS at runtime, and a
``TYPE_CHECKING``-guarded import never executes — flagging it would be a
false positive against that purpose, not a real widening of the
subprocess's import surface.

## The target path is DERIVED, not hand-typed (architect co-vet, #5183)

A hand-typed ``_ROOT / "src" / ... / "approval_ledger.py"`` literal has
exactly the failure mode #5175 (this issue's own sibling fix, landed the
same night) closed for the write-gate carve-out: if ``approval_ledger.py``
is ever renamed or moved, the literal path silently stops existing, this
gate's own "absent reads as compliant" rule (below) makes that read as a
PASS, and the gate goes quietly blind rather than failing loud. Deriving
the path from the REAL module's own ``__file__`` means a rename raises
``ImportError`` here instead — the gate breaks LOUDLY at the moment the
thing it protects moves, rather than silently protecting nothing.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from reyn.security.permissions import approval_ledger as _approval_ledger_module

_ROOT = Path(__file__).resolve().parent.parent
_APPROVAL_LEDGER_PATH = Path(_approval_ledger_module.__file__).resolve()


def _skip_type_checking_bodies(tree: ast.AST) -> ast.AST:
    """Strip the body of every ``if TYPE_CHECKING:`` (or ``if typing.
    TYPE_CHECKING:``) block out of *tree* in place, so a subsequent
    ``ast.walk`` never descends into imports that only ever run for a
    type checker, not at real runtime — see the module docstring's
    "never TYPE_CHECKING-guarded" section for why that distinction
    matters for THIS gate specifically."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_type_checking = (
            isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"
        ) or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if is_type_checking:
            node.body = []
    return tree


def reyn_internal_imports(path: Path) -> "list[str]":
    """Every reyn-internal module name *path* imports (module-level or
    deferred, anywhere in its AST, excluding an ``if TYPE_CHECKING:``
    block's body — see the module docstring) — ``[]`` when the file has
    none, is absent, or fails to parse (never an error; a caller checks
    ``[]`` vs non-empty, the same "absent reads as compliant" shape the
    sibling boundary gates use for the FILE READ itself — contrast with
    the module-level ``_APPROVAL_LEDGER_PATH``, which is derived via a
    real import and therefore fails loud, not silently, if the file this
    gate is supposed to be checking has moved).

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
    tree = _skip_type_checking_bodies(tree)
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
