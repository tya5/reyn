"""Tier 1: `scripts/check_blocking_has_reread_note.py`'s own contract — a PR
carrying a BLOCKING comment but no TESTS-READ-shaped note naming its
CURRENT head is rejected (#5453).

Contract, not OS invariant: the subject is a repo gate's decision function,
protecting the same class house rule 8's own gate protects (a note
EXISTING is not the same as a note naming THIS tree), but on a DIFFERENT
trigger — "does this PR carry a BLOCKING comment" instead of "does it
touch tests/".

This module deliberately does NOT re-derive the marker/SHA logic — it
reuses `check_tests_read_names_its_tree.py`'s own functions (loaded the
same way the module under test loads them, via
``importlib.util.spec_from_file_location``), so a behavior change there
is exercised through the SAME code path this gate actually runs, not a
second, independently-written copy that could silently drift.

Payloads are built inline rather than fetched: `evaluate` is pure by
design so the decision is testable without GitHub.
"""
from __future__ import annotations

import importlib.util

from tests._support.paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "_check_blocking_has_reread_note",
    REPO_ROOT / "scripts" / "check_blocking_has_reread_note.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)


def _pr(*, comments=(), commits=(), head="ffffffff"):
    return {"comments": list(comments), "commits": list(commits), "headRefOid": head}


def _commit(oid):
    return {"oid": oid}


def _blocking(head_sha: str) -> dict:
    return {"body": f"**[lead-coder]** — BLOCKING (head {head_sha})\n\nsome point."}


# ── accept side — rule does not apply ───────────────────────────────────────

def test_no_blocking_comment_at_all_passes_without_needing_any_note():
    """Tier 1: the boundary itself — a PR with zero BLOCKING comments is
    entirely outside #5453's scope, regardless of whether a TESTS-READ
    note exists (lead-coder: "全PRには広げないでください")."""
    code, lines = _MOD.evaluate(_pr(comments=[{"body": "LGTM, nice work"}]))
    assert code == 0
    assert "does not apply" in "\n".join(lines)


def test_a_fresh_tests_read_note_naming_the_current_head_passes():
    """Tier 1: the accept path — a BLOCKING comment landed, and a
    TESTS-READ-shaped note names the PR's current head."""
    code, _ = _MOD.evaluate(_pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "BLOCKING-CLEARED (head bbbbbbb)\n\nfixed."},
            {"body": "TESTS-READ (head bbbbbbb)"},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 0


def test_a_reread_note_naming_the_current_head_also_passes():
    """Tier 1: architect's #5453 non-blocking recommendation — a `src`-only
    PR's comment claiming `TESTS-READ` would misstate what was actually
    read (nothing under `tests/` is even in the diff); `RE-READ (head
    <sha>)` is the honest form for that case, and this gate accepts it as
    an equal alternative to `TESTS-READ`, never a lesser one."""
    code, _ = _MOD.evaluate(_pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "BLOCKING-CLEARED (head bbbbbbb)\n\nfixed."},
            {"body": "RE-READ (head bbbbbbb)"},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 0


def test_one_tests_read_note_satisfies_both_this_gate_and_house_rule_8():
    """Tier 1: architect's own stated benefit of reusing the SAME marker —
    a PR that touches `tests/` (and so needs a `TESTS-READ` note for house
    rule 8 regardless) does not need a SECOND, differently-marked comment
    to also satisfy THIS gate. One `TESTS-READ` note, read through BOTH
    gates' own `evaluate`, passes both — proven by actually invoking
    `check_tests_read_names_its_tree.py`'s `evaluate` too (loaded the same
    way the module under test loads it), not merely asserted in prose."""
    import importlib.util as _ilu

    tests_read_spec = _ilu.spec_from_file_location(
        "_check_tests_read_names_its_tree_5453_dual",
        REPO_ROOT / "scripts" / "check_tests_read_names_its_tree.py",
    )
    tests_read_mod = _ilu.module_from_spec(tests_read_spec)
    assert tests_read_spec.loader is not None
    tests_read_spec.loader.exec_module(tests_read_mod)

    pr = _pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "BLOCKING-CLEARED (head bbbbbbb)\n\nfixed."},
            {"body": "TESTS-READ (head bbbbbbb)"},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb")],
        head="bbbbbbb",
    )
    blocking_code, _ = _MOD.evaluate(pr)
    # check_tests_read_names_its_tree.evaluate additionally needs `files`
    # (its own tests/-touching trigger) — absent here on purpose since
    # this test's whole point is that the SAME comment clears both gates
    # regardless of what triggered house rule 8's own check.
    tests_read_code, _ = tests_read_mod.evaluate(
        {**pr, "files": ["tests/scripts/test_check_doc_drift_5003.py"]}
    )
    assert blocking_code == 0
    assert tests_read_code == 0


# ── reject side ─────────────────────────────────────────────────────────────

def test_a_blocking_comment_with_no_tests_read_note_at_all_is_rejected():
    """Tier 1: the #5453 gap itself — a BLOCKING comment was raised and
    (per `check_open_blocking_checkboxes.py`'s own separate gate) may even
    be correctly BLOCKING-CLEARED by the author, but NOTHING ever named
    the current tree as re-read."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "BLOCKING-CLEARED (head bbbbbbb)\n\nsome point."},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "no TESTS-READ- or RE-READ-shaped" in "\n".join(lines)


def test_a_tests_read_note_naming_a_stale_head_is_rejected():
    """Tier 1: #5204's own ∃-over-current-head rule, reused here — a note
    naming an EARLIER commit does not satisfy a PR that has since moved,
    the same shape `check_tests_read_names_its_tree.py`'s own gate
    enforces for house rule 8."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "TESTS-READ (head aaaaaaa)"},
            {"body": "BLOCKING-CLEARED (head ccccccc)\n\nsome point."},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb"), _commit("ccccccc")],
        head="ccccccc",
    ))
    assert code == 1
    joined = "\n".join(lines)
    assert "aaaaaaa" in joined
    assert "ccccccc" in joined


def test_a_prose_mention_of_the_word_blocking_does_not_trigger_the_rule():
    """Tier 1: reused from `check_open_blocking_checkboxes.py`'s own
    #5318 false-positive fix — a review comment discussing whether
    something IS a blocking point (no `(head <sha>)` co-located) must not
    be misread as a formal raise."""
    code, lines = _MOD.evaluate(_pr(
        comments=[{"body": "not sure this is blocking, thoughts?"}],
    ))
    assert code == 0
    assert "does not apply" in "\n".join(lines)


def test_a_tests_read_note_with_no_resolvable_sha_is_rejected():
    """Tier 1: the #5096 shape, reused — a claim line landed but names
    nothing resolvable as a real commit of this PR."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("aaaaaaa"),
            {"body": "TESTS-READ. Passing."},
        ],
        commits=[_commit("aaaaaaa"), _commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "none of them names a" in "\n".join(lines)


# ── #5919: fixed-marker syntax, census + architect deny fixtures ────────────


def test_a_sentence_denying_a_re_read_is_not_read_as_one():
    """Tier 1: deny side — #5919's own census input. Before the fix,
    `_RE_READ_MARKER.search` matched "RE-READ" anywhere on the first line,
    so a comment DENYING that a re-read is needed read as the note itself
    ("No RE-READ (head start) needed here..." even carries a SHA-shaped
    token, "start", that this gate must not treat as a real claim)."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("bbbbbbb"),
            {"body": "No RE-READ (head start) needed here, this is a docs-only typo fix."},
        ],
        commits=[_commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "no TESTS-READ- or RE-READ-shaped" in "\n".join(lines)


def test_a_backtick_fenced_re_read_marker_is_not_a_claim():
    """Tier 1: architect (#5919) — column-0 alone is not enough; a
    backtick-fenced marker can open a line while still being a mention."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("bbbbbbb"),
            {"body": "`RE-READ (head bbbbbbb)` is the syntax, per the gate."},
        ],
        commits=[_commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "no TESTS-READ- or RE-READ-shaped" in "\n".join(lines)


def test_a_quoted_re_read_marker_is_not_a_claim():
    """Tier 1: architect (#5919) — a markdown-quoted line."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("bbbbbbb"),
            {"body": "> RE-READ (head bbbbbbb)"},
        ],
        commits=[_commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "no TESTS-READ- or RE-READ-shaped" in "\n".join(lines)


# ── #5919 stage 3: borrow consolidation acceptance ──────────────────────────


def test_check_open_blocking_checkboxes_is_not_loaded_by_this_gate_at_all():
    """Tier 1: LOAD-BEARING acceptance (architect + lead-coder, #5919 stage
    3) — this module no longer imports `check_open_blocking_checkboxes.py`
    as `_blocking` at all (a NameError, not merely absent today); everything
    this gate needed from it was recognition, and has moved to
    `_markers.py`. The general "no gate module reads a private attribute
    off a dynamically-loaded sibling" census (of which the OLD, narrower
    form of this exact test was a hand-maintained, 2-alias special case)
    now lives repo-wide, both its populations derived rather than typed by
    hand, in `test_5967_gate_module_private_borrow_census.py` (#5967 — the
    #5919-stage-3-shaped test itself was #5967's own motivating instance:
    it hardcoded `check_blocking_has_reread_note.py` and `{"_tests_read",
    "_blocking"}`, so a THIRD file/alias would have gone unseen)."""
    import ast

    tree = ast.parse(
        (REPO_ROOT / "scripts" / "check_blocking_has_reread_note.py").read_text(encoding="utf-8"),
    )
    assert "_blocking" not in {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }, (
        "check_open_blocking_checkboxes.py must no longer be loaded as "
        "_blocking at all -- everything this gate needed from it was "
        "recognition, and has moved to _markers.py"
    )


def test_strip_moving_marker_blocking_back_to_private_breaks_this_gate():
    """Tier 1: strip witness for the acceptance above — reproduces the
    EXACT failure shape #5919 stage 2->3 hit for real (11 failed CI
    checks, lead-coder's own report): removing a name from `_markers.py`'s
    public surface (simulating "moved back to private" / "renamed without
    updating the borrower") must make this gate's own
    `_has_blocking_comment` raise, not silently degrade to "no BLOCKING
    comment found" (that direction would be the SAME fail-open class
    #5919 exists to close, just moved one level)."""
    original = _MOD._markers.MARKER_BLOCKING
    del _MOD._markers.MARKER_BLOCKING
    try:
        import pytest

        with pytest.raises(AttributeError):
            _MOD._has_blocking_comment(["**[x]** — BLOCKING (head abc1234)"])
    finally:
        _MOD._markers.MARKER_BLOCKING = original


def test_strip_moving_note_marker_back_to_private_breaks_this_gate():
    """Tier 1: the same strip witness for `_markers.NOTE_MARKER` — the
    OTHER name this gate reuses across the module boundary."""
    original = _MOD._markers.NOTE_MARKER
    del _MOD._markers.NOTE_MARKER
    try:
        import pytest

        with pytest.raises(AttributeError):
            _MOD._is_reread_note("**[x]** — TESTS-READ (head abc1234)")
    finally:
        _MOD._markers.NOTE_MARKER = original


def test_a_blocking_comment_opening_with_a_blank_line_is_still_recognised():
    """Tier 1: LOAD-BEARING — #5919 stage 3's own real finding (architect
    requested direct verification, not inference from the two pre-stage-3
    implementations' textual difference). A BLOCKING comment prefixed
    with one blank line before its marker line must still be RAISED —
    the exact shape `_markers.first_nonempty_line` (not the pre-stage-3
    bare split) exists to fix. Driven through the public `evaluate()`,
    not the private `_has_blocking_comment` directly: a PR with no
    re-read note at all reports RED "carries a BLOCKING comment" only if
    the blank-line-prefixed comment above was actually recognised as one
    — if it were missed, this PR would instead report OK ("carries no
    BLOCKING comment"), the opposite, silently-wrong verdict."""
    code, lines = _MOD.evaluate(_pr(
        comments=[{"body": "\n**[lead-coder]** — BLOCKING (head bbbbbbb)\n\nsome point."}],
        commits=[_commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 1
    assert "carries a BLOCKING comment but no" in "\n".join(lines)


def test_a_reread_note_opening_with_a_blank_line_is_still_recognised_as_a_claim():
    """Tier 1: LOAD-BEARING — the house-rule-8/#5453 side of the same
    fail-open. A previously-uncounted 5th silent-miss instance in #5919's
    own original census (architect's own self-correction on the issue
    thread): a TESTS-READ/RE-READ note opening with a blank line used to
    be silently treated as "no note", which this gate would then read as
    "carries a BLOCKING comment but no note at all" -- RED for the wrong
    reason, or worse, GREEN if some other unrelated note happened to
    satisfy it. Verified end to end through evaluate(), not just the
    isolated marker/line-selection primitives."""
    code, lines = _MOD.evaluate(_pr(
        comments=[
            _blocking("bbbbbbb"),
            {"body": "\n**[e2e-coder]** — TESTS-READ (head bbbbbbb)\n\ngrounds go here"},
        ],
        commits=[_commit("bbbbbbb")],
        head="bbbbbbb",
    ))
    assert code == 0
    assert "names the current head" in "\n".join(lines)
