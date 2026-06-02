"""Tier 2: #1092 PR-B — every call site that threads the #1212 op-loop gate
(``tool_calls_op_loop_skills=``) ALSO threads the convergence gate
(``routerloop_convergence_skills=``).

Catches the leaf entry-point trap (the #1248 class, one layer below the wired
spine): a constructor call that passes ``tool_calls_op_loop_skills`` but forgets
``routerloop_convergence_skills`` leaves the converged op-loop unreachable from
THAT entry point — e.g. ``cli/commands/dogfood.py`` (the sandbox_2 dogfood path),
``cli/commands/chat.py`` / ``mcp.py`` / ``chainlit_app/app.py``. An AST-walk pins
the parity so a new entry point (or a future caller) can't silently drop one gate
while keeping the other.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"
_OLD = "tool_calls_op_loop_skills"
_NEW = "routerloop_convergence_skills"


def test_every_op_loop_gate_callsite_also_threads_convergence_gate() -> None:
    """Tier 2: no Call passes ``tool_calls_op_loop_skills=`` without
    ``routerloop_convergence_skills=`` (the two gates thread in lockstep)."""
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            if _OLD in kwargs and _NEW not in kwargs:
                offenders.append(
                    f"{py.relative_to(_SRC.parents[1])}:{node.lineno}"
                )
    assert not offenders, (
        "these call sites thread the #1212 op-loop gate but NOT the #1092 "
        f"convergence gate (leaf reachability trap): {sorted(set(offenders))}"
    )
