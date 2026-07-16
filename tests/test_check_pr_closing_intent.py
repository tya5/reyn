"""Tier 1: scripts/check_pr_closing_intent.py contradiction-detection contract.

Pins the invariant issue #3007 ratified: the intent a PR body *declares*
about issue #N must match the closing behavior GitHub's own parser
(``closingIssuesReferences``) actually resolved for #N. The three checks
(false negative / false positive / undeclared) are pure facets of that one
invariant, and ``check_contradictions`` is a pure function over
``(body, closing_refs)`` — no network, no subprocess — so this is a Tier 1
contract test against known inputs/outputs.

Public surface only (no MagicMock, no private-state asserts): each case
calls ``check_contradictions`` and asserts on the returned ``Finding``
objects' public fields (``check`` / ``issue``).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    """Import scripts/check_pr_closing_intent.py without a scripts/ package.

    scripts/ has no ``__init__.py`` (mirrors the loader idiom used by
    ``tests/test_tier_audit_format_pin.py`` for the sibling audit script).
    """
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "scripts" / "check_pr_closing_intent.py"
    spec = importlib.util.spec_from_file_location("check_pr_closing_intent", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_pr_closing_intent"] = module
    spec.loader.exec_module(module)
    return module


m = _load_module()


def _checks(findings):
    return sorted((f.check, f.issue) for f in findings)


def test_check1_fires_on_backtick_fenced_closing_keyword_not_resolved():
    """Tier 1: backtick-fenced `Closes #N` still triggers check 1.

    Real GitHub closing-keyword auto-close also ignores backticks, so our
    own regex must see through them the same way rather than being defused
    by fencing, when the parser did not resolve N as a closing reference.
    """
    body = "Some prose. `Closes #123` more prose."
    findings = m.check_contradictions(body, closing_refs=[])
    assert _checks(findings) == [(1, 123)]


def test_check1_passes_when_parser_agrees():
    """Tier 1: `Closes #N` with N present in closingIssuesReferences is clean."""
    body = "Closes #77"
    findings = m.check_contradictions(body, closing_refs=[77])
    assert findings == []


def test_check2_fires_on_part_of_when_parser_will_close():
    """Tier 1: real historical case #3003 triggers check 2.

    Body says "part of #2827" while closingIssuesReferences contains 2827
    (fixture built from the PR's own real declaration text and parser
    output, per issue #3007's falsification requirement #2).
    """
    body = "part of #2827 (part 2 only; part 1 is a separate PR)."
    findings = m.check_contradictions(body, closing_refs=[2827])
    assert _checks(findings) == [(2, 2827)]


def test_check2_passes_when_referenced_issue_correctly_absent():
    """Tier 1: `part of #N` with N correctly absent from closingIssuesReferences

    is clean (real PR #3014: "part of #3009", closingIssuesReferences == []).
    """
    body = "part of #3009 (item 1 only)."
    findings = m.check_contradictions(body, closing_refs=[])
    assert findings == []


def test_check3_fires_on_undeclared_parser_closure():
    """Tier 1: closingIssuesReferences non-empty for N, body has zero

    declaring phrases (closing or non-closing) about N — the architect's
    hole: prose like "addresses #N" parses as a closing reference without
    the author ever writing Closes/Fixes/Resolves or part of/toward.
    """
    body = "This PR addresses #55 nicely, thanks reviewer."
    findings = m.check_contradictions(body, closing_refs=[55])
    assert _checks(findings) == [(3, 55)]


def test_check3_does_not_fire_when_nonclosing_declaration_present():
    """Tier 1: check 3 only requires SOME declaration (closing or

    non-closing) about N — a bare "part of #N" mention is enough to avoid
    check 3 even though it would still trip check 2 if the parser closes N.
    """
    body = "part of #90"
    findings = m.check_contradictions(body, closing_refs=[90])
    # check 2 fires (declared non-closing, parser closes) but check 3 must
    # NOT — the body does declare something about #90.
    assert _checks(findings) == [(2, 90)]


def test_clean_pr_multiple_issues_no_contradictions():
    """Tier 1: a well-formed body — proper `Closes #N` outside backticks

    matching closingIssuesReferences exactly, and `part of #M` with M
    correctly absent — produces zero findings (must not cry wolf).
    """
    body = "Closes #200\n\npart of #300 (separate scope, not closed here)."
    findings = m.check_contradictions(body, closing_refs=[200])
    assert findings == []


def test_find_closing_declarations_strips_backticks():
    """Tier 1: the closing-declaration finder itself sees through backticks."""
    assert m.find_closing_declarations("`Fixes #9`") == {9}


def test_find_nonclosing_declarations_matches_toward():
    """Tier 1: "toward #N" is recognized as a non-closing declaration."""
    assert m.find_nonclosing_declarations("toward #42") == {42}
