"""Tier 1: #5499 — the pure decision logic behind the axis-label gate.

See `scripts/axis_label_gate.py`'s own module docstring for why the
vocabulary is a small hand-written pattern (not derived from label
description or color — both were measured and rejected) and why both
directions of live-reconciliation must be checked, not just one.
"""
from __future__ import annotations

from scripts.axis_label_gate import (
    AXIS_LABEL_VOCABULARY,
    NEEDS_AXIS_LABEL,
    compute_label_action,
    format_needs_axis_comment,
    resolve_axis_vocabulary,
)


def test_the_vocabulary_constant_itself_is_not_empty() -> None:
    """Tier 1: accept-side / noise guard, lead-coder's #5499 condition ②
    — independent of any live reconciliation, the constant this whole
    gate is built on must be non-empty, or every downstream test below
    would pass vacuously."""
    assert AXIS_LABEL_VOCABULARY.prefixes
    assert AXIS_LABEL_VOCABULARY.exact


def test_resolve_matches_a_prefixed_live_label_not_individually_named() -> None:
    """Tier 1: a live label matching a PREFIX family (not itself in the
    hand-written exact set) must be picked up — this is the whole point
    of using prefixes instead of enumerating every roi:/thin:/priority:
    value."""
    live = ["roi:high", "bug", "documentation", *AXIS_LABEL_VOCABULARY.exact]
    vocab = resolve_axis_vocabulary(live)
    assert "roi:high" in vocab.matched
    assert vocab.ok


def test_resolve_matches_an_exact_named_live_label() -> None:
    """Tier 1: a live label in the hand-written exact set is matched."""
    live = ["bug", *AXIS_LABEL_VOCABULARY.exact]
    vocab = resolve_axis_vocabulary(live)
    assert "band" in vocab.matched
    assert vocab.ok


def test_resolve_does_not_match_an_unrelated_live_label() -> None:
    """Tier 1: deny-side sibling — a live label matching neither a prefix
    nor the exact set (the ordinary case: bug/documentation/enhancement/
    ...) must never be treated as an axis label."""
    vocab = resolve_axis_vocabulary(["bug", "documentation", "enhancement"])
    assert vocab.matched == frozenset()


def test_resolve_flags_red_when_every_named_axis_label_vanished() -> None:
    """Tier 1: #5482's "a scanned population must never legitimately
    reach 0" shape — if the live label list contains NONE of the
    vocabulary's names at all, ``ok`` must be False, never silently
    treated as "nothing to check"."""
    vocab = resolve_axis_vocabulary(["bug", "documentation"])
    assert not vocab.matched
    assert not vocab.ok


def test_resolve_flags_red_when_an_exact_name_vanished_even_if_others_match() -> None:
    """Tier 1: lead-coder's #5499 correction — a vanished EXACT name must
    fail (``ok`` False) even when OTHER vocabulary members still match
    live labels. A pre-fix version of this gate only checked the
    intersection-empty case and silently narrowed coverage otherwise
    (the same shape #5517 was blocked for) — this pins the fix."""
    live = ["roi:high", "thin:retrieval"]  # "band" et al. never appear
    vocab = resolve_axis_vocabulary(live)
    assert vocab.matched  # some real match exists...
    assert vocab.vanished_exact_names  # ...but an exact name is missing
    assert not vocab.ok  # ...so this must still be red


def test_resolve_reports_no_vanished_names_when_all_exact_names_are_live() -> None:
    """Tier 1: accept-side sibling to the previous test — when every
    exact name in the vocabulary is present live, nothing is reported as
    vanished."""
    live = set(AXIS_LABEL_VOCABULARY.exact) | {"bug"}
    vocab = resolve_axis_vocabulary(sorted(live))
    assert vocab.vanished_exact_names == ()
    assert vocab.ok


def test_action_is_add_when_issue_has_no_axis_label() -> None:
    """Tier 1: the MISSING direction — an issue with no axis label and no
    needs-axis label yet must be flagged."""
    assert compute_label_action(["bug"], frozenset({"band"})) == "add"


def test_action_is_remove_when_issue_gains_an_axis_label() -> None:
    """Tier 1: the PRESENT direction — the direction #5517's own review
    named as the one a same-shape gate must not skip: an issue that
    already carries needs-axis, and NOW also carries a real axis label,
    must have needs-axis removed, not left standing."""
    assert compute_label_action(["bug", NEEDS_AXIS_LABEL, "band"], frozenset({"band"})) == "remove"


def test_action_is_noop_when_issue_already_has_an_axis_label() -> None:
    """Tier 1: idempotency — an issue with a real axis label and no
    needs-axis label must not be touched."""
    assert compute_label_action(["bug", "band"], frozenset({"band"})) is None


def test_action_is_noop_when_needs_axis_already_correctly_present() -> None:
    """Tier 1: idempotency — an issue with no axis label that already
    carries needs-axis must not be re-added (no toggle-loop across
    opened/labeled/unlabeled firing repeatedly)."""
    assert compute_label_action(["bug", NEEDS_AXIS_LABEL], frozenset({"band"})) is None


def test_comment_names_the_vocabulary_actually_checked() -> None:
    """Tier 1: architect's #5499 condition ② — the needs-axis comment
    must enumerate the vocabulary used THIS run, so a false positive from
    a not-yet-pattern-matched new axis label is self-explaining rather
    than silent."""
    comment = format_needs_axis_comment(frozenset({"band", "roi:high"}))
    assert "band" in comment
    assert "roi:high" in comment
