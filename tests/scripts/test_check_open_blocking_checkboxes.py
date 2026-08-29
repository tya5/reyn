"""Tier 1: #5314 — a PR body's blocking checkbox must not be closable by
editing the one place the gate looks (the body itself).

#5311/#5312 (one night, 2026-08-27) measured 3 real bypasses of the
original #5135 gate (body-only, open-checkbox-only): a reviewer's
`- [ ] 🔴` deleted in a body rewrite (still unresolved, green), the same
checkbox ticked to `[x]` with no other change (still unresolved, green),
and 4 checkboxes bulk-ticked with genuinely resolved content but nothing
corroborating that (accidentally correct this time, but unverifiable).

Condition A (comment) and condition B (body) are tested both separately
(so a failure names which condition fired) and their OR (via the fixture
CLI, mirroring `evaluate`'s own combined return)."""
from __future__ import annotations

import json

from scripts.check_open_blocking_checkboxes import evaluate, main, run_gate

_HEAD = "abc1234567"


def _pr(body: str, comments: "list[str]" = (), head: str = _HEAD) -> dict:
    return {
        "body": body,
        "comments": [{"body": c} for c in comments],
        "headRefOid": head,
    }


# ---------------------------------------------------------------------------
# Condition B — body (① open, extended ②③④ for checked)
# ---------------------------------------------------------------------------


def test_open_blocking_checkbox_variants_fail() -> None:
    """Tier 1: ① an open checkbox fails, unchanged from #5135 — every
    supported list-marker/formatting variant."""
    for body in (
        "- [ ] 🔴 unresolved",
        "  - [ ] 🔴 nested",
        "* [ ]🔴 compact",
        "+ [ ] **🔴** decorated",
    ):
        code, _ = evaluate(_pr(body))
        assert code != 0


def test_checked_checkbox_with_verbatim_comment_passes() -> None:
    """Tier 1: ② a checked checkbox WITH a comment quoting its own text
    verbatim passes — ticking is not enough on its own, but ticking PLUS
    a corroborating comment is what #5314 asks authors to do."""
    code, _ = evaluate(_pr(
        "- [x] 🔴 the widget must validate input",
        comments=["Fixed — the widget must validate input, confirmed on every call."],
    ))
    assert code == 0


def test_checked_checkbox_with_no_comment_fails() -> None:
    """Tier 1: ③ (the tick bypass, #5311 case 2) — a checked checkbox with
    NO comment anywhere fails. Ticking alone must not be a record."""
    code, lines = evaluate(_pr("- [x] 🔴 the widget must validate input"))
    assert code != 0
    assert any("checked blocking line" in line for line in lines)


def test_checked_checkbox_with_unrelated_comment_fails() -> None:
    """Tier 1: ④ a comment exists, but it does not quote the checked
    line's own text — "something was written" must not be enough, or
    this becomes a checklist item answered yes every time."""
    code, _ = evaluate(_pr(
        "- [x] 🔴 the widget must validate input",
        comments=["Pushed a fix for an unrelated typo in the README."],
    ))
    assert code != 0


# ---------------------------------------------------------------------------
# Condition A — comment (⑤ BLOCKING with no CLEARED, current-head check)
# ---------------------------------------------------------------------------


def test_blocking_comment_with_no_cleared_fails_even_with_empty_body() -> None:
    """Tier 1: ⑤ a BLOCKING comment with no BLOCKING-CLEARED anywhere
    fails — even when the body has nothing in it at all (the reviewer
    who follows house rule 7 and posts the point as a comment is
    protected regardless of what the body currently says). This is the
    deletion bypass #5311 case 1 closes: the gate is stateless, so "the
    body's checkbox was deleted" and "the body never had one" are the
    SAME input to `evaluate` — there is no separate test for "deleted",
    only this one, which already covers an empty body unconditionally."""
    code, lines = evaluate(_pr(
        "",
        comments=[f"**[architect]** — BLOCKING (head {_HEAD})\nThe cache must be invalidated on write."],
    ))
    assert code != 0
    assert any("BLOCKING comment has no matching" in line for line in lines)


def test_a_blocking_raise_does_not_expire_just_because_a_push_landed() -> None:
    """Tier 1: (lead-coder's TESTS-READ catch, #5317) a BLOCKING comment
    naming a STALE head — one older than the PR's current one — still
    fails when it has no CLEARED comment. The raise's OWN head is never
    checked (only a CLEARED comment's head must be current); a raise
    does not expire on a push. Without this, an ordinary push after a
    reviewer's blocking point — no removal, no tick, just normal work —
    would silently make the point vanish: worse than the deletion bypass
    #5311 already measured, since it needs no deliberate action at all.
    Real instance: this very PR's own first BLOCKING comment named
    b3e8a32b2, and its head moved to 43e09c7d6 on the very next push."""
    code, lines = evaluate(_pr(
        "",
        comments=["**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write."],
        head="2222222",  # a push landed after the (still-unresolved) raise
    ))
    assert code != 0
    assert any("BLOCKING comment has no matching" in line for line in lines)


def test_blocking_comment_with_verbatim_cleared_at_current_head_passes() -> None:
    """Tier 1: ⑥ (condition A's accept side — without this, condition A
    could be "always red" and still satisfy every other acceptance test)
    a BLOCKING comment with a LATER BLOCKING-CLEARED comment
    that names the current head and quotes the identifying line verbatim
    passes — the two-comment record #5314 asks reviewers to keep."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nThe cache must be invalidated on write. Now handled in commit {_HEAD}.",
        ],
    ))
    assert code == 0


def test_cleared_naming_a_stale_head_does_not_resolve() -> None:
    """Tier 1: (architect's addition) a BLOCKING-CLEARED that names an
    OLDER head than the PR's current one does not resolve it: a push
    landing after the pair reopens the point (mirrors
    check_tests_read_names_its_tree.py's own ∃-over-CURRENT-head rule)."""
    code, lines = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            "**[architect]** — BLOCKING-CLEARED (head 1111111)\nThe cache must be invalidated on write. Now handled.",
        ],
        head="2222222",  # a new push landed after the pair
    ))
    assert code != 0
    assert any("current head" in line for line in lines)


def test_prose_mentioning_blocking_with_no_sha_is_not_a_raise() -> None:
    """Tier 1: (architect's TESTS-READ catch) a comment whose first line
    merely DISCUSSES blocking ("my blocking is closed"), with no
    `(head <sha>)` co-located on that same line, is not a formal raise —
    the same marker+SHA co-location rule
    check_tests_read_names_its_tree.py already applies on its own claim
    side. Without this, this exact sentence would be miscounted as a
    raise requiring a verbatim-quoting CLEARED comment forever, since
    "my blocking is closed" is itself an everyday sentence in this
    repo's own review threads."""
    code, _ = evaluate(_pr(
        "",
        comments=["**[lead-coder]** — my blocking is closed, thanks for the fix."],
    ))
    assert code == 0


def test_cleared_with_different_wording_does_not_resolve() -> None:
    """Tier 1: a CLEARED comment that exists but does not quote the
    BLOCKING comment's identifying line verbatim does not resolve it —
    "some comment was posted" is not "this point was addressed"."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            "**[architect]** — BLOCKING (head 1111111)\nThe cache must be invalidated on write.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nLooks good to me, thanks!",
        ],
    ))
    assert code != 0


# ---------------------------------------------------------------------------
# Condition C — near-miss detection + decoration-stripping (#5522)
# ---------------------------------------------------------------------------


def test_backtick_decorated_sha_now_matches_and_resolves() -> None:
    """Tier 1: LOAD-BEARING — the real incident (lead-coder, #5517,
    2026-08-29): a BLOCKING comment's first line read
    ``**BLOCKING (head `9862413f0`)**`` — the backtick around the SHA
    broke the pre-#5522 regex, so the gate saw no BLOCKING comment at
    all and stayed silent for 12 minutes. Decoration is now stripped
    before matching, so this now parses as a real marker AND (with a
    matching CLEARED comment) resolves — the single-factor flip #5522's
    own acceptance criterion names."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            f"**[lead-coder]** — **BLOCKING (head `{_HEAD}`)**\nThe cache invalidation is missing.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nThe cache invalidation is missing.",
        ],
    ))
    assert code == 0, "a decorated marker with a matching CLEARED comment must now resolve"


def test_undecorated_form_of_the_same_comment_also_resolves() -> None:
    """Tier 1: negative control for the test above — the SAME comment,
    written without decoration, already worked pre-#5522 and must keep
    working (this PR only ADDS tolerance, never narrows the accepted
    shape)."""
    code, _ = evaluate(_pr(
        "",
        comments=[
            f"**[lead-coder]** — BLOCKING (head {_HEAD})\nThe cache invalidation is missing.",
            f"**[architect]** — BLOCKING-CLEARED (head {_HEAD})\nThe cache invalidation is missing.",
        ],
    ))
    assert code == 0


def test_decorated_blocking_with_no_cleared_comment_fails_via_condition_a() -> None:
    """Tier 1: a decorated BLOCKING comment with no CLEARED counterpart
    at all now parses as a real marker (condition A fires, not just
    condition C's near-miss) — matching the un-decorated case's own
    behavior exactly."""
    code, _ = evaluate(_pr(
        "",
        comments=[f"**BLOCKING (head `{_HEAD}`)**\nThe cache invalidation is missing."],
    ))
    assert code != 0


def test_near_miss_single_factor_flip_malformed_is_red_fixed_is_green() -> None:
    """Tier 1: LOAD-BEARING — #5522's own acceptance criterion ①, the
    single-factor flip. A missing paren (a real typo shape, not
    decoration `_undecorated` would strip — backtick/`*` decoration now
    parses as a real marker after #5522's other fix, see the two tests
    above, so it can no longer demonstrate near-miss in isolation) makes
    the marker unrecognized: RED (near-miss). Adding ONLY the missing
    paren, nothing else, makes the SAME comment GREEN once resolved by
    a matching CLEARED comment."""
    malformed_code, malformed_lines = evaluate(_pr(
        "",
        comments=[
            f"**[x]** — BLOCKING (head {_HEAD}\nSome point.",  # missing closing paren
            f"**[x]** — BLOCKING-CLEARED (head {_HEAD})\nSome point.",
        ],
    ))
    assert malformed_code != 0
    assert any("near-miss" in line for line in malformed_lines), (
        f"expected a near-miss finding -- got {malformed_lines!r}"
    )

    fixed_code, _ = evaluate(_pr(
        "",
        comments=[
            f"**[x]** — BLOCKING (head {_HEAD})\nSome point.",  # paren added -- the ONE flipped factor
            f"**[x]** — BLOCKING-CLEARED (head {_HEAD})\nSome point.",
        ],
    ))
    assert fixed_code == 0, "the SAME comment, only the missing paren added, must resolve"


def test_near_miss_fires_specifically_not_just_condition_a_reusing_the_message() -> None:
    """Tier 1: LOAD-BEARING falsification — the near-miss finding must
    be distinguishably present (not just "some RED reason or other").
    A decorated marker with NO sha-shaped hex run near "head" at all
    (so it could never have parsed as a real marker even undecorated)
    still gets flagged, proving condition C's own word-not-marker check
    fires independently of condition A's marker-vs-no-marker logic."""
    code, lines = evaluate(_pr(
        "", comments=["**[x]** — **BLOCKING** (head `not-hex-at-all`)\nSome point."],
    ))
    assert code != 0
    assert any("near-miss" in line for line in lines), (
        f"expected a near-miss finding in the output -- got {lines!r}"
    )


def test_bare_word_on_first_line_with_no_sha_at_all_is_a_near_miss() -> None:
    """Tier 1: a first line that says "BLOCKING" (uppercase, matching
    the marker's own case-sensitivity) with nothing resembling `(head
    <sha>)` at all — not a decoration problem, just no marker shape —
    is still a near-miss, not silence: the word signals INTENT even
    without a well-formed marker."""
    code, lines = evaluate(_pr(
        "", comments=["**[x]** — BLOCKING, needs a fix here."],
    ))
    assert code != 0
    assert any("near-miss" in line for line in lines)


def test_lowercase_blocking_in_prose_is_not_a_near_miss() -> None:
    """Tier 1: deny-side sibling for the WORD check itself — lowercase
    "blocking" (ordinary prose, case-sensitivity matches the marker
    regexes' own IGNORECASE-dropped posture) does not trigger condition
    C. Mirrors test_prose_mentioning_blocking_with_no_sha_is_not_a_raise
    for condition A."""
    code, lines = evaluate(_pr(
        "", comments=["**[x]** — my blocking is closed, thanks."],
    ))
    assert code == 0
    assert not any("near-miss" in line for line in lines)


def test_blocking_word_only_in_the_middle_of_the_body_is_not_a_near_miss() -> None:
    """Tier 1: LOAD-BEARING — #5522's own deny-side sibling requirement,
    verbatim: "本文の途中にBLOCKINGを含むだけのcommentはnear-missにし
    ない". A comment whose FIRST line is unrelated prose, with the word
    BLOCKING appearing only later in the body, must not be flagged —
    condition C is scoped to the same first-non-empty-line surface
    condition A's own regexes read, never the whole body. This issue's
    OWN body (architect named it explicitly) is the real-world instance
    of this shape."""
    code, lines = evaluate(_pr(
        "",
        comments=[
            "**[x]** — Thanks for the fix, LGTM overall.\n\n"
            "One note: the old BLOCKING gate implementation had a similar issue.",
        ],
    ))
    assert code == 0
    assert not any("near-miss" in line for line in lines)


def test_blocking_cleared_word_alone_with_no_sha_is_also_a_near_miss() -> None:
    """Tier 1: the bare-word check also catches a decorated/malformed
    BLOCKING-CLEARED attempt, not just BLOCKING — `_BARE_WORD` matches
    inside "BLOCKING-CLEARED" too (the hyphen is a word boundary)."""
    code, lines = evaluate(_pr(
        "", comments=["**[x]** — BLOCKING-CLEARED, see above."],
    ))
    assert code != 0
    assert any("near-miss" in line for line in lines)


def test_a_real_marker_never_also_reports_as_a_near_miss() -> None:
    """Tier 1: a comment that DOES parse as a real marker (condition A)
    must never ALSO be reported as a near-miss — the two are mutually
    exclusive by construction (near-miss only fires when the marker
    regex does NOT match)."""
    code, lines = evaluate(_pr(
        "",
        comments=[
            f"**[x]** — BLOCKING (head {_HEAD})\nSome point.",
            f"**[x]** — BLOCKING-CLEARED (head {_HEAD})\nSome point.",
        ],
    ))
    assert code == 0
    assert not any("near-miss" in line for line in lines)


# ---------------------------------------------------------------------------
# Fail-closed / CLI plumbing (unchanged shape from #5135)
# ---------------------------------------------------------------------------


def test_missing_body_fails_closed() -> None:
    """Tier 1: a missing PR body fails closed rather than passing vacuously."""
    code, _ = evaluate({"comments": [], "headRefOid": _HEAD})
    assert code != 0


def test_pr_supplier_failure_fails_closed() -> None:
    """Tier 1: a failed PR-payload supplier returns nonzero without patching."""
    def fail():
        raise OSError("network down")

    assert run_gate(fail) != 0


def test_fixture_cli_supports_both_states(tmp_path) -> None:
    """Tier 1: the CLI reports both a resolved and an unresolved fixture."""
    fixture = tmp_path / "pr.json"
    fixture.write_text(
        json.dumps(_pr(
            "- [x] 🔴 done",
            comments=["Fixed — done."],
        )),
        encoding="utf-8",
    )
    assert main(["--fixture", str(fixture)]) == 0
    fixture.write_text(json.dumps(_pr("- [ ] 🔴 todo")), encoding="utf-8")
    assert main(["--fixture", str(fixture)]) != 0


# ---------------------------------------------------------------------------
# #5522 acceptance point ② — TESTS-READ and BLOCKING treated the same
# ---------------------------------------------------------------------------


def test_tests_read_and_blocking_markers_both_survive_the_same_decoration() -> None:
    """Tier 1: LOAD-BEARING — #5522's own acceptance criterion, verbatim:
    "TESTS-READ と BLOCKING を同じ扱いにする test (どちらも掴む)". Before
    #5522, `check_tests_read_names_its_tree.py`'s SHA extraction already
    tolerated a backtick between the marker word and the hex run
    (structurally — its hex-boundary pattern never required strict
    adjacency to any literal character); `check_open_blocking_checkboxes.
    py`'s marker did not (the real incident this issue fixes). Both must
    now recognise the SAME backtick-decorated shape — driven entirely
    through each module's own PUBLIC surface (`evaluate`/
    `find_note_shas`), never a private regex object."""
    import scripts.check_tests_read_names_its_tree as _tests_read

    sha = "9862413f0abc123"

    # BLOCKING side, via the real public evaluate() — a decorated marker
    # WITH a matching CLEARED comment must resolve (green), the same
    # single-factor-flip claim test_backtick_decorated_sha_now_matches_
    # and_resolves already isolates in more detail; this test's own job
    # is only the cross-gate comparison below.
    blocking_code, _ = evaluate(_pr(
        "",
        comments=[
            f"**[x]** — **BLOCKING (head `{sha}`)**\nSome point.",
            f"**[x]** — BLOCKING-CLEARED (head {sha})\nSome point.",
        ],
        head=sha,
    ))
    assert blocking_code == 0, "decorated BLOCKING must resolve via the public evaluate() surface"

    # TESTS-READ side, via the real public find_note_shas().
    decorated_tests_read_line = f"**[x]** — TESTS-READ (head `{sha}`)"
    found_shas = _tests_read.find_note_shas(decorated_tests_read_line, known_oids=[sha])
    assert sha in found_shas, (
        f"TESTS-READ's own public SHA extraction must recognise the SAME "
        f"decorated SHA the BLOCKING gate's public evaluate() now does -- "
        f"got {found_shas!r}"
    )
