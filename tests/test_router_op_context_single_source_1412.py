"""Tier 2: #1412 — the chat-router OpContext is built from a single source.

``ChatSession._make_router_op_context`` and
``RouterHostAdapter.make_router_op_context`` built the
``skill_name="chat_router"`` OpContext with ~95% identical code and drifted
(#1410/#1411 threaded base_dir to one, lagged the other — the #187 wrong-FS
class). The fix routes both through ``build_router_op_context``
(reyn/chat/router_op_context.py).

Pinned invariants (src-wide AST, the #1402 sole-construction pattern):

- An ``OpContext(..., skill_name="chat_router", ...)`` is constructed ONLY in
  ``router_op_context.py`` anywhere in ``src/reyn``. A second chat-router
  OpContext construction re-opens the drift class → this fails, naming
  file:line (incl. hidden sites).
- Both hosts delegate to ``build_router_op_context`` (positive guard — the
  chokepoint is used, not bypassed).

Cf. [[feedback_multi_callsite_wiring_audit]] / #1402 src-wide invariant.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "reyn"
_FACTORY_REL = "chat/router_op_context.py"


def _chat_router_opcontext_sites() -> list[str]:
    sites: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "OpContext"
            ):
                continue
            for kw in node.keywords:
                if (
                    kw.arg == "skill_name"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "chat_router"
                ):
                    sites.append(f"{rel}:{node.lineno}")
    return sites


def _calls_named(rel: str, name: str) -> int:
    tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
    return sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == name
    )


def test_chat_router_opcontext_built_only_in_factory() -> None:
    """Tier 2: #1412 — the chat_router OpContext is constructed ONLY in
    router_op_context.py; both hosts route through build_router_op_context so
    a new capability reaches both paths by construction. Falsifiable: a second
    inline chat-router OpContext construction fails this, naming file:line."""
    sites = _chat_router_opcontext_sites()
    offenders = [s for s in sites if not s.startswith(_FACTORY_REL + ":")]
    assert sites, "no OpContext(skill_name='chat_router') found at all (factory missing?)"
    assert not offenders, (
        "OpContext(skill_name='chat_router') built outside router_op_context.py "
        "— re-opens the #1412 drift class; route through build_router_op_context: "
        f"{offenders}"
    )


def test_both_hosts_delegate_to_build_router_op_context() -> None:
    """Tier 2: #1412 — ChatSession and RouterHostAdapter both call
    build_router_op_context (positive guard: the chokepoint is used)."""
    for rel in ("chat/session.py", "chat/services/router_host_adapter.py"):
        assert _calls_named(rel, "build_router_op_context") >= 1, (
            f"{rel} must delegate to build_router_op_context (#1412 single-source)"
        )
