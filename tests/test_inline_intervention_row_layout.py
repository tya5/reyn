"""Tier 2: inline-CUI intervention picker — inline-row layout + label casing
(owner UX round 2, following #2942's scrollback dedup).

Two pure decisions, both extracted to module-level functions so the
prompt_toolkit-closure glue in run_inline_input's above_region_frags /
above_region_height stays thin (per the project's own testing discipline for
this module: live Application redraw timing is verified via tmux, not unit
tests — but the ALGORITHMIC decisions feeding it are ordinary pure functions
and belong under Tier 2):

- ``is_intervention_region_key`` — is the above-region currently hosting a
  closed-set intervention ("iv:<id>") vs a command-UI picker ("cmd:...", e.g.
  /rewind) vs nothing? Only interventions get the new layout; the /rewind
  picker's existing rendering is untouched.
- ``iv_choices_fit_one_row`` — do a set of choice labels fit on one inline
  row, or does file_access_choices' recursive-path option (real paths run
  100+ chars) force the vertical fallback?

Plus ``_display_label`` (intervention_region.py) — the bracket-letter casing
normalization for the inline picker only, verified not to touch the
choice_id match key or leak into other renderers' labels.
"""
from __future__ import annotations

from reyn.interfaces.inline.app import is_intervention_region_key, iv_choices_fit_one_row
from reyn.interfaces.inline.intervention_region import InterventionElement, _display_label

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
    labels = ["[y]es", "[a]lways", "[n]o", "[n]ever"]
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


# ── _display_label (bracket-letter casing) ─────────────────────────────────


def test_uppercase_hotkey_bracket_is_lowercased_for_display():
    """Tier 2: "[A]lways" / "[N]ever" display as "[a]lways" / "[n]ever" in the
    inline picker — the case only matters for surfaces that read a typed
    hotkey (match_choice, case-sensitive), which the inline picker never does."""
    assert _display_label("[A]lways") == "[a]lways"
    assert _display_label("[N]ever") == "[n]ever"


def test_already_lowercase_bracket_is_unchanged():
    """Tier 2: idempotent on labels that are already lowercase — no-op, not
    a crash or a mangled result."""
    assert _display_label("[y]es") == "[y]es"
    assert _display_label("[j]ust this path always") == "[j]ust this path always"


def test_only_the_leading_bracket_is_touched():
    """Tier 2: a label whose body happens to contain another bracket-like
    substring is not mangled — only the LEADING hotkey bracket is normalized."""
    label = "[R]ecursive under '/Users/x/[test]' always"
    assert _display_label(label) == "[r]ecursive under '/Users/x/[test]' always"


def test_intervention_element_lines_show_normalized_casing():
    """Tier 2: end-to-end through the real InterventionElement — lines() (the
    picker's display rows) show normalized casing; on_select's choice_id
    (the authoritative match key) is completely unaffected since it never
    passed through the label at all."""
    captured = []
    el = InterventionElement(
        "iv-1",
        [("yes", "[y]es"), ("always", "[A]lways"), ("no", "[n]o"), ("never", "[N]ever")],
        lambda cid, label: captured.append(cid),
    )
    assert el.lines() == ["[y]es", "[a]lways", "[n]o", "[n]ever"]
    el.on_select(1)
    assert captured == ["always"]  # the real, unaltered choice id
