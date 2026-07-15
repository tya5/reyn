"""Tier 2: inline-CUI intervention picker — inline-row layout (owner UX round 2,
following #2942's scrollback dedup).

``is_intervention_region_key`` / ``iv_choices_fit_one_row`` are extracted to
module-level pure functions so the layout DECISION feeding
run_inline_input's above_region_frags / above_region_height closures is
unit-testable, consistent with this module's own discipline (live
Application redraw timing stays tmux-verified, not unit-tested):

- ``is_intervention_region_key`` — is the above-region currently hosting a
  closed-set intervention ("iv:<id>") vs a command-UI picker ("cmd:...", e.g.
  /rewind) vs nothing? Only interventions get the new layout; the /rewind
  picker's existing rendering is untouched.
- ``iv_choices_fit_one_row`` — do a set of choice labels fit on one inline
  row, or does file_access_choices' recursive-path option (real paths run
  100+ chars) force the vertical fallback?

NOTE — a prior revision of this file also pinned a bracket-letter casing
normalization (lowercasing "[A]lways"/"[N]ever" for inline display). A lead
review caught that as WRONG, not cosmetic: the input bar below this same
picker also accepts a typed answer via the case-sensitive `match_choice()`
(user_intervention.py), so "[A]lways" vs "[a]lways" is the real hotkey, not
visual noise — normalizing it would make a user typing "n" meaning to
permanently decline via "[N]ever" silently get the one-shot "[n]o" instead
(a false sense of a permanent deny). That transform was reverted; see
`test_intervention_element_preserves_label_casing_verbatim` below, which
exists specifically to catch a reintroduction of this bug.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import is_intervention_region_key, iv_choices_fit_one_row
from reyn.interfaces.inline.intervention_region import InterventionElement

# ── is_intervention_region_key ────────────────────────────────────────────


def test_iv_key_is_recognized():
    """Tier 2: the region_holder key convention _sync_region stamps for a
    closed-set intervention ("iv:<id>") is recognized."""
    assert is_intervention_region_key("iv:abc123") is True


def test_cmd_key_is_not_an_intervention():
    """Tier 2: the /rewind picker's key convention ("cmd:...") must NOT be
    treated as an intervention — it keeps the existing solid-fill rendering,
    unaffected by this redesign."""
    assert is_intervention_region_key("cmd:12345") is False


def test_none_key_is_not_an_intervention():
    """Tier 2: no active region content → not an intervention (region is
    inert/collapsed at this point regardless)."""
    assert is_intervention_region_key(None) is False


def test_empty_string_key_is_not_an_intervention():
    """Tier 2: falsy-but-not-None key — same as None, no crash on `.startswith`."""
    assert is_intervention_region_key("") is False


# ── iv_choices_fit_one_row ─────────────────────────────────────────────────


def test_generic_yn_choices_fit_one_row():
    """Tier 2: the common case — yes/always/no/never, all short — fits."""
    labels = ["[y]es", "[A]lways", "[n]o", "[N]ever"]
    assert iv_choices_fit_one_row(labels) is True


def test_empty_choice_list_does_not_fit():
    """Tier 2: no choices → no row to draw inline (falsy, not a crash)."""
    assert iv_choices_fit_one_row([]) is False


def test_file_access_recursive_choice_does_not_fit(tmp_path):
    """Tier 2: falsifying — the REAL file_access_choices() label shape (a
    genuine interpolated path) must NOT fit inline. This is the exact gap the
    owner caught in the mockup review; measured against a real path, the
    combined width was 141 chars."""
    from reyn.intervention_choices import file_access_choices

    real_dir = str(tmp_path / "reyn" / "interfaces" / "inline")
    choices = file_access_choices(real_dir)
    labels = [c.label for c in choices]
    assert iv_choices_fit_one_row(labels) is False


def test_shell_hook_and_elicitation_gate_choices_fit_one_row():
    """Tier 2: the other two short-label producers also fit — the rule isn't
    special-cased to generic_yn_choices alone."""
    from reyn.intervention_choices import elicitation_gate_choices, shell_hook_choices

    assert iv_choices_fit_one_row([c.label for c in shell_hook_choices()]) is True
    assert iv_choices_fit_one_row([c.label for c in elicitation_gate_choices()]) is True


def test_width_boundary_is_inclusive():
    """Tier 2: exact-fit boundary — a combined width equal to max_width fits;
    one character over does not (off-by-one guard on the packing decision)."""
    labels = ["a" * 34, "a" * 34]  # 34+34+2(gap) = 70
    assert iv_choices_fit_one_row(labels, max_width=70) is True
    labels_over = ["a" * 35, "a" * 34]  # 71
    assert iv_choices_fit_one_row(labels_over, max_width=70) is False


# ── label casing must be preserved verbatim (regression guard) ─────────────


def test_intervention_element_preserves_label_casing_verbatim():
    """Tier 2: security-relevant regression guard. InterventionElement.lines()
    must show EXACTLY the label intervention_choices.py defines — including
    the bracket-letter case, which IS the real hotkey `match_choice()`
    (case-sensitive) matches against when the same answer is typed into the
    input bar instead of picked via cursor+Enter. Any transform that changes
    "[A]lways"/"[N]ever" to lowercase would make the displayed letter stop
    matching the real hotkey (and could make "n" silently resolve to the
    one-shot "[n]o" for a user meaning the persistent "[N]ever") — a category
    of bug this test exists specifically to catch a reintroduction of."""
    el = InterventionElement(
        "iv-1",
        [("yes", "[y]es"), ("always", "[A]lways"), ("no", "[n]o"), ("never", "[N]ever")],
        lambda cid, label: None,
    )
    assert el.lines() == ["[y]es", "[A]lways", "[n]o", "[N]ever"]
