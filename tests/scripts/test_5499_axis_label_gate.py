"""Tier 1: #5499/#5519 — the pure decision logic behind the axis-label gate.

See `scripts/axis_label_gate.py`'s own module docstring for the full design
history: #5499's first landing carried a hand-written name/prefix
vocabulary (description-text and label-color derivation were both measured
and rejected at the time); #5519 replaced it with a description MARKER
every axis label now carries on GitHub, so the vocabulary lives entirely in
live label descriptions and this script no longer hand-enumerates any
label name.

Only the pure marker-check (:func:`label_declares_axis`) and the pure
decision functions built on top of it are tested here — lead-coder's own
caution (#5519): a test that hits the live `gh api` label list would be
network-dependent and CI-flaky. The live reconciliation
(:func:`resolve_axis_vocabulary` against a real label list) is exercised
by the gate script itself at run time, not by this file.
"""
from __future__ import annotations

from scripts.axis_label_gate import (
    AXIS_DESCRIPTION_MARKER,
    NEEDS_AXIS_LABEL,
    compute_label_action,
    format_needs_axis_comment,
    label_declares_axis,
    resolve_axis_vocabulary,
)


def test_the_marker_constant_itself_is_not_empty() -> None:
    """Tier 1: accept-side / noise guard — independent of any live
    reconciliation, the marker this whole gate is built on must be a real,
    non-empty string, or every downstream test below would pass vacuously
    (a blank marker would make ``str.endswith("")`` always True)."""
    assert AXIS_DESCRIPTION_MARKER


def test_label_declares_axis_when_description_ends_with_the_marker() -> None:
    """Tier 1: the accept side — a description ending with the marker,
    whatever text precedes it (mirrors the real repo labels: some already
    mention "軸" in prose, some don't — the marker is what counts, not the
    prose around it)."""
    assert label_declares_axis(f"憲章の cross-cutting band に掛かる {AXIS_DESCRIPTION_MARKER}")
    assert label_declares_axis(f"plain english description {AXIS_DESCRIPTION_MARKER}")
    # Trailing whitespace after the marker must not silently un-mark it.
    assert label_declares_axis(f"trailing space after marker {AXIS_DESCRIPTION_MARKER}  ")


def test_label_declares_axis_is_false_without_the_marker() -> None:
    """Tier 1: deny-side pin — the property #5519's own acceptance
    criterion names explicitly: adding a brand-new label whose
    description does NOT carry the marker must never be picked up as an
    axis label, however plausible its own prose sounds (including prose
    that mentions "軸"/"axis" without the structured marker) — a "treats
    everything as axis" implementation would pass every OTHER test in
    this file but fail this one."""
    assert not label_declares_axis("Something isn't working")
    assert not label_declares_axis("")
    assert not label_declares_axis(None)
    # Mentions the concept in prose but never carries the marker itself —
    # this is the exact #5499-era gap #5519 exists to close: prose-text
    # scanning cannot be trusted, only the structured marker can.
    assert not label_declares_axis("この issue は優先順位の軸に関わる")
    # The marker appears, but not at the END — must not match either
    # (avoids treating "[axis] this label's real subject" as declared,
    # which would let an unrelated axis-shaped label slip through unmarked).
    assert not label_declares_axis(f"{AXIS_DESCRIPTION_MARKER} not trailing")


def test_resolve_matches_only_labels_whose_description_carries_the_marker() -> None:
    """Tier 1: :func:`resolve_axis_vocabulary` end to end — a marked label
    is matched, an unmarked one (however label-like its NAME looks) is
    not, and there is no hand-written name list involved anywhere in this
    path."""
    live = [
        {"name": "band", "description": f"cross-cutting band {AXIS_DESCRIPTION_MARKER}"},
        {"name": "priority:next", "description": f"次に取るべき {AXIS_DESCRIPTION_MARKER}"},
        {"name": "bug", "description": "Something isn't working"},
        {"name": "roi:high", "description": None},  # marker-less: not matched
    ]
    vocab = resolve_axis_vocabulary(live)
    assert vocab.matched == frozenset({"band", "priority:next"})
    assert "bug" not in vocab.matched
    assert "roi:high" not in vocab.matched, (
        "a plausibly-named label with no marker in its description must "
        "never be treated as an axis label — this is the deny-side pin "
        "#5519's acceptance criteria name explicitly"
    )
    assert vocab.ok


def test_resolve_flags_red_when_no_live_label_carries_the_marker() -> None:
    """Tier 1: #5482's "a scanned population must never legitimately
    reach 0" shape — covers BOTH "every marked label was deleted" and
    "the marker was stripped from every description" identically, since
    there is no separate hand-list side to distinguish them anymore."""
    live = [
        {"name": "bug", "description": "Something isn't working"},
        {"name": "documentation", "description": "Docs"},
    ]
    vocab = resolve_axis_vocabulary(live)
    assert not vocab.matched
    assert not vocab.ok


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
    an unmarked label is self-explaining rather than silent."""
    comment = format_needs_axis_comment(frozenset({"band", "roi:high"}))
    assert "band" in comment
    assert "roi:high" in comment
    assert AXIS_DESCRIPTION_MARKER in comment
