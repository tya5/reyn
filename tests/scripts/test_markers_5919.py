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


# ── first_line ────────────────────────────────────────────────────────────


def test_first_line_isolates_only_the_first_line():
    """Tier 1: the exclusion every marker in this module depends on — a
    document's line 2 onward must never even be handed to a pattern."""
    assert _MOD.first_line("TESTS-READ (head abc)\n\ngrounds go here") == "TESTS-READ (head abc)"


def test_first_line_of_a_single_line_string_is_itself():
    """Tier 1: the no-newline case — a string with nothing to split on
    returns unchanged, so callers need no special-case for a one-line
    comment/body."""
    assert _MOD.first_line("only one line") == "only one line"
