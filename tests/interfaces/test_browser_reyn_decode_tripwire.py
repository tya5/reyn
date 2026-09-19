"""Tier 2: the openui browser's _reyn decode stays in lockstep with the encoder.

The browser (``openui/static/index.html``) is a reyn-aware AG-UI client: it reads
the ``_reyn`` reconstruction block off each SSE event and rebuilds the
``{kind, text, meta}`` the design's ``agent.message`` channel consumes. That
decode is JavaScript, so a Python-side rename of an ``_encode_display`` ``_reyn``
field (``frame`` / ``kind`` / ``text`` / ``meta``) would update the Python decode
+ keep the Python tests green while the browser's ``reyn.<field>`` reads break
SILENTLY (no Python test exercises the JS).

This is the drift tripwire (ADR-0039 P6b): it regex-extracts the ``reyn.<field>``
names the browser's decode function reads and asserts each is a key the real
``_encode_display`` puts on its ``_reyn`` block. Rename a field on ONE side only
⇒ RED. Real encoder output; no mocks.

#6230 stage 2 extends this SAME tripwire (not a new mechanism — PR #6238
review, lead-coder) to the browser's ``_legibleDegradeText`` — a hand-ported
JS twin of :func:`reyn.interfaces.repl.renderer.legible_degrade_text`, with no
shared source across the language boundary. A comment asking a future editor
to "keep the two in sync" is exactly what this repo already rejected once for
the field-rename case above (a comment cannot be enforced); the fix is the
same shape as the rest of this file — regex-extract the JS literal wording
and assert it against the REAL Python function's own output, so a one-sided
edit to either side's wording goes RED instead of silently drifting.
"""
from __future__ import annotations

import re

from reyn.interfaces.repl.renderer import legible_degrade_text
from reyn.interfaces.transport.agui.protocol import _encode_display
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT

_INDEX_HTML = (
    REPO_ROOT
    / "src" / "reyn" / "interfaces" / "web" / "openui" / "static" / "index.html"
)


def _encoder_reyn_keys() -> set[str]:
    """The exact keys ``_encode_display`` writes into its ``_reyn`` block — the
    authoritative wire contract the browser decodes."""
    ev = _encode_display(DisplayFrame(OutboxMessage(kind="agent", text="x", meta={})))
    return set(ev.data["_reyn"].keys())


def _browser_decode_fn() -> str:
    """The body of the browser's ``_onReynDisplayEvent`` decode function."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"function _onReynDisplayEvent\(ev\) \{(.*?)\n  \}", html, re.DOTALL)
    assert m, "could not locate _onReynDisplayEvent in index.html (decode moved?)"
    return m.group(1)


def test_browser_reads_only_reyn_fields_the_encoder_emits() -> None:
    """Tier 2: every ``reyn.<field>`` the browser decode reads is a key
    ``_encode_display`` emits. A one-sided rename ⇒ RED (the silent-JS-drift the
    cross-language D6 check found manually, now committed)."""
    fn = _browser_decode_fn()
    # The decode binds ``const reyn = data._reyn`` then reads ``reyn.<field>``.
    read_fields = set(re.findall(r"\breyn\.(\w+)", fn))
    emitted = _encoder_reyn_keys()

    assert read_fields, "tripwire found no reyn.<field> reads — decode shape changed"
    missing = read_fields - emitted
    assert not missing, (
        "browser index.html reads _reyn fields the Python encoder no longer emits "
        f"(rename drift): {sorted(missing)}; encoder emits {sorted(emitted)}"
    )


def test_browser_reads_the_reyn_block_and_display_frame_tag() -> None:
    """Tier 2: the browser reads the ``_reyn`` block off the event and gates on
    ``frame === "display"`` — the two structural anchors of the decode. If either
    the block key or the display frame-tag value changes on the wire, this RED's."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert "data._reyn" in html, "browser must read the _reyn reconstruction block"

    ev = _encode_display(DisplayFrame(OutboxMessage(kind="agent", text="x", meta={})))
    assert ev.data["_reyn"]["frame"] == "display"  # the value the browser gates on
    fn = _browser_decode_fn()
    assert 'reyn.frame !== "display"' in fn or 'reyn.frame === "display"' in fn, (
        "browser must gate on the display frame-tag; if _encode_display's frame "
        "value changed from 'display', update index.html in lockstep"
    )


def _browser_degrade_fn() -> str:
    """The body of the browser's ``_legibleDegradeText`` — the JS port of
    :func:`~reyn.interfaces.repl.renderer.legible_degrade_text`."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    m = re.search(r"function _legibleDegradeText\(kind, text\) \{(.*?)\n  \}", html, re.DOTALL)
    assert m, "could not locate _legibleDegradeText in index.html (degrade moved?)"
    return m.group(1)


def test_browser_degrade_tier3_fixed_line_matches_python_byte_for_byte() -> None:
    """Tier 2: #6230 stage 2's own drift tripwire — the browser's tier-3
    (``kind`` AND ``text`` both empty) fixed line, regex-extracted from the
    JS source, must be BYTE-IDENTICAL to the REAL Python
    ``legible_degrade_text("", "")`` output. The two languages compute
    this string independently (no shared source); only a literal
    comparison against the real function call — not a re-typed expected
    string — proves neither side edited its own copy alone.

    Strip-falsify (observed): appending ", DRIFTED" to ONLY the JS tier-3
    literal makes this test (and the tier-order test below, since its own
    tier-3 regex no longer matches either) fail cleanly with the exact
    two strings shown in the assertion message — never a hang, since
    nothing here waits on anything. Reverted, reran, green."""
    fn = _browser_degrade_fn()
    # The LAST `return "...";` in the function body — `_browser_degrade_fn`
    # already stripped the function's own closing brace, so the fixed-line
    # return is simply the final statement (no trailing brace to anchor on).
    m = re.search(r'return "([^"]*)";\s*$', fn)
    assert m, "could not locate the tier-3 fixed-line return in _legibleDegradeText"
    js_fixed_line = m.group(1)

    python_output = legible_degrade_text("", "")
    assert js_fixed_line == python_output, (
        f"browser tier-3 fixed line {js_fixed_line!r} != Python's real "
        f"legible_degrade_text('', '') output {python_output!r} — wording drift"
    )


def test_browser_degrade_tier2_wording_matches_python_around_the_kind() -> None:
    """Tier 2: same drift check for tier 2 (``kind`` named, ``text``
    empty). The wording OUTSIDE the interpolated kind value must be
    byte-identical on both sides; the interpolation itself is NOT
    compared (Python's ``!r`` produces single-quoted repr,
    ``JSON.stringify`` produces double-quoted JSON — a genuine,
    unavoidable per-language convention difference, not wording this
    tripwire's job to unify)."""
    fn = _browser_degrade_fn()
    m = re.search(
        r'if \(kind\) return "([^"]*)" \+ JSON\.stringify\(kind\) \+ "([^"]*)";', fn,
    )
    assert m, "could not locate the tier-2 kind-naming line in _legibleDegradeText"
    js_prefix, js_suffix = m.group(1), m.group(2)

    python_output = legible_degrade_text("__some_kind__", "")
    assert python_output.startswith(js_prefix), (
        f"browser tier-2 prefix {js_prefix!r} does not match the start of "
        f"Python's real output {python_output!r} — wording drift"
    )
    assert python_output.endswith(js_suffix), (
        f"browser tier-2 suffix {js_suffix!r} does not match the end of "
        f"Python's real output {python_output!r} — wording drift"
    )
    assert "__some_kind__" in python_output, (
        "sanity: the kind itself must still appear somewhere in Python's "
        "own tier-2 output"
    )


def test_browser_degrade_has_the_same_three_tiers_in_the_same_order_as_python() -> None:
    """Tier 2: structural parity between the two ports — both try ``text``
    first, then ``kind``, then the fixed fallback, in that order. Catches
    a REORDERED or a DROPPED tier on the JS side even in the (impossible
    by construction, but not by this test alone) case where each tier's
    own wording still happened to match."""
    fn = _browser_degrade_fn()
    tier1 = re.search(r"if \(text\) return text;", fn)
    tier2 = re.search(r"if \(kind\) return", fn)
    tier3 = re.search(r'return "\(an unreadable frame arrived\)";', fn)
    assert tier1 and tier2 and tier3, (
        "browser _legibleDegradeText must keep all 3 tiers "
        "(text -> kind -> fixed fallback); one or more not found as expected"
    )
    assert tier1.start() < tier2.start() < tier3.start(), (
        "browser _legibleDegradeText's 3 tiers are out of order relative "
        "to the Python function they port (text -> kind -> fixed fallback)"
    )
