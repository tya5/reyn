"""SMIL timeline sampler (#3811 first slice) — a declarative-subset evaluator,
NOT a SMIL runtime.

Lightweight rasterizers (cairosvg, resvg) render only an SVG's static initial
state; SMIL/CSS/script animation is explicitly out of scope for them. A full
SMIL runtime (event/syncbase triggers, spline easing, additive/accumulate,
motion-along-path) is a stateful time+event model, orthogonal to a pure
rasterizer. This module evaluates neither — it evaluates a measured,
LLM-authored SUBSET of SMIL (#3811 comment thread: 12 prompts x 2 models,
real reyn-run LLM output, not synthetic fixtures) at a single point in time
``t``, producing a STATIC svg snapshot a rasterizer can already handle.

The supported profile (measured coverage: 29/32 real animation elements,
91%) is exactly the design notes on #3811:

- Elements: ``<animate>``, ``<animateTransform>`` (translate/scale/rotate),
  ``<set>``.
- Timing: ``dur``, ``begin`` (OFFSET-ONLY — ``begin="click"``/syncbase is the
  "no events" boundary, measured 0/32 in real output), ``repeatCount``,
  ``fill="freeze"``.
- Interpolation: ``from``/``to``/``values`` + ``keyTimes`` + ``calcMode``
  ``linear``/``discrete`` (``spline`` measured 1/32 — degrade to linear, see
  :func:`sample_svg_at`'s violation reporting, not silently ignored).
- Animatable attributes (allowlist, with their value type): ``opacity``
  (number), ``fill``/``stroke`` (color), ``x``/``y``/``cx``/``cy``/``r``/
  ``width``/``height`` (length, unitless), ``transform`` (via
  ``animateTransform`` only), ``stroke-dashoffset`` (number),
  ``stroke-dasharray`` (number — #3811's measured MVP-profile gap: without
  it, ``stroke-dashoffset``'s draw-on-a-line idiom has no dash pattern to
  offset against, so allowing one without the other is not a smaller
  profile, it is a broken one).

Explicitly OUT of this slice (each is a measured absence, not a guess —
0/32 or an isolated, out-of-use-case sample in the #3811 measurement):
``animateMotion``, ``calcMode="spline"``, ``additive``/``accumulate``,
path-data (``d``) morphing, event/syncbase ``begin``.

★ Every construct this sampler does not evaluate is dropped from the
snapshot (the target attribute stays at its SVG-authored base value — safe,
because a declarative animation's base value is always a valid still state
in its own right, same "ignore is safe" reasoning #3811's design notes give
for exactly this case). Dropping is never silent: :func:`sample_svg_at`
ALWAYS returns the violations it found alongside the snapshot, so a caller
that only looks at the snapshot has to affirmatively discard them to lose
the record — no separate opt-in validator pass to forget to run (lead-coder
review, #3811: this repo hit three "the ignore mechanism itself has no
trace" incidents in one night — a validator that runs only if asked is the
same shape).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

_SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", _SVG_NS)

_ANIMATE_TAGS = frozenset(
    {f"{{{_SVG_NS}}}animate", f"{{{_SVG_NS}}}set", f"{{{_SVG_NS}}}animateTransform"}
)
# SMIL elements this sampler recognizes as animation directives AT ALL, so it
# can tell "an out-of-profile animation element" (a violation) apart from
# "an ordinary SVG element that happens to sit near one" (not this module's
# concern).
_ALL_SMIL_ANIMATION_TAGS = _ANIMATE_TAGS | {
    f"{{{_SVG_NS}}}animateMotion",
    f"{{{_SVG_NS}}}animateColor",  # SVG2-deprecated, but real authors still emit it
    f"{{{_SVG_NS}}}mpath",
}

_LENGTH_ATTRS = frozenset({"x", "y", "cx", "cy", "r", "width", "height"})
_NUMBER_ATTRS = frozenset({"opacity", "stroke-dashoffset", "stroke-dasharray"})
_COLOR_ATTRS = frozenset({"fill", "stroke"})
# The full allowlist — anything else on attributeName is a violation.
_ALLOWED_ATTRS = _LENGTH_ATTRS | _NUMBER_ATTRS | _COLOR_ATTRS

_TRANSFORM_KINDS = frozenset({"translate", "scale", "rotate", "skewX", "skewY"})

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_COLOR_RE = re.compile(
    r"^rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)$", re.IGNORECASE
)


@dataclass(frozen=True)
class SmilViolation:
    """One construct this sampler did not evaluate — the target attribute
    was left at its base (unanimated) value instead. Never discarded
    internally; always returned alongside the snapshot (module docstring)."""

    kind: str
    detail: str
    element_tag: str


def _local_tag(elem: "ET.Element") -> str:
    tag = elem.tag
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_offset_seconds(value: "str | None", *, default: float = 0.0) -> "float | None":
    """Parse a ``begin``/``dur`` offset like ``\"1.5s\"``/``\"200ms\"``/``\"2\"``.

    Returns ``None`` if ``value`` is not a plain numeric offset (event/
    syncbase forms like ``\"click\"``/``\"x.end\"``/``\"indefinite\"`` all
    fail this parse — the caller reports that as a violation, never guesses
    a fallback offset for them)."""
    if value is None:
        return default
    value = value.strip()
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)(s|ms)?", value)
    if not m:
        return None
    num = float(m.group(1))
    return num / 1000.0 if m.group(2) == "ms" else num


def _parse_color(value: str) -> "tuple[float, float, float] | None":
    value = value.strip()
    m = _HEX_COLOR_RE.match(value)
    if m:
        hexpart = m.group(1)
        if len(hexpart) == 3:
            hexpart = "".join(c * 2 for c in hexpart)
        return tuple(int(hexpart[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    m = _RGB_COLOR_RE.match(value)
    if m:
        return (float(m.group(1)), float(m.group(2)), float(m.group(3)))
    return None


def _format_color(rgb: "tuple[float, float, float]") -> str:
    r, g, b = (max(0, min(255, round(c))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lerp(a: float, b: float, frac: float) -> float:
    return a + (b - a) * frac


def _format_number(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def _interpolate_value(a: str, b: str, frac: float, attr_name: str) -> str:
    if attr_name in _COLOR_ATTRS:
        ca, cb = _parse_color(a), _parse_color(b)
        if ca is None or cb is None:
            return a if frac < 1.0 else b
        return _format_color((_lerp(ca[0], cb[0], frac), _lerp(ca[1], cb[1], frac), _lerp(ca[2], cb[2], frac)))
    # animateTransform values (rotate="angle cx cy", translate="x y",
    # scale="sx sy") are space-separated NUMBER LISTS, not a single scalar —
    # interpolate each component independently. A plain scalar attribute
    # (opacity, x, r, ...) is the len==1 case of the same code path.
    parts_a, parts_b = a.split(), b.split()
    if len(parts_a) != len(parts_b) or not parts_a:
        return a if frac < 1.0 else b
    try:
        floats_a = [float(p) for p in parts_a]
        floats_b = [float(p) for p in parts_b]
    except ValueError:
        return a if frac < 1.0 else b
    return " ".join(
        _format_number(_lerp(fa, fb, frac)) for fa, fb in zip(floats_a, floats_b)
    )


def _keyframes(elem: "ET.Element") -> "tuple[list[str], list[float]] | None":
    """Return (values, key_times) for an animate/set element, or None if it
    has neither a from/to pair nor a values list (a violation the caller
    reports)."""
    values_attr = elem.get("values")
    if values_attr is not None:
        values = [v.strip() for v in values_attr.split(";")]
        key_times_attr = elem.get("keyTimes")
        if key_times_attr is not None:
            key_times = [float(k.strip()) for k in key_times_attr.split(";")]
        elif len(values) > 1:
            key_times = [i / (len(values) - 1) for i in range(len(values))]
        else:
            key_times = [0.0]
        return values, key_times
    to_val = elem.get("to")
    if to_val is None:
        return None
    from_val = elem.get("from")
    if from_val is None:
        # <set> has no "from" at all; a bare <animate to="..."> with no
        # "from" has no defined base to interpolate from in this profile —
        # treated as a snap-to-value (matches <set>'s own semantics), not a
        # violation: MVP profile lists <set> as in-scope and this collapses
        # to the same "the value snaps at begin" behaviour.
        return [to_val], [0.0]
    return [from_val, to_val], [0.0, 1.0]


def _evaluate_element(
    anim: "ET.Element", t: float, violations: "list[SmilViolation]"
) -> "tuple[str, str] | None":
    """Returns (attribute_name, value_at_t) for one animate/set/
    animateTransform element, or None if it produced a violation (nothing
    to apply — the target attribute is left at its base value)."""
    tag = _local_tag(anim)
    attr_name = anim.get("attributeName")
    is_transform = tag == "animateTransform"

    if not is_transform:
        if attr_name is None or attr_name not in _ALLOWED_ATTRS:
            violations.append(
                SmilViolation(
                    "unknown_attribute",
                    f"attributeName={attr_name!r} is outside the supported allowlist",
                    tag,
                )
            )
            return None
    else:
        transform_kind = anim.get("type", "")
        if transform_kind not in _TRANSFORM_KINDS:
            violations.append(
                SmilViolation(
                    "unsupported_transform_type",
                    f"animateTransform type={transform_kind!r} is not supported",
                    tag,
                )
            )
            return None

    begin = _parse_offset_seconds(anim.get("begin"), default=0.0)
    if begin is None:
        violations.append(
            SmilViolation(
                "event_or_syncbase_begin",
                f"begin={anim.get('begin')!r} is not an offset — event/syncbase "
                "timing is out of this sampler's profile",
                tag,
            )
        )
        return None

    dur = _parse_offset_seconds(anim.get("dur"), default=None)  # type: ignore[arg-type]
    if dur is None and anim.get("dur") is not None:
        violations.append(
            SmilViolation("unparseable_dur", f"dur={anim.get('dur')!r}", tag)
        )
        return None

    calc_mode = anim.get("calcMode", "linear")
    if calc_mode not in ("linear", "discrete"):
        violations.append(
            SmilViolation(
                "unsupported_calc_mode",
                f"calcMode={calc_mode!r} degraded to 'linear' (spline/paced not "
                "evaluated)",
                tag,
            )
        )
        calc_mode = "linear"

    if anim.get("additive") not in (None, "replace") or anim.get("accumulate") not in (
        None,
        "none",
    ):
        violations.append(
            SmilViolation(
                "additive_or_accumulate",
                "additive/accumulate is not supported — replace/none assumed",
                tag,
            )
        )
        # Not fatal to the whole element: fall through and still evaluate
        # the base from/to/values timeline, just without the accumulation
        # the design note flags as visibly lossy either way.

    keyframes = _keyframes(anim)
    if keyframes is None:
        violations.append(
            SmilViolation(
                "missing_from_to_or_values",
                "neither a from/to pair nor a values list",
                tag,
            )
        )
        return None
    values, key_times = keyframes

    fill = anim.get("fill", "remove")
    if fill not in ("freeze", "remove"):
        fill = "remove"

    # SMIL's own default (absent repeatCount) is 1 — plays exactly once, not
    # indefinitely. Only the literal "indefinite" means unbounded.
    repeat_count_raw = anim.get("repeatCount")
    is_indefinite_repeat = repeat_count_raw == "indefinite"
    repeat_count: "float | None" = None if is_indefinite_repeat else 1.0
    if not is_indefinite_repeat and repeat_count_raw is not None:
        try:
            repeat_count = float(repeat_count_raw)
        except ValueError:
            violations.append(
                SmilViolation(
                    "unparseable_repeat_count", f"repeatCount={repeat_count_raw!r}", tag
                )
            )
            repeat_count = 1.0

    active_dur = dur if dur is not None else 0.0
    if active_dur <= 0.0:
        value = values[-1]
    else:
        elapsed = t - begin
        if elapsed < 0.0:
            value = values[0]
        else:
            total_span = (
                active_dur * repeat_count if repeat_count is not None else None
            )
            if not is_indefinite_repeat and total_span is not None and elapsed >= total_span:
                value = values[-1] if fill == "freeze" else values[0]
            else:
                cycle_pos = elapsed % active_dur
                frac = cycle_pos / active_dur
                value = _value_at_fraction(
                    values, key_times, frac, calc_mode, attr_name or "transform"
                )

    if is_transform:
        return "transform", _render_transform(anim.get("type", ""), value)
    assert attr_name is not None  # validated above for the non-transform branch
    return attr_name, value


def _value_at_fraction(
    values: "list[str]", key_times: "list[float]", frac: float, calc_mode: str, attr_name: str
) -> str:
    if len(values) == 1:
        return values[0]
    last = len(key_times) - 1
    for i in range(last):
        is_last_segment = i == last - 1
        # Each segment [key_times[i], key_times[i+1]) is half-open (SMIL's
        # own discrete-step semantic: hold values[i] up to, but not
        # including, the next keyframe, then step) — except the LAST
        # segment, which is closed on both ends so frac==1.0 still resolves.
        in_segment = (
            key_times[i] <= frac <= key_times[i + 1]
            if is_last_segment
            else key_times[i] <= frac < key_times[i + 1]
        )
        if in_segment:
            if calc_mode == "discrete":
                return values[i]
            span = key_times[i + 1] - key_times[i]
            local_frac = (frac - key_times[i]) / span if span > 0 else 0.0
            return _interpolate_value(values[i], values[i + 1], local_frac, attr_name)
    return values[-1]


def _render_transform(kind: str, value: str) -> str:
    return f"{kind}({value})"


def sample_svg_at(svg_text: str, t: float) -> "tuple[str, tuple[SmilViolation, ...]]":
    """Evaluate the supported SMIL subset (module docstring) in ``svg_text``
    at time ``t`` (seconds), returning a STATIC svg snapshot plus every
    profile violation encountered — never silently dropped, see module
    docstring.

    Every ``<animate>``/``<set>``/``<animateTransform>`` child element is
    removed from the returned snapshot regardless of whether it was
    evaluated: the snapshot is a frozen still frame, so no animation
    directive belongs in it (an evaluated one already applied its computed
    value onto the target element's own attribute)."""
    root = ET.fromstring(svg_text)
    violations: "list[SmilViolation]" = []

    for parent in root.iter():
        anim_children = [
            child for child in list(parent) if child.tag in _ALL_SMIL_ANIMATION_TAGS
        ]
        for anim in anim_children:
            tag = _local_tag(anim)
            if f"{{{_SVG_NS}}}{tag}" not in _ANIMATE_TAGS:
                violations.append(
                    SmilViolation(
                        "out_of_profile_element",
                        f"<{tag}> is not in the supported element set "
                        "(animate/set/animateTransform only)",
                        tag,
                    )
                )
                parent.remove(anim)
                continue
            result = _evaluate_element(anim, t, violations)
            if result is not None:
                attr_name, value = result
                parent.set(attr_name, value)
            parent.remove(anim)

    return ET.tostring(root, encoding="unicode"), tuple(violations)
