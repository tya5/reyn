"""#3811 first slice: the SMIL timeline sampler's supported profile.

Real SVG snippets throughout, no mocks — evaluated against the real
``sample_svg_at`` at real time values, asserting the resulting attribute
string on the real ``<xml>`` output. Violation reporting is asserted
alongside the snapshot wherever a construct is out of profile, per the
module's own "never silently drop" contract (lead-coder review, #3811).
"""
from __future__ import annotations

import re

from reyn.core.present.smil import sample_svg_at


def _attr(svg_out: str, tag: str, attr: str) -> "str | None":
    m = re.search(rf"<{tag}\b[^>]*\b{attr}=\"([^\"]*)\"", svg_out)
    return m.group(1) if m else None


# ── linear interpolation, scalar attributes ────────────────────────────────


def test_animate_opacity_interpolates_linearly_between_from_and_to() -> None:
    """Tier 1: an ``<animate>`` with ``from``/``to`` linearly interpolates
    a scalar attribute's value at the sampled time, not just at the
    endpoints."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" fill="freeze" />'
        "</circle></svg>"
    )
    out0, v0 = sample_svg_at(svg, 0.0)
    out_mid, v_mid = sample_svg_at(svg, 0.5)
    assert _attr(out0, "circle", "opacity") == "0"
    assert _attr(out_mid, "circle", "opacity") == "0.5"
    assert v0 == () and v_mid == ()


def test_fill_freeze_holds_the_end_value_past_the_active_duration() -> None:
    """Tier 1: ``fill=\"freeze\"`` holds the animation's END value once its
    active duration has elapsed, rather than resetting or continuing to
    extrapolate past ``to``."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" fill="freeze" />'
        "</circle></svg>"
    )
    out_end, _ = sample_svg_at(svg, 1.0)
    out_past, _ = sample_svg_at(svg, 5.0)
    assert _attr(out_end, "circle", "opacity") == "1"
    assert _attr(out_past, "circle", "opacity") == "1"


def test_fill_remove_default_resets_to_base_past_the_active_duration() -> None:
    """Tier 1: ``fill`` defaults to ``\"remove\"`` when absent — the base
    (first) value returns once the animation's active duration ends,
    distinct from ``freeze``'s hold-at-end behaviour above."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" />'
        "</circle></svg>"
    )
    out_past, _ = sample_svg_at(svg, 5.0)
    assert _attr(out_past, "circle", "opacity") == "0"


def test_repeat_count_absent_defaults_to_playing_once_not_forever() -> None:
    """Tier 1: SMIL's own default for an absent repeatCount is 1 (play once),
    not indefinite — a real bug this module's first draft had (repeatCount
    absent was treated the same as "indefinite", silently looping the
    animation forever instead of freezing/resetting once)."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" fill="freeze" />'
        "</circle></svg>"
    )
    out_past, _ = sample_svg_at(svg, 3.5)
    # If repeatCount silently defaulted to indefinite, t=3.5 (mid-cycle in a
    # looping 1s animation) would read back as 0.5, not the frozen end value.
    assert _attr(out_past, "circle", "opacity") == "1"


def test_repeat_count_indefinite_wraps_the_cycle() -> None:
    """Tier 1: an explicit ``repeatCount=\"indefinite\"`` wraps the timeline
    modulo its duration — sampled past the first cycle, it reads the SAME
    point in a later cycle, not the frozen or reset end state."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" '
        'repeatCount="indefinite" />'
        "</circle></svg>"
    )
    out_2_5, _ = sample_svg_at(svg, 2.5)
    assert _attr(out_2_5, "circle", "opacity") == "0.5"


# ── discrete calcMode, multi-value keyTimes ────────────────────────────────


def test_discrete_calc_mode_steps_at_each_key_time_not_interpolating() -> None:
    """Tier 1: ``calcMode=\"discrete\"`` STEPS to each keyframe's value at
    its ``keyTimes`` boundary rather than interpolating between them —
    asserted just before/at/after the step point to pin the exact
    boundary, not just "eventually changes"."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" values="0;1;0" keyTimes="0;0.5;1" '
        'calcMode="discrete" dur="1s" repeatCount="indefinite" />'
        "</circle></svg>"
    )
    before, _ = sample_svg_at(svg, 0.49)
    at_step, _ = sample_svg_at(svg, 0.5)
    after, _ = sample_svg_at(svg, 0.51)
    assert _attr(before, "circle", "opacity") == "0"
    assert _attr(at_step, "circle", "opacity") == "1"
    assert _attr(after, "circle", "opacity") == "1"


# ── colors ───────────────────────────────────────────────────────────────


def test_fill_color_interpolates_componentwise() -> None:
    """Tier 1: a ``fill``/``stroke`` color attribute interpolates each of
    R/G/B independently, not the hex string's characters."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="fill" from="#ff0000" to="#0000ff" dur="1s" '
        'fill="freeze" />'
        "</circle></svg>"
    )
    out_mid, _ = sample_svg_at(svg, 0.5)
    assert _attr(out_mid, "circle", "fill") == "#800080"


# ── animateTransform: multi-component value interpolation ─────────────────


def test_animate_transform_rotate_interpolates_all_three_components() -> None:
    """Tier 1: rotate's value is \"angle cx cy\" — three numbers, not one — a
    real bug this module's first draft had (only the first component's
    string was ever compared as a whole, silently freezing the whole
    transform at its start value for any multi-component target)."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10">'
        '<animateTransform attributeName="transform" type="rotate" '
        'from="0 5 5" to="360 5 5" dur="2s" repeatCount="indefinite" />'
        "</rect></svg>"
    )
    out_mid, _ = sample_svg_at(svg, 1.0)
    assert _attr(out_mid, "rect", "transform") == "rotate(180 5 5)"


def test_animate_transform_translate_interpolates_both_axes() -> None:
    """Tier 1: translate's value is \"x y\" — both components interpolate
    together, not just the attribute existing at all."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><rect width="10" height="10">'
        '<animateTransform attributeName="transform" type="translate" '
        'from="0 0" to="100 50" dur="1s" fill="freeze" />'
        "</rect></svg>"
    )
    out_mid, _ = sample_svg_at(svg, 0.5)
    assert _attr(out_mid, "rect", "transform") == "translate(50 25)"


# ── stroke-dasharray (the measured MVP-profile gap) ────────────────────────


def test_stroke_dasharray_set_pairs_with_animated_dashoffset() -> None:
    """Tier 1: #3811's measurement found stroke-dasharray missing from the
    original MVP allowlist — without it, an animated stroke-dashoffset (the
    draw-on-a-line idiom) has no dash pattern to offset against. Both must
    be supported together, and produce no violations."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><line x1="0" y1="0" x2="100" y2="0">'
        '<set attributeName="stroke-dasharray" to="100" begin="0s" />'
        '<animate attributeName="stroke-dashoffset" from="100" to="0" dur="1s" '
        'fill="freeze" />'
        "</line></svg>"
    )
    out_mid, violations = sample_svg_at(svg, 0.5)
    assert _attr(out_mid, "line", "stroke-dasharray") == "100"
    assert _attr(out_mid, "line", "stroke-dashoffset") == "50"
    assert violations == ()


# ── violations: never silently dropped ─────────────────────────────────────


def test_out_of_profile_element_animate_motion_is_reported_not_silent() -> None:
    """Tier 1: ``<animateMotion>`` is outside the supported element set —
    the sampler reports it as a violation (never silently ignores) and
    strips it from the frame regardless."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><g>'
        '<animateMotion path="M0,0 L10,10" dur="1s" />'
        "</g></svg>"
    )
    out, violations = sample_svg_at(svg, 0.5)
    assert any(
        v.kind == "out_of_profile_element" and v.element_tag == "animateMotion"
        for v in violations
    )
    # The animateMotion element itself is stripped from the static snapshot
    # (a frozen frame carries no animation directives) but the <g> survives.
    assert "<g" in out and "animateMotion" not in out


def test_event_begin_click_is_reported_and_leaves_the_base_value() -> None:
    """Tier 1: an event-triggered ``begin=\"click\"`` is outside the
    offset-only timing profile — reported as a violation, and the target
    attribute is left at its SVG-authored base value (the "ignore is safe"
    design note: a declarative animation's base value is always a valid
    still state)."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="0" cy="0" r="5">'
        '<animate attributeName="cx" begin="click" to="50" dur="1s" />'
        "</circle></svg>"
    )
    out, violations = sample_svg_at(svg, 0.5)
    assert any(v.kind == "event_or_syncbase_begin" for v in violations)
    assert _attr(out, "circle", "cx") == "0"


def test_d_path_morph_is_reported_as_an_unknown_attribute() -> None:
    """Tier 1: animating ``d`` (path-data morphing) is explicitly out of
    profile — reported, and the path's original ``d`` is left untouched."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0">'
        '<animate attributeName="d" to="M10 10" dur="1s" />'
        "</path></svg>"
    )
    out, violations = sample_svg_at(svg, 0.5)
    assert any(v.kind == "unknown_attribute" for v in violations)
    assert _attr(out, "path", "d") == "M0 0"


def test_calc_mode_spline_degrades_to_linear_and_is_reported() -> None:
    """Tier 1: ``calcMode=\"spline\"`` degrades to linear interpolation
    (still animates — a lossy but visible degrade) rather than being
    dropped outright, and the degrade itself is reported as a violation."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" calcMode="spline" '
        'keySplines="0.5 0 0.5 1" dur="1s" fill="freeze" />'
        "</circle></svg>"
    )
    out, violations = sample_svg_at(svg, 0.5)
    # Degraded to linear (not dropped): still animates, just without spline
    # easing — same "ignore is a lossy but visible degrade" design note.
    assert _attr(out, "circle", "opacity") == "0.5"
    assert any(v.kind == "unsupported_calc_mode" for v in violations)


def test_additive_sum_is_reported_but_does_not_block_the_base_timeline() -> None:
    """Tier 1: ``additive=\"sum\"`` is unsupported (replace is assumed
    instead) — reported as a violation, but the underlying from/to timeline
    still evaluates rather than being dropped entirely."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" '
        'fill="freeze" additive="sum" />'
        "</circle></svg>"
    )
    out, violations = sample_svg_at(svg, 0.5)
    assert _attr(out, "circle", "opacity") == "0.5"
    assert any(v.kind == "additive_or_accumulate" for v in violations)


def test_animation_elements_are_stripped_from_the_static_snapshot() -> None:
    """Tier 1: the snapshot is a frozen still frame — no <animate>/<set>/
    <animateTransform> directive belongs in it, evaluated or not."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5">'
        '<animate attributeName="opacity" from="0" to="1" dur="1s" />'
        "</circle></svg>"
    )
    out, _ = sample_svg_at(svg, 0.5)
    assert "<animate" not in out
