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


def test_discussing_marker_exempts_check1_for_named_issue():
    """Tier 1: a `closing-check: discussing #N` marker exempts check 1 for N.

    The mention-only declaration form: the body quotes a closing keyword to
    explain it (as this script's own PR must) rather than declaring intent,
    so the declared-but-unresolved contradiction does not apply to N.
    """
    body = (
        "<!-- closing-check: discussing #2620 -->\n"
        "We quote `Closes #2620` as a worked example."
    )
    findings = m.check_contradictions(body, closing_refs=[])
    assert findings == []


def test_discussing_marker_does_not_exempt_a_genuine_declaration():
    """Tier 1: the marker is per-issue — it must not become a body-wide bypass.

    A body that both declares (`Closes #3007`) and discusses (#2620) keeps
    check 1 live on the genuine declaration. A body-wide marker would
    silently drop check-1 protection from the real `Closes`, turning the
    escape hatch into the very defect this gate exists to catch.
    """
    body = (
        "Closes #3007\n"
        "<!-- closing-check: discussing #2620 -->\n"
        "We quote `Closes #2620` as a worked example."
    )
    # #3007 declared but NOT resolved by the parser → check 1 must still fire.
    findings = m.check_contradictions(body, closing_refs=[])
    assert _checks(findings) == [(1, 3007)]


def test_discussing_marker_cannot_silence_an_actual_closure():
    """Tier 1: the marker never reaches the parser's output.

    "discussing #N" while `closingIssuesReferences` contains N is itself a
    contradiction (author says mention-only, GitHub says it closes) → check
    2. An exemption that suppressed this would reintroduce the #3003 defect.
    """
    body = "<!-- closing-check: discussing #2827 -->\nWe discuss `Closes #2827`."
    findings = m.check_contradictions(body, closing_refs=[2827])
    assert _checks(findings) == [(2, 2827)]


def test_find_discussing_declarations_reads_multiple_issue_numbers():
    """Tier 1: one marker can name several issue numbers."""
    body = "<!-- closing-check: discussing #2620 #2972 #2827 -->"
    assert m.find_discussing_declarations(body) == {2620, 2972, 2827}


def test_find_closing_declarations_strips_backticks():
    """Tier 1: the closing-declaration finder itself sees through backticks.

    GitHub does NOT honor a fenced keyword (verified: #2990's fenced
    `Closes #2620` left `closingIssuesReferences` empty and #2620 open), so
    this matcher is deliberately stricter than GitHub's in the direction
    that surfaces the contradiction.
    """
    assert m.find_closing_declarations("`Fixes #9`") == {9}


def test_find_nonclosing_declarations_matches_toward():
    """Tier 1: "toward #N" is recognized as a non-closing declaration."""
    assert m.find_nonclosing_declarations("toward #42") == {42}
