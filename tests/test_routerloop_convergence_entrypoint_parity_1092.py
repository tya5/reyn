"""Tier 2: #1092 PR-C-3 — the converged op-loop gate (``tool_calls_op_loop_skills``)
PROPAGATES from every entry point down to ``PhaseExecutor``; no layer drops it.

Single-gate successor to the #1260 two-gate-lockstep parity test. PR-C-3 merged the
transitional ``routerloop_convergence_skills`` gate into ``tool_calls_op_loop_skills``
(now the converged op-loop's single gate) and retired the frame-fed ``_run_op_loop``,
so the old "thread BOTH gates in lockstep" contract no longer applies. The
leaf-reachability concern it guarded (the #1248 class) survives in single-gate form:
a layer that RECEIVES the gate (as a parameter or a stored ``self`` attribute) but
forgets to PROPAGATE it onward leaves the converged op-loop unreachable from that
entry point — silently, with zero opt-in skills, undetectably until a dogfood run.

Invariant (AST-walk, falsifiable): every module that REFERENCES the gate — as a
parameter ``tool_calls_op_loop_skills``, a ``self._tool_calls_op_loop_skills``
attribute, or a ``config.tool_calls_op_loop_skills`` read — must also PROPAGATE it:
pass it onward as a ``tool_calls_op_loop_skills=`` kwarg (forward to the next layer)
OR consume it as an ``op_loop_enabled=`` kwarg (the terminal OSRuntime→PhaseExecutor
conversion). A module that references but never propagates is dropping the gate.

This holds across the spine (agent → runtime → run_orchestrator → phase_executor)
AND the leaf entry points (chat / web / chainlit / cli-{dogfood,chat,mcp} /
skill_node_runner). Docstring / comment mentions don't count — only real
parameter / attribute / kwarg AST nodes.

Falsification: delete the ``tool_calls_op_loop_skills=`` forward in any threading
module (e.g. ``agent.py``'s ``OSRuntime(...)`` call) → that module references the
gate but no longer propagates it → this test FAILS, naming the offender.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"

_GATE = "tool_calls_op_loop_skills"
_GATE_ATTR = "_tool_calls_op_loop_skills"
_CONSUME = "op_loop_enabled"  # the terminal OSRuntime → PhaseExecutor conversion


def _references_gate(tree: ast.AST) -> bool:
    """True if the module references the gate as code (NOT in a docstring/comment):
    a parameter named ``tool_calls_op_loop_skills``, an attribute access ending in
    ``tool_calls_op_loop_skills`` / ``_tool_calls_op_loop_skills``, or a bare Name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.arg == _GATE:
            return True
        if isinstance(node, ast.Attribute) and node.attr in (_GATE, _GATE_ATTR):
            return True
        if isinstance(node, ast.Name) and node.id in (_GATE, _GATE_ATTR):
            return True
    return False


def _propagates_gate(tree: ast.AST) -> bool:
    """True if the module passes the gate onward — a ``tool_calls_op_loop_skills=``
    kwarg (forward) or an ``op_loop_enabled=`` kwarg (terminal consume)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in (_GATE, _CONSUME):
            return True
    return False


def test_op_loop_gate_propagates_through_every_layer() -> None:
    """Tier 2: no module references the converged op-loop gate without propagating
    it (forward as ``tool_calls_op_loop_skills=`` or consume as ``op_loop_enabled=``)
    — so the gate reaches PhaseExecutor from every entry point (single-gate
    leaf-reachability, the #1260 lockstep successor)."""
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        if _references_gate(tree) and not _propagates_gate(tree):
            offenders.append(str(py.relative_to(_SRC.parents[1])))
    assert not offenders, (
        "these modules reference the converged op-loop gate "
        f"({_GATE!r}) but never propagate it onward (forward as "
        f"{_GATE}= or consume as {_CONSUME}=) — the gate is dropped, leaving the "
        f"converged op-loop unreachable from that entry point: {sorted(offenders)}"
    )


def test_routerloop_convergence_skills_gate_fully_retired() -> None:
    """Tier 2: the transitional ``routerloop_convergence_skills`` gate is GONE from
    src code (PR-C-3 hard-removed it, merging it into ``tool_calls_op_loop_skills``).
    A lingering code reference would mean a half-merged gate — the dual-gate drift the
    single-gate flip eliminates. Docstring mentions of the retired name are allowed
    (historical context); only code references (param / attribute / kwarg / Name)."""
    _RETIRED = "routerloop_convergence_skills"
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            hit = (
                (isinstance(node, ast.arg) and node.arg == _RETIRED)
                or (isinstance(node, ast.Attribute) and node.attr == _RETIRED)
                or (isinstance(node, ast.keyword) and node.arg == _RETIRED)
                or (isinstance(node, ast.Name) and node.id == _RETIRED)
            )
            if hit:
                offenders.append(f"{py.relative_to(_SRC.parents[1])}:{node.lineno}")
    assert not offenders, (
        "the transitional routerloop_convergence_skills gate must be fully retired "
        f"from src code (PR-C-3 merged it into {_GATE}); code references remain: "
        f"{sorted(set(offenders))}"
    )
