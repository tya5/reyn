"""Tier 1: `scripts/_markers.py`'s own contract — the shared marker-detection
primitives four gates now build their declaration/exemption regexes from
(#5919 stage 1).

Each primitive's job is narrow: produce a pattern that ordinary prose
cannot satisfy by accident, while still matching every real, deliberate
declaration shape already in use across the repo (bare marker, role-prefixed
marker, HTML-comment marker, fixed source-comment marker). These tests pin
that contract directly against `_markers.py`, independent of any one gate
that consumes it — a change here is exactly the "next gate reopens the same
hole" risk the module's own docstring names, so its own behaviour needs its
own witness.
"""
from __future__ import annotations

import importlib.util
import re

from tests._support.paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "_check_markers_5919", REPO_ROOT / "scripts" / "_markers.py",
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)


# ── role_prefixed_marker ─────────────────────────────────────────────────


def test_bare_keyword_at_line_start_matches():
    """Tier 1: accept — the keyword alone, opening the line."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert pattern.match("TESTS-READ (head abc1234)")


def test_role_prefixed_keyword_matches():
    """Tier 1: accept — the CLAUDE.md rule-2 role prefix immediately
    followed by the keyword."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert pattern.match("**[e2e-coder]** — TESTS-READ (B: independent) (head abc1234)")


def test_a_denying_sentence_does_not_match():
    """Tier 1: deny — #5919's own census input. The keyword appears on
    the line, but does not OPEN it."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert not pattern.match(
        "This PR is not ready for a TESTS-READ note yet — still investigating 9cc1006.",
    )


def test_a_backtick_fenced_marker_does_not_match():
    """Tier 1: deny — architect (#5919): column-0 alone is not enough,
    since a backtick can also open a line."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert not pattern.match("`TESTS-READ (head abc1234)`")


def test_a_quoted_marker_does_not_match():
    """Tier 1: deny — a markdown blockquote opens with `>`, not the
    keyword or role prefix."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert not pattern.match("> TESTS-READ (head abc1234)")


def test_a_mid_sentence_mention_does_not_match():
    """Tier 1: deny — the keyword discussed mid-sentence."""
    pattern = _MOD.role_prefixed_marker(r"TESTS-READ(?:Y)?\b")
    assert not pattern.match("The reviewer marks it as TESTS-READ once done.")


# ── html_comment_marker ──────────────────────────────────────────────────


def test_html_comment_marker_matches_the_real_syntax():
    """Tier 1: accept — the exact `<!-- closing-check: discussing #N -->`
    shape `check_pr_closing_intent.py` uses."""
    pattern = _MOD.html_comment_marker("closing-check", r"discussing\s+((?:#\d+[\s,]*)+?)")
    match = pattern.search("prose <!-- closing-check: discussing #2620 #2972 --> more prose")
    assert match is not None
    assert match.group(1).strip() == "#2620 #2972"


def test_html_comment_marker_does_not_match_prose_about_it():
    """Tier 1: deny — a sentence merely discussing the tag/payload words
    without the HTML-comment syntax itself must not match."""
    pattern = _MOD.html_comment_marker("closing-check", r"discussing\s+((?:#\d+[\s,]*)+?)")
    assert pattern.search("this PR is closing-check discussing #2620, sort of") is None


# ── fixed_comment_marker ─────────────────────────────────────────────────


def test_fixed_comment_marker_matches_at_the_comments_own_start():
    """Tier 1: accept — `# EXEMPT: <name>` opening its own comment line."""
    pattern = _MOD.fixed_comment_marker("EXEMPT")
    match = pattern.match("# EXEMPT: out_of_process_reyn")
    assert match is not None
    assert match.group(1) == "out_of_process_reyn"


def test_fixed_comment_marker_does_not_match_a_mention_inside_prose():
    """Tier 1: deny — #5919's own census input for
    `check_subprocess_reyn_pin.py`: a comment merely mentioning the
    exempted name, not opening with the directive."""
    pattern = _MOD.fixed_comment_marker("EXEMPT")
    assert pattern.match(
        "# this subprocess call never touches out_of_process_reyn -- plain smoke check",
    ) is None


def test_fixed_comment_marker_does_not_match_mid_comment():
    """Tier 1: deny — the directive word appearing after other text on
    the same comment line."""
    pattern = _MOD.fixed_comment_marker("EXEMPT")
    assert pattern.match("# see EXEMPT: below for the real one") is None


# ── undecorated_after_role_prefix (#5919 stage 2) ───────────────────────────

_DECORATION = re.compile(r"[`*]")


def test_undecorated_after_role_prefix_strips_decoration_around_the_marker():
    """Tier 1: LOAD-BEARING — the exact real shape that broke
    `check_open_blocking_checkboxes.py`'s own anchored marker before this
    function existed: a role-prefixed, ADDITIONALLY bold-wrapped marker.
    The role prefix's own literal `**[...]** — ` must survive untouched
    (or `role_prefixed_marker`'s anchor could never match afterward), while
    the decoration AROUND the marker itself is still stripped."""
    line = "**[lead-coder]** — **BLOCKING (head `abc1234`)**"
    result = _MOD.undecorated_after_role_prefix(line, _DECORATION)
    assert result == "**[lead-coder]** — BLOCKING (head abc1234)"


def test_undecorated_after_role_prefix_with_no_role_prefix_strips_the_whole_line():
    """Tier 1: a bare marker (no role prefix at all) falls back to
    stripping the whole line — the SAME behaviour as an unconditional
    strip, since there is no role-prefix literal to protect."""
    line = "**BLOCKING (head `abc1234`)**"
    result = _MOD.undecorated_after_role_prefix(line, _DECORATION)
    assert result == "BLOCKING (head abc1234)"


def test_undecorated_after_role_prefix_a_blanket_strip_would_break_the_role_prefix():
    """Tier 1: the deny-side witness for WHY this function exists — an
    unconditional whole-line strip (the naive fix) erases the role
    prefix's own required `**[...]**` syntax, which
    `_markers.ROLE_PREFIX` needs literally intact to match at all."""
    line = "**[lead-coder]** — **BLOCKING (head `abc1234`)**"
    naive_whole_line_strip = _DECORATION.sub("", line)
    assert naive_whole_line_strip == "[lead-coder] — BLOCKING (head abc1234)"
    assert re.match(_MOD.ROLE_PREFIX, naive_whole_line_strip) is None, (
        "a blanket strip must destroy the role prefix's own literal "
        "syntax -- this is the defect undecorated_after_role_prefix fixes"
    )
    assert re.match(
        _MOD.ROLE_PREFIX,
        _MOD.undecorated_after_role_prefix(line, _DECORATION),
    ) is not None


# ── first_nonempty_line ───────────────────────────────────────────────────


def test_first_nonempty_line_isolates_only_the_first_line():
    """Tier 1: the exclusion every marker in this module depends on — a
    document's line 2 onward must never even be handed to a pattern."""
    assert (
        _MOD.first_nonempty_line("TESTS-READ (head abc)\n\ngrounds go here")
        == "TESTS-READ (head abc)"
    )


def test_first_nonempty_line_of_a_single_line_string_is_itself():
    """Tier 1: the no-newline case — a string with nothing to split on
    returns unchanged, so callers need no special-case for a one-line
    comment/body."""
    assert _MOD.first_nonempty_line("only one line") == "only one line"


def test_first_nonempty_line_skips_a_leading_blank_line():
    """Tier 1: LOAD-BEARING — #5919 stage 3's own real finding (architect
    requested this be verified directly, not merely inferred from the
    two pre-stage-3 implementations' textual difference): a comment
    opening with a blank line before its real marker must still be
    read. Strip witness: reverting to the pre-stage-3 bare
    ``text.split("\\n", 1)[0]`` returns an EMPTY string for this exact
    input, which then fails every marker regex trivially — a real,
    previously-shipping fail-open in house rule 8's own gate (a 5th
    silent-miss instance #5919's original census undercounted),
    confirmed empirically by constructing this input and running it
    through the real gate before this fix landed."""
    text = "\n**[e2e-coder]** — TESTS-READ (head abc1234)\n\ngrounds go here"
    assert _MOD.first_nonempty_line(text) == "**[e2e-coder]** — TESTS-READ (head abc1234)"


def test_first_nonempty_line_of_an_all_blank_string_is_empty():
    """Tier 1: a comment with no real content at all (never a real input,
    but a caller must not crash on it) reports no line, not a crash or a
    stray blank string."""
    assert _MOD.first_nonempty_line("\n\n   \n") == ""


# ── DECORATION / undecorated ─────────────────────────────────────────────


def test_undecorated_strips_backtick_and_asterisk_unconditionally():
    """Tier 1: the unconditional sibling of undecorated_after_role_prefix
    — for text that is never itself a role-prefix candidate (a near-miss
    watcher's own bare-word check)."""
    assert _MOD.undecorated("**BLOCKING (head `abc1234`)**") == "BLOCKING (head abc1234)"


def test_undecorated_strips_a_role_prefixes_own_asterisks_too():
    """Tier 1: deny-side witness for WHY undecorated_after_role_prefix
    exists as a SEPARATE function — the unconditional undecorated() does
    NOT preserve a role prefix's own literal syntax, unlike its
    role-prefix-aware sibling (see that function's own tests)."""
    line = "**[lead-coder]** — BLOCKING (head abc1234)"
    assert _MOD.undecorated(line) == "[lead-coder] — BLOCKING (head abc1234)"


# ── MARKER_BLOCKING / MARKER_CLEARED (#5919 stage 3) ─────────────────────


def test_marker_blocking_matches_the_real_shape():
    """Tier 1: accept — the exact `BLOCKING (head <sha>)` co-located shape,
    role-prefixed, as posted in this repo today."""
    assert _MOD.MARKER_BLOCKING.match("**[lead-coder]** — BLOCKING (head abc1234)")


def test_marker_blocking_does_not_match_blocking_cleared():
    """Tier 1: deny — MARKER_BLOCKING must never accept a CLEARED comment
    (the negative lookahead this shares with the pre-stage-3 regex)."""
    assert not _MOD.MARKER_BLOCKING.match("BLOCKING-CLEARED (head abc1234)")


def test_marker_cleared_matches_the_real_shape():
    """Tier 1: accept — the CLEARED counterpart."""
    assert _MOD.MARKER_CLEARED.match("**[lead-coder]** — BLOCKING-CLEARED (head abc1234)")


def test_marker_blocking_denies_a_negating_sentence():
    """Tier 1: deny — #5919's own census input, applied to the moved
    instance."""
    assert not _MOD.MARKER_BLOCKING.match("**[x]** — my blocking is closed, thanks")


# ── NOTE_MARKER (#5919 stage 3, moved from check_tests_read_names_its_tree.py) ──


def test_note_marker_matches_the_real_tests_read_shape():
    """Tier 1: accept — the exact shape house rule 8 requires, now the
    SAME instance both check_tests_read_names_its_tree.py and
    check_blocking_has_reread_note.py read."""
    assert _MOD.NOTE_MARKER.match("**[e2e-coder]** — TESTS-READ (head abc1234)")


def test_note_marker_tolerates_the_testsready_typo():
    """Tier 1: accept — TESTS-READY, the typo several sessions produce."""
    assert _MOD.NOTE_MARKER.match("TESTS-READY (head abc1234)")


def test_note_marker_denies_a_denying_sentence():
    """Tier 1: deny — the exact #5919 census input this marker exists to
    reject."""
    assert not _MOD.NOTE_MARKER.match(
        "This PR is not ready for a TESTS-READ note yet — still investigating 9cc1006.",
    )
