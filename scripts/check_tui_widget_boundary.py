#!/usr/bin/env python3
"""#5131 gate A — a ``textual_chat/`` WIDGET module never imports transport
or registry.

Architect ruling (#5131): the "up" half of this arc's react discipline is
already a framework (Textual ``Message`` subclasses, 10+ of them — widgets
throw events upward, never touch the wire directly) — measured, not
assumed: exactly ONE file in this package, ``app.py``, imports
``reyn.interfaces.transport`` or ``reyn.runtime.registry``; every other
module is already correctly widget-only. This gate PINS that boundary —
structural, zero-FP (an import statement is unambiguous, unlike "does this
duplicate state" — that's gate B, a ratchet, not this) — so it can never
silently erode as new widget files land.

``app.py`` itself is the ONE place transport/registry access belongs (it
IS the seam between the wire and the widget tree) — excluded by name, not
grandfathered by a baseline: this is a fixed architectural role, not a
population of legacy violations to shrink over time.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_DIR = _ROOT / "src" / "reyn" / "interfaces" / "inline" / "textual_chat"

# The one file allowed to be the wire<->widget-tree seam.
_EXEMPT_FILENAMES = frozenset({"app.py"})

_FORBIDDEN_PREFIXES = (
    "reyn.interfaces.transport",
    "reyn.runtime.registry",
)


def _imported_module_names(tree: ast.Module) -> "list[str]":
    names: "list[str]" = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def find_violations(package_dir: Path = _PACKAGE_DIR) -> "list[tuple[Path, str]]":
    """Return ``(file, offending_module)`` for every non-exempt widget
    module importing a forbidden prefix. AST-based (real ``Import``/
    ``ImportFrom`` nodes), not a substring search — a docstring or comment
    merely MENTIONING "transport" (this package's own module docstrings do)
    must never false-positive here."""
    violations: "list[tuple[Path, str]]" = []
    for path in sorted(package_dir.glob("*.py")):
        if path.name in _EXEMPT_FILENAMES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue  # a real syntax error is caught by other CI gates, not this one
        for module_name in _imported_module_names(tree):
            if any(module_name.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES):
                violations.append((path, module_name))
    return violations


def main() -> int:
    if not _PACKAGE_DIR.is_dir():
        print(f"check_tui_widget_boundary: {_PACKAGE_DIR} not found", file=sys.stderr)
        return 1
    violations = find_violations()
    if violations:
        print("check_tui_widget_boundary FAILED:\n", file=sys.stderr)
        for path, module_name in violations:
            print(
                f"  {path.relative_to(_ROOT)} imports {module_name!r} — "
                "widget modules never touch transport/registry directly "
                "(#5131); route through app.py instead.",
                file=sys.stderr,
            )
        return 1
    print("check_tui_widget_boundary OK: no widget module imports transport/registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
