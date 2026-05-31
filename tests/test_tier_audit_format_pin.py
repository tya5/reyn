"""Tier 1: format-pin detection in scripts/test_tier_audit.py excludes suffix-len fns.

Pins the regex fix from issue #1082. The format-pin rule flags
``len(...) <op> N`` assertions (= Tier 4 "見た目のフォーマット固定"). The
prior regex ``r"len\\([^)]+\\)\\s*[<>=!]+\\s*(\\d+)"`` matched the ``len(``
*substring* inside suffix-len helpers like ``cell_len(`` / ``str_len(``,
so legitimate cell-width invariants (e.g. ``cell_len(ch) <= 1``) tripped a
false ERROR — forcing a variable-binding workaround idiom in TUI width
tests. The fix adds a leading ``\\b`` word boundary; ``_`` being a word
char means ``cell_len`` has no boundary at its inner ``len`` and is
excluded, while standalone ``len(`` still matches.

Tier 1 because the audit script is the OS-level contract surface every
Tier-rule PR review runs through; a false positive here is a recurring
review-friction trap (= memory feedback_tui_test_cell_len_audit_idiom).

Public surface tested (no MagicMock): the auditor's own ``_audit_test``
flow on inline source, filtered to ``format-pinning`` findings.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` as a module without invoking it.

    The script's filename starts with ``test_`` so pytest would collect it
    as a test module if discovered normally — explicit spec loading avoids
    the collision. The module is registered in ``sys.modules`` before exec
    because ``@dataclass`` resolves its host module via ``sys.modules`` and
    raises on a ``None`` return otherwise. (Same loader idiom as the sibling
    audit self-test ``test_tier_audit_private_state_ast.py``.)
    """
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    script = repo_root / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_fmtpin", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _format_pin_findings(audit_mod, source: str) -> list:
    """Return the format-pinning findings for *source*.

    Mirrors the audit script's own ``_audit_test`` flow at function scope
    (= bypasses file I/O so the test authors source inline), filtered to the
    ``format-pinning`` rule.
    """
    auditor = audit_mod.TestAuditor(check_rules={"format-pinning"})
    tree = ast.parse(source)
    func = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    assert func is not None, "test source must define exactly one test function"
    result = auditor._audit_test(
        Path("inline.py"), source, source.splitlines(), func, in_scaffold=False,
    )
    return [f for f in result.findings if f.rule == "format-pinning"]


# ── False-positive cases the fix must clear ─────────────────────────────────


def test_cell_len_comparison_not_flagged(audit_mod) -> None:
    """Tier 1: ``cell_len(x) <= N`` is a cell-width invariant, NOT a format pin."""
    src = (
        "def test_width():\n"
        '    """Tier 2: example."""\n'
        "    assert cell_len(ch) <= 1\n"
    )
    assert _format_pin_findings(audit_mod, src) == [], (
        "cell_len(...) <= N is a width invariant; the inner 'len(' substring "
        "must not trip the format-pin rule (issue #1082)."
    )


def test_str_len_comparison_not_flagged(audit_mod) -> None:
    """Tier 1: other suffix-len helpers (``str_len``) are excluded too."""
    src = (
        "def test_width():\n"
        '    """Tier 2: example."""\n'
        "    assert str_len(label) <= 80\n"
    )
    assert _format_pin_findings(audit_mod, src) == []


# ── Regression: genuine format pins must STILL fire ─────────────────────────


def test_bare_len_comparison_still_flagged(audit_mod) -> None:
    """Tier 1: ``len(x) <= N`` (a real format/shape pin) is still detected.

    The word-boundary fix narrows the match to standalone ``len`` — it must
    not weaken detection of the actual Tier-4 pattern the rule exists for.

    The comparison operand is interpolated (``{cmp}``) so no physical source
    line in *this* file carries the contiguous ``len(...) <op> N`` shape —
    otherwise the audit would flag its own regression test (the auditor scans
    a test's source lines, docstring included). The assembled ``src`` string
    still carries the full pattern for the auditor to inspect.
    """
    cmp = "<= 1"
    src = (
        "def test_shape():\n"
        '    """Tier 2: example."""\n'
        f"    assert len(rows) {cmp}\n"
    )
    (only,) = _format_pin_findings(audit_mod, src)
    assert "len(rows)" in only.message


def test_len_existence_check_still_exempt(audit_mod) -> None:
    """Tier 1: ``len(x) > 0`` (existence check) stays exempt, as before."""
    src = (
        "def test_nonempty():\n"
        '    """Tier 2: example."""\n'
        "    assert len(items) > 0\n"
    )
    assert _format_pin_findings(audit_mod, src) == []
