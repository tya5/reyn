"""Tier 1: Rule 1 (tier-docstring) accepts "Tier scaffold:" inside
tests/scaffold/, and rejects it (deny side, ERROR) everywhere else.

Lead-coder's own #5606 finding (surfaced during #5604 review): Rule 1's
``TIER_DOCSTRING_RE`` (``^Tier [123][abc]?:``) does not vary on
``in_scaffold`` — only Rules 5/6 do — so a genuine scaffold test (an
extraction-refactor characterization, or #5603's "does the upstream defect
this repo works around still exist" shape) has no Tier of its own to name:
its subject is a third party's behavior or a past state, not a reyn
contract. Forced to pick Tier 1/2/3 anyway, it necessarily misdeclares
(CLAUDE.md six questions ⑥: "the declared Tier is not the one question 1
named") — and there was no way to write it that avoided this, because the
gate itself forced the choice.

This test drives the auditor's own ``_audit_test`` directly (same idiom as
the sibling self-test ``tests/scripts/test_tier_audit_format_pin.py``) so
both branches — accept inside ``tests/scaffold/``, deny outside — are
exercised against the SAME docstring text, varying only ``in_scaffold``.

Deny side is the load-bearing half (lead-coder's own explicit requirement,
issue #5606): without it, "Tier scaffold:" would be a Tier 4 escape route
usable from any file, not tests/scaffold/'s own vocabulary.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from tests._support.paths import REPO_ROOT


def _load_audit_module():
    """Import ``scripts/test_tier_audit.py`` as a module without invoking it.

    Same loader idiom as ``test_tier_audit_format_pin.py`` — the script's
    filename starts with ``test_`` so pytest would otherwise try to collect
    it as a test module.
    """
    repo_root = REPO_ROOT
    script = repo_root / "scripts" / "test_tier_audit.py"
    spec = importlib.util.spec_from_file_location("_audit_tier_audit_scaffold_5606", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def audit_mod():
    return _load_audit_module()


def _tier_docstring_findings(audit_mod, source: str, *, in_scaffold: bool) -> list:
    """Return the ``tier-docstring`` findings for *source*, driving Rule 1
    directly with the given ``in_scaffold`` flag — the exact parameter Rule
    1 previously ignored (#5606's own finding)."""
    auditor = audit_mod.TestAuditor(check_rules={"tier-docstring"})
    tree = ast.parse(source)
    func = next(
        (n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))),
        None,
    )
    assert func is not None, "test source must define exactly one test function"
    result = auditor._audit_test(
        Path("inline.py"), source, source.splitlines(),
        audit_mod._split_source_lines(source), func, in_scaffold=in_scaffold,
    )
    return [f for f in result.findings if f.rule == "tier-docstring"]


_SCAFFOLD_SRC = (
    "def test_upstream_defect_still_reproduces():\n"
    '    """Tier scaffold: does the upstream defect this repo works around\n'
    '    still reproduce — GREEN means still broken, see module docstring."""\n'
    "    assert True\n"
)


# ── Accept side: "Tier scaffold:" inside tests/scaffold/ ────────────────────


def test_scaffold_declaration_accepted_inside_scaffold(audit_mod) -> None:
    """Tier 1: #5606 accept — the SAME docstring that fails outside
    (below) is accepted with no finding when ``in_scaffold=True``."""
    findings = _tier_docstring_findings(audit_mod, _SCAFFOLD_SRC, in_scaffold=True)
    assert findings == [], findings


# ── Deny side: "Tier scaffold:" outside tests/scaffold/ is an ERROR ─────────


def test_scaffold_declaration_rejected_outside_scaffold(audit_mod) -> None:
    """Tier 1: #5606 deny (lead-coder's own explicit requirement) — the
    IDENTICAL docstring, only ``in_scaffold=False`` this time, must be an
    ERROR. Without this, "Tier scaffold:" would be a Tier 4 escape route
    reachable from any test file, not tests/scaffold/'s own vocabulary."""
    (only,) = _tier_docstring_findings(audit_mod, _SCAFFOLD_SRC, in_scaffold=False)
    assert only.level == "ERROR", only
    assert "Tier scaffold:" in only.message, only.message
    assert "outside tests/scaffold/" in only.message, only.message


# ── Regression: numeric Tier declarations are unaffected, either side ───────


def test_numeric_tier_still_accepted_inside_scaffold(audit_mod) -> None:
    """Tier 1: #5606 non-regression — a scaffold test may still declare a
    real numeric Tier (e.g. Tier 2) instead of the scaffold vocabulary;
    Rule 1's existing numeric-Tier path is untouched by this fix."""
    src = (
        "def test_something():\n"
        '    """Tier 2: a scaffold test that happens to fit a real tier."""\n'
        "    assert True\n"
    )
    assert _tier_docstring_findings(audit_mod, src, in_scaffold=True) == []


def test_numeric_tier_still_accepted_outside_scaffold(audit_mod) -> None:
    """Tier 1: #5606 non-regression — the ordinary, non-scaffold case is
    unaffected: a real numeric Tier declaration outside tests/scaffold/
    still passes exactly as before."""
    src = (
        "def test_something():\n"
        '    """Tier 2: an ordinary, non-scaffold test."""\n'
        "    assert True\n"
    )
    assert _tier_docstring_findings(audit_mod, src, in_scaffold=False) == []


def test_malformed_docstring_still_rejected_either_side(audit_mod) -> None:
    """Tier 1: #5606 non-regression — a docstring matching neither the
    numeric Tier form nor "Tier scaffold:" is still rejected, in or out of
    tests/scaffold/ — the fix only widens the accepted vocabulary inside
    tests/scaffold/, it does not loosen the fallback ERROR."""
    src = (
        "def test_something():\n"
        '    """not a tier declaration at all."""\n'
        "    assert True\n"
    )
    for in_scaffold in (True, False):
        (only,) = _tier_docstring_findings(audit_mod, src, in_scaffold=in_scaffold)
        assert only.level == "ERROR", (in_scaffold, only)
        assert "Tier scaffold:" not in only.message, (
            "the generic-malformed-docstring error must not be confused "
            f"with the scaffold-specific deny message: {only.message!r}"
        )
