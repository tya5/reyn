"""Tier 1: scripts/check_pr_closing_intent.py contradiction-detection contract.

Pins the invariant issue #3007 ratified: the intent a PR body *declares*
about issue #N must match the closing behavior GitHub's own parser
(``closingIssuesReferences``) actually resolved for #N, PLUS (check 4,
added to close the #3187/#1909 gap) the closing behavior a commit message
independently declares. The four checks are pure facets of that one
invariant, and ``check_contradictions`` is a pure function over
``(body, closing_refs, commit_messages)`` — no network, no subprocess — so
this is a Tier 1 contract test against known inputs/outputs.

Public surface only (no MagicMock, no private-state asserts): each case
calls ``check_contradictions`` and asserts on the returned ``Finding``
objects' public fields (``check`` / ``issue``).
"""
from __future__ import annotations

import importlib.util
import sys

from tests._support.paths import REPO_ROOT


def _load_module():
    """Import scripts/check_pr_closing_intent.py without a scripts/ package.

    scripts/ has no ``__init__.py`` (mirrors the loader idiom used by
    ``tests/scripts/test_tier_audit_format_pin.py`` for the sibling audit script).
    """
    repo_root = REPO_ROOT
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


# ---------------------------------------------------------------------------
# Use vs mention (#3559): a body that records CLAUDE.md rule 4's enumeration
# necessarily quotes "part of #X" inside a body whose operative declaration
# is `Closes #X`. Check 2 read the quotation as a scope declaration and
# failed PR #3558 — penalising the record of following the rule the gate
# enforces. Bodies below are the real #3558 shape (its `Closes #3553` plus
# the Test-plan line that tripped the gate), and its plain-prose form.
# ---------------------------------------------------------------------------


def test_check2_treats_part_of_as_mention_when_the_record_follows_closes():
    """Tier 1: the #3559 acceptance condition — rule 4's enumeration recorded

    in PLAIN PROSE, with no fences and no `closing-check` marker, is clean.
    The body declares `Closes #3553` canonically and GitHub's parser resolved
    3553, so the "part of #3553" in the Test-plan line is a mention of the
    search that was run, not a claim about this PR's scope.
    """
    body = (
        "**[per-PR-coder]** — worker inherits the invoker's narrowing.\n\n"
        "Closes #3553\n\n"
        "## Test plan\n"
        "- [x] Enumerated 3553 in:body before writing the closing keyword — "
        "the only hit is #3554 (MERGED); no open part of #3553 PR remains."
    )
    findings = m.check_contradictions(body, closing_refs=[3553])
    assert findings == []


def test_check2_treats_part_of_as_mention_when_the_record_precedes_closes():
    """Tier 1: the same body with the record ABOVE the closing keyword.

    The mention-first leg. An earlier attempt at this rule resolved use vs
    mention by which declaration came first, which made the verdict flip on
    line order alone and reddened exactly this arrangement — a plain-prose
    record of rule 4 whose only workaround would be reordering or fencing,
    i.e. the ritual the acceptance condition forbids. Same meaning, same
    verdict: green.
    """
    body = (
        "**[per-PR-coder]** — worker inherits the invoker's narrowing.\n\n"
        "Enumerated 3553 in:body before writing the closing keyword — the "
        "only hit is #3554 (MERGED); no open part of #3553 PR remains.\n\n"
        "Closes #3553"
    )
    findings = m.check_contradictions(body, closing_refs=[3553])
    assert findings == []


def test_check2_treats_fenced_part_of_as_mention_when_body_also_closes():
    """Tier 1: the same record written with backticks (#3558's literal line)

    is clean too. Fencing is a *symptom* of mention, never the criterion —
    both spellings must pass, so neither can be the thing being tested.
    """
    body = (
        "**[per-PR-coder]** — worker inherits the invoker's narrowing.\n\n"
        "Closes #3553\n\n"
        "- [x] `Closes` の前に `3553 in:body` を全列挙 — 該当は #3554"
        "（MERGED）のみ、open な `part of #3553` は無し"
    )
    findings = m.check_contradictions(body, closing_refs=[3553])
    assert findings == []


def test_check2_is_silent_when_both_forms_are_canonical():
    """Tier 1: the accepted trade — a false negative is preferred here.

    The mention rule is unconditional: given a canonical `Closes #N`, every
    other "part of #N" in that body reads as a mention, INCLUDING one the
    author meant as a real scope declaration. So a body declaring both forms
    canonically and unfenced for the same N is silent where it once failed.

    Pinned deliberately, not discovered: the removed false positive is
    systematic (it fires on every rule-4-compliant PR that records the
    enumeration, and the cheapest fix is deleting the record), while the
    accepted false negative is self-contradictory input whose close is the
    one the author asked for — no surprise closure, which is the harm check 2
    guards. Rationale in full in the module docstring.
    """
    body = "Closes #400\n\nAlso part of #400, which cannot both be true."
    findings = m.check_contradictions(body, closing_refs=[400])
    assert findings == []


def test_check2_still_fires_on_the_real_3003_body_shape_with_its_prose_keyword():
    """Tier 1: the mention rule must not silence its own motivating incident.

    Real PR #3003 declares "part of #2827" and elsewhere explains that a
    closing keyword "would auto-close #2827 with part 1 undone" — keyword-
    shaped prose (both excerpts are real substrings of that body). `_CLOSING_RE`
    matches that `close`, as it must, so a co-occurrence rule over the wide
    vocabulary went green here. Corroboration is restricted to the canonical
    declaring forms, and "auto-close" is not one, so this stays red — with no
    appeal to where in the body either phrase sits.
    """
    body = (
        "**[per-PR-coder]** — part of #2827 (part 2 only; part 1 = asdf/mise "
        "resolution is NOT in this PR, so no closing keyword).\n\n"
        "Per CLAUDE.md rule 4, a sub-PR must not carry a closing keyword: "
        "`Closes` here would auto-close #2827 with part 1 undone."
    )
    findings = m.check_contradictions(body, closing_refs=[2827])
    assert _checks(findings) == [(2, 2827)]


def test_check2_still_fires_when_the_body_only_quotes_the_closing_keyword():
    """Tier 1: a merely QUOTED closing keyword cannot corroborate.

    Fenced spans are removed before looking for the canonical declaration,
    so a doc-style body that quotes `Closes #N` while genuinely declaring
    "part of #N" still reports the contradiction. GitHub honors no fenced
    keyword, so an N in closingIssuesReferences there came from a bare prose
    keyword — the #3003 class again.
    """
    body = (
        "Rule 4 says to write `Closes #2827` only in the final PR of an arc.\n\n"
        "This PR is part of #2827 (part 2 only)."
    )
    findings = m.check_contradictions(body, closing_refs=[2827])
    assert _checks(findings) == [(2, 2827)]


def test_discussing_marker_is_not_downgraded_to_a_mention_by_a_closing_keyword():
    """Tier 1: the mention rule covers the prose vocabulary only.

    `<!-- closing-check: discussing #N -->` is exact syntax an author types
    for one purpose, so it is never an incidental quotation. A body that
    both closes #N and declares it merely discussed is author confusion, and
    check 2 must still report it.
    """
    body = "Closes #77\n\n<!-- closing-check: discussing #77 -->"
    findings = m.check_contradictions(body, closing_refs=[77])
    assert _checks(findings) == [(2, 77)]


def test_canonical_closing_finder_is_the_mirror_of_the_check1_finder():
    """Tier 1: the corroboration finder reads fences opposite to check 1's.

    `find_closing_declarations` reads the body as the AUTHOR declared it
    (fences defused) so check 1 still sees the declaration GitHub ignored;
    the corroboration finder reads it as GITHUB parses it (fences honored)
    so a quoted keyword cannot pass as the author's own scope declaration.
    Same input, deliberately opposite answers — the per-check asymmetry the
    module docstring justifies.
    """
    body = "Docs quote `Closes #10`, but this PR is only part of #10."
    assert m.find_canonical_closing_declarations(body) == set()
    assert m.find_closing_declarations(body) == {10}


def test_canonical_closing_finder_rejects_keyword_shaped_prose():
    """Tier 1: corroboration needs a declaring FORM, not any keyword GitHub honors.

    `_CLOSING_RE` must match "auto-close #N" and "will close #N" — GitHub
    acts on both, which is checks 1 and 3's business. Check 2 asks whether
    the author *declared* a close, and neither is a declaration; widening
    corroboration to them is what silenced check 2 on real #3003.
    """
    prose = "a keyword would auto-close #2827, and merging will close #2827"
    assert m.find_canonical_closing_declarations(prose) == set()
    assert m.find_closing_declarations(prose) == {2827}
    assert m.find_canonical_closing_declarations("Closes #2827") == {2827}


# ---------------------------------------------------------------------------
# Check 4: commit-message closing declarations (real PR #3187 / #1909 shape).
#
# Real data, verbatim from `gh pr view 3187 --json body,commits,
# closingIssuesReferences` at the time this gate was written (not
# hand-invented fixtures — see testing.ja.md's Mock-vs-Fake guidance to
# prefer real data shapes). The PR body excerpt below is a real substring
# (author-declared "part of #1909" plus a real, pre-existing "discussing
# #1909" marker the author had added for unrelated reasons); the commit
# message is the real second commit's messageHeadline/messageBody, whose
# ``Closes #1909`` line is exactly what leaked into squash-merge commit
# ``d9b4c3a0`` and auto-closed #1909 despite the clean body and empty
# ``closingIssuesReferences``.
# ---------------------------------------------------------------------------

_PR3187_BODY_EXCERPT = (
    "**[per-PR-coder]** — part of #1909\n"
    "<!-- closing-check: discussing #1909 -->\n\n"
    "## Scope: turn-boundary narrowing only\n"
    "This PR eliminates the multi-turn attack surface for issue 1909."
)

_PR3187_COMMIT_WITH_LEAK = (
    "fix(1909): propagate external-source taint from tool-results into his…\n"
    "\n"
    "router_loop.py's feedback() already tagged returns_external_content tool\n"
    "results with _external_source (FP-0050/#1822 S2) and already extracted the\n"
    "tag, but discarded the local variable instead of threading it into the\n"
    "persisted history-entry meta.\n"
    "\n"
    "Closes #1909"
)


def test_check4_fires_on_real_3187_shape_commit_leak_body_says_part_of():
    """Tier 1: real reproduction — PR #3187's actual body + commit text.

    Body correctly declares ``part of #1909`` (checks 1-3 all PASS, as they
    did live: closingIssuesReferences was empty when checked before merge).
    But a commit message independently carries ``Closes #1909`` — exactly
    what caused #1909 to auto-close on squash-merge (merge commit
    d9b4c3a0) despite the clean body. Check 4 must be the only thing that
    catches this: checks 1-3 stay silent (asserted explicitly) because
    ``closing_refs=[]`` is what the live PR actually showed pre-merge.
    """
    findings = m.check_contradictions(
        _PR3187_BODY_EXCERPT,
        closing_refs=[],
        commit_messages=[_PR3187_COMMIT_WITH_LEAK],
    )
    assert _checks(findings) == [(4, 1909)]


def test_check4_does_not_fire_when_body_also_declares_closing():
    """Tier 1: normal/intentional path — body agrees with the commit.

    If the body ALSO writes ``Closes #N``, the commit-message declaration is
    not a contradiction (both sides intend the close), so check 4 must stay
    silent.
    """
    findings = m.check_contradictions(
        "Closes #1909",
        closing_refs=[1909],
        commit_messages=[_PR3187_COMMIT_WITH_LEAK],
    )
    assert findings == []


def test_check4_escape_hatch_works_when_marker_is_in_the_same_commit_message():
    """Tier 1: escape hatch fires — same vocabulary, scoped to the SAME blob.

    A commit message that both quotes ``Closes #N`` (e.g. explaining this
    very gate, as this script's own commit messages must avoid doing
    literally) and carries the discussing marker for N *within that same
    commit message* is exempt for that commit.
    """
    commit_msg = (
        "docs: explain the closing-intent gate\n\n"
        "This gate flags a bare `Closes #555` left in a commit message.\n"
        "<!-- closing-check: discussing #555 -->"
    )
    findings = m.check_contradictions(
        "part of #555",
        closing_refs=[],
        commit_messages=[commit_msg],
    )
    assert findings == []


def test_check4_escape_hatch_does_not_exempt_across_blobs_body_marker_ignored():
    """Tier 1: a marker in the PR BODY must NOT exempt a commit-message leak.

    This is the real #3187 shape reproduced precisely (see
    ``test_check4_fires_on_real_3187_shape_commit_leak_body_says_part_of``
    above): the body's ``discussing #1909`` marker exists for its own
    reasons and does not reach into a *different* text blob (a commit
    message) to suppress a genuine leak there. A body-wide/cross-blob
    exemption would silently defeat check 4 on exactly this real incident —
    the same "escape hatch must not exempt too much" property the existing
    per-issue marker design already protects for check 1
    (``test_discussing_marker_does_not_exempt_a_genuine_declaration``).
    """
    findings = m.check_contradictions(
        "part of #1909\n<!-- closing-check: discussing #1909 -->",
        closing_refs=[],
        commit_messages=[_PR3187_COMMIT_WITH_LEAK],
    )
    assert _checks(findings) == [(4, 1909)]


def test_check4_silent_when_no_commit_messages_supplied():
    """Tier 1: backward compatibility — omitting commit_messages is a no-op.

    Every pre-existing call site/test in this file calls
    ``check_contradictions(body, closing_refs)`` with no third argument;
    check 4 must never fire from an absent commit-message list.
    """
    findings = m.check_contradictions("part of #1909", closing_refs=[])
    assert findings == []
