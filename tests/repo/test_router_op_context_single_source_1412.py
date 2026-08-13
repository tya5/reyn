"""Tier 2: #1412/#3607 — the chat-router OpContext has ONE construction site.

``Session._make_router_op_context`` and
``RouterHostAdapter.make_router_op_context`` built the
``actor="chat_router"`` OpContext with ~95% identical code and drifted
(#1410/#1411 threaded base_dir to one, lagged the other — the #187 wrong-FS
class). #1412 routed both through ``build_router_op_context``; #3607 removed
the second CALL of it, because sharing an assembly function still leaves two
argument lists to diverge — and twelve fields had.

Pinned invariants (src-wide AST, the #1402 sole-construction pattern):

- An ``OpContext(..., actor="chat_router", ...)`` is constructed ONLY in
  ``router_op_context.py`` anywhere in ``src/reyn``. A second chat-router
  OpContext construction re-opens the drift class → this fails, naming
  file:line (incl. hidden sites).
- ``build_router_op_context`` is CALLED exactly once in all of ``src/reyn``,
  and that one call is inside ``router_op_context.py`` itself (the supplier's
  ``build``). A surface that re-assembles its own argument list — the #1412
  drift class one level up — fails this, naming the file.

Cf. [[feedback_multi_callsite_wiring_audit]] / #1402 src-wide invariant.
"""
from __future__ import annotations

import ast

from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src" / "reyn"
_FACTORY_REL = "runtime/router_op_context.py"


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
                    kw.arg == "actor"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "chat_router"
                ):
                    sites.append(f"{rel}:{node.lineno}")
    return sites


def _call_sites_of(name: str) -> list[str]:
    """Every ``name(...)`` call site under src/reyn, as ``rel:lineno``."""
    sites: list[str] = []
    for py in sorted(_SRC.rglob("*.py")):
        rel = str(py.relative_to(_SRC))
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == name
            ):
                sites.append(f"{rel}:{n.lineno}")
    return sites


def test_chat_router_opcontext_built_only_in_factory() -> None:
    """Tier 2: #1412 — the chat_router OpContext is constructed ONLY in
    router_op_context.py; both hosts route through build_router_op_context so
    a new capability reaches both paths by construction. Falsifiable: a second
    inline chat-router OpContext construction fails this, naming file:line."""
    sites = _chat_router_opcontext_sites()
    offenders = [s for s in sites if not s.startswith(_FACTORY_REL + ":")]
    assert sites, "no OpContext(actor='chat_router') found at all (factory missing?)"
    assert not offenders, (
        "OpContext(actor='chat_router') built outside router_op_context.py "
        "— re-opens the #1412 drift class; route through build_router_op_context: "
        f"{offenders}"
    )


def test_build_router_op_context_has_exactly_one_call_site() -> None:
    """Tier 2: #3607 — the chokepoint is used AND used once.

    The positive half (a call exists) keeps the factory from being bypassed;
    the singular half is what #1412 could not assert, and is the whole point of
    #3607: two surfaces calling one assembly function still write two argument
    lists, and those had diverged on twelve fields. Falsifiable in both
    directions — delete the call and this names the absence; add a second
    anywhere under src/reyn and this names the file:line."""
    sites = _call_sites_of("build_router_op_context")
    assert sites, (
        "build_router_op_context is called nowhere — the chokepoint is bypassed"
    )
    outside = [s for s in sites if not s.startswith(_FACTORY_REL + ":")]
    assert not outside, (
        "build_router_op_context called outside "
        f"{_FACTORY_REL} — a second caller writes a second argument list, which "
        f"is the #1412 drift class: {outside}"
    )
    assert not sites[1:], (
        "build_router_op_context has more than one call site "
        f"(RouterOpContextSource.build is the only one): {sites}"
    )
