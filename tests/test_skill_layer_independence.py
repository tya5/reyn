"""Tier 2: reyn.skill takes no runtime dependency on reyn.runtime (#1794).

`reyn.skill` is the lower layer that `reyn.runtime` depends on, not vice versa.
The skill-execution consolidation (#1794) moves modules INTO `reyn.skill`, and
each stage must preserve this layer direction. `verify_package_move` checks
straggler references but not import *direction*, so this is the mechanical
layer-direction gate (first wired in S1).

A `reyn.runtime` import under ``if TYPE_CHECKING:`` is allowed — it is a
type-only reference, not a runtime dependency (and `from __future__ import
annotations` keeps the annotations lazy). Any *executed* `reyn.runtime` import
(module-level outside TYPE_CHECKING, or function-local) is a real dependency and
fails this gate — those are the edges the consolidation must invert.
"""
from __future__ import annotations

import ast
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1] / "src" / "reyn" / "skill"


def _is_type_checking_if(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_dep_imports(py_file: Path) -> list[str]:
    """reyn.runtime imports that execute at runtime (not TYPE_CHECKING-guarded)."""
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

    type_only_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_if(node):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    type_only_lines.add(sub.lineno)

    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("reyn.runtime") and node.lineno not in type_only_lines:
                hits.append(f"{py_file}:{node.lineno}: from {node.module} import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("reyn.runtime") and node.lineno not in type_only_lines:
                    hits.append(f"{py_file}:{node.lineno}: import {alias.name}")
    return hits


def test_reyn_skill_takes_no_runtime_dependency():
    """Tier 2: no module under reyn.skill has an executed reyn.runtime import."""
    violations: list[str] = []
    for py in sorted(SKILL_ROOT.rglob("*.py")):
        violations.extend(_runtime_dep_imports(py))
    assert not violations, (
        "reyn.skill must not depend on reyn.runtime (layer direction, #1794):\n"
        + "\n".join(violations)
    )
