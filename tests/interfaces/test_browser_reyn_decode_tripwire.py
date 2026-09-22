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

#6230 stage 2 (PR #6238 review, architect + lead-coder): the browser's own
"never empty" degrade first landed as a JS twin of
:func:`reyn.interfaces.repl.renderer.legible_degrade_text` here — a hand
duplication this exact tripwire file would then have needed extending to
cover. Instead, the guarantee moved to the SOURCE of the wire text
(:func:`~reyn.interfaces.transport.agui.protocol._encode_display`, scoped to
CUSTOM-mapped kinds only — see ``test_encode_display_custom_kind_text``
below), so the browser has nothing of its own left to drift: it reads
``reyn.text`` (a plain passthrough) same as every other field this file
already tripwires. The last test in this file asserts the JS-side degrade
function is genuinely GONE, not merely unused, so a future re-add doesn't
silently reintroduce the duplication this stage removed.
"""
from __future__ import annotations

import re

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


def test_encode_display_fills_empty_text_only_for_a_custom_mapped_kind() -> None:
    """Tier 2: #6230 stage 2 item 3 (architect ruling, PR #6238 review) —
    ``_encode_display`` guarantees non-empty ``text`` ONLY for a
    CUSTOM-mapped kind (a reyn-private kind with no OTHER standard field a
    generic client could fall back to — presentation/trace/intervention/
    control sentinels). Real encoder call, real wire-reconstructed
    ``OutboxMessage`` (``from_wire`` — the actual untrusted-kind
    construction path), no mocks."""
    # "__totally_unknown__" is not in DISPLAY_KINDS -> CUSTOM-mapped.
    msg = OutboxMessage.from_wire(kind="__totally_unknown__", text="")
    ev = _encode_display(DisplayFrame(msg))
    assert ev.data["_reyn"]["text"], (
        "a CUSTOM-mapped kind's empty text must be filled at the wire source"
    )
    assert ev.data["value"]["text"] == ev.data["_reyn"]["text"], (
        "the standard CUSTOM envelope and the _reyn reconstruction block "
        "must carry the SAME (already-degraded) text — a generic client "
        "reading `value.text` and a reyn client reading `_reyn.text` must "
        "see identical content"
    )


def test_encode_display_never_fills_a_standard_kinds_legitimately_empty_text() -> None:
    """Tier 2: falsification pair for the test above — a STANDARD-mapped
    kind's (``agent``/``status``/``reasoning``/``error``) empty ``text``
    is a legitimate state (e.g. an LLM turn that produced no text content)
    and must reach the wire UNTOUCHED, never fabricated into a fake
    "unreadable frame" line. Without this pair, the previous test alone
    would not distinguish "the guarantee correctly targets CUSTOM only"
    from "it fills every empty text, including a real empty reply" — the
    exact overreach the architect's review caught.

    Strip-falsify (observed): widening ``_encode_display``'s own guard
    from ``ag_type is CUSTOM`` to an unconditional ``True`` makes this
    test fail cleanly — the real fabricated fallback line appears where
    ``""`` was expected (``AssertionError: ... == '(unrecognized frame:
    kind=\\'agent\\', no text)' == ''``), never a hang. Reverted, reran,
    green."""
    msg = OutboxMessage(kind="agent", text="")
    ev = _encode_display(DisplayFrame(msg))
    assert ev.data["_reyn"]["text"] == "", (
        "a standard-mapped kind's empty text must pass through unmodified"
    )
    assert ev.data["delta"] == "", (
        "the standard TEXT_MESSAGE_CONTENT envelope must also see the "
        "real, unmodified (possibly empty) text"
    )


def test_browser_no_longer_carries_its_own_degrade_logic() -> None:
    """Tier 2: #6230 stage 2's own drift-elimination witness — the JS
    ``_legibleDegradeText`` function this stage first added (a hand-ported
    twin of ``legible_degrade_text``, the thing THIS file's other tests
    exist to catch drift in) must be GONE, not merely unused: the
    guarantee it duplicated now lives once, server-side, in
    ``_encode_display`` (see the two tests above). If a future edit
    re-adds a JS-side degrade function, this goes RED as a prompt to
    re-read this stage's own reasoning before reintroducing the
    duplication it removed."""
    html = _INDEX_HTML.read_text(encoding="utf-8")
    assert "_legibleDegradeText" not in html, (
        "a JS degrade function reappeared in index.html — the whole point "
        "of moving the guarantee into _encode_display was to leave nothing "
        "in the browser that could drift out of wording-lockstep with the "
        "Python function again"
    )
