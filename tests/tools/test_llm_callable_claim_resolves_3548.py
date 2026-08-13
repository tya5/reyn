"""Tier 2: a docstring's ``LLM-callable via ToolDefinition X`` claim must name a
tool the default registry actually resolves (#3548).

The claim is a reachability statement about the LLM's surface — "the model can
call this" — and a reader (human or agent) has no other cheap way to check it.
#3548's three instances (``semantic_search`` / ``drop_source`` /
``index_update``) survived FP-0066 P1b (#3257), which retired the very
``ToolDefinition``s they named; the ops kept working as OS-internal substrate,
so nothing failed and nothing noticed. This module is the gate that would have
turned that PR red.

**Why this pair is gateable at all** — and the pair CLAUDE.md warns is NOT:
``control-ir.md`` ↔ ``OP_KIND_MODEL_MAP`` is explicitly on the author, not on
CI. The difference is that "is this name in the registry?" has a single,
total, in-process answer: ``get_default_registry()`` builds by executing a
straight-line sequence of ``registry.register(...)`` calls with no config,
env, or plugin conditional in it, so absence from it is absence, full stop —
there is no "registered only when X is configured" reading that would make a
true claim look false here. If that ever stops being true (a conditional
registration lands), this gate starts producing false positives and the
honest fix is to teach it the condition or delete it, not to weaken the
claim's wording.

Scope note: the gate can only see the ONE prose form. A docstring that says
"the LLM calls this as ``foo``" is invisible to it. It is a drift-arrester for
the established phrasing, not a proof that every reachability claim in ``src``
is true — and the self-test below keeps it from silently becoming neither.

Real instances throughout: the real source tree, the real registry.
"""
from __future__ import annotations

import ast
import re

from reyn.tools import get_default_registry
from tests._support.paths import REPO_ROOT

_SRC = REPO_ROOT / "src" / "reyn"

# ``(?<!NOT )`` keeps a NEGATED claim ("NOT LLM-callable ...") from being read as
# an assertion — the correction #3548 lands uses exactly that wording, so without
# the guard the fix would fail its own gate.
_CLAIM = re.compile(r"(?<!NOT )LLM-callable via ToolDefinition\s+`{1,2}([A-Za-z_][A-Za-z0-9_]*)`{1,2}")


def _claims_in_source(source: str, label: str) -> list[tuple[str, str]]:
    """Every ``(tool_name, where)`` claim in ``source``'s docstrings.

    AST, not a raw-text regex: a regex over the whole file matches inside string
    literals and comments too, which is how #3548's own sweep produced false
    positives before it was redone in-process.
    """
    found: list[tuple[str, str]] = []
    tree = ast.parse(source, label)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        # Docstrings wrap; the claim can straddle a newline.
        flat = " ".join(doc.split())
        for match in _CLAIM.finditer(flat):
            where = f"{label}::{getattr(node, 'name', '<module>')}"
            found.append((match.group(1), where))
    return found


def _claims_in_tree() -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for path in sorted(_SRC.rglob("*.py")):
        found.extend(
            _claims_in_source(path.read_text(encoding="utf-8"), str(path.relative_to(_SRC.parent.parent)))
        )
    return found


def test_extractor_finds_a_claim_and_ignores_a_negated_one() -> None:
    """Tier 2: the instrument works — a zero from the sweep below is a finding,
    not a broken regex. Both legs matter: it must SEE the positive form (else
    the gate is vacuous) and must NOT see the negated form (else the #3548
    correction, which says "NOT LLM-callable", fails the gate it ships with).
    """
    module = '''
"""Header.

LLM-callable via ToolDefinition ``definitely_not_a_real_tool``.
"""


class A:
    """Body text.

    NOT LLM-callable: there is no ``also_not_real`` ToolDefinition — retired.
    LLM-callable via ToolDefinition `another_fake_tool`.
    """
'''
    names = {name for name, _ in _claims_in_source(module, "<synthetic>")}
    assert "definitely_not_a_real_tool" in names
    assert "another_fake_tool" in names
    assert "also_not_real" not in names


def test_synthetic_claim_would_go_red() -> None:
    """Tier 2: the gate has teeth — a claim naming an unregistered tool is
    detectable as unresolved by the SAME lookup the sweep uses.
    """
    registered = set(get_default_registry().names())
    assert "definitely_not_a_real_tool" not in registered


def test_every_llm_callable_claim_resolves_in_the_default_registry() -> None:
    """Tier 2: the invariant — each claim names a tool the registry resolves."""
    claims = _claims_in_tree()
    assert claims, (
        "no 'LLM-callable via ToolDefinition X' claim found anywhere in src/ — "
        "either the phrasing was abandoned (retire this gate) or the sweep is "
        "broken (fix it); a silent zero here makes the gate vacuous."
    )
    registry = get_default_registry()
    unresolved = [(name, where) for name, where in claims if registry.lookup(name) is None]
    assert not unresolved, (
        "docstring claims 'LLM-callable via ToolDefinition X' for a tool the "
        f"default registry does not resolve: {unresolved}. Either the tool was "
        "retired (correct the docstring — say what the op IS reachable through) "
        "or the registration was dropped (restore it)."
    )
