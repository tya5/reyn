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
"""
from __future__ import annotations

import re
from pathlib import Path

from reyn.interfaces.transport.agui.protocol import _encode_display
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_INDEX_HTML = (
    Path(__file__).resolve().parents[1]
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
