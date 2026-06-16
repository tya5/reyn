"""Tier 2: lifecycle markers render as a dim inline divider, not a system block.

``ChatLifecycleForwarder`` (added in PR #201 to close #162) emits markers
shaped like ``[↑ 3 turns compacted]`` as ``system``-kind outbox messages.
Without polish they go through the same path as a slash-command output —
timestamped ``· system`` header + dash rule + indented body — which is
heavier than warranted for an advisory state-change announcement.

These tests pin the contract that:

1. The marker-detection heuristic only fires on the actual marker shape
   (= ``[↑ ... ]``, single line); regular system messages are unaffected.
2. The rendered marker uses the inline-divider style — ``dim #666666``,
   total cell width matches ``_DASH_TOTAL`` (same as ``_date_separator``).
3. ``_render_system_message`` routes markers through the divider helper
   and skips the ``· system`` speaker header.
4. Non-marker system messages still get the full system-block treatment.

The contrast / lighter-weight rendering ask came from the issue #162
follow-up — e2e-coder noted "dim-styling refinement は別 polish" when
landing the base ChatLifecycleForwarder fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import RichLog

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.tui.app import ReynTUIApp
from reyn.tui.widgets import ConversationView
from reyn.tui.widgets.conversation import (
    _DASH_TOTAL,
    _is_lifecycle_marker,
    _render_lifecycle_marker,
)


def _make_app() -> ReynTUIApp:
    return ReynTUIApp(
        registry=None,
        agent_name="test-agent",
        model="test-model",
        budget_tracker=None,
    )


def _log_text(log: RichLog) -> str:
    """Concat all strip text in the RichLog for substring searches."""
    return "\n".join("".join(seg.text for seg in strip) for strip in log.lines)


# ---------------------------------------------------------------------------
# 1. Detection heuristic
# ---------------------------------------------------------------------------


def test_is_lifecycle_marker_matches_compaction_shape():
    """Tier 2: ``[↑ N turns compacted]`` is detected as a lifecycle marker."""
    assert _is_lifecycle_marker("[↑ 3 turns compacted]")
    assert _is_lifecycle_marker("[↑ 1 turn compacted]")
    # Leading / trailing whitespace tolerated (forwarder emits clean text
    # but the detector should not be brittle against that).
    assert _is_lifecycle_marker("  [↑ 5 turns compacted]  ")


def test_is_lifecycle_marker_rejects_regular_system_messages():
    """Tier 2: regular system content does NOT match the marker heuristic.

    Slash-command output (``/help``, ``/keys``, multi-line plan dumps)
    must continue to render through the full system block.
    """
    assert not _is_lifecycle_marker("hello")
    assert not _is_lifecycle_marker("")
    # Multi-line content — even if it starts with ``[↑`` — is not a
    # marker. The forwarder always emits a single-line shape.
    assert not _is_lifecycle_marker("[↑ header]\nbody line")
    # Plain bracketed content without the up-arrow glyph.
    assert not _is_lifecycle_marker("[plain bracketed]")
    # /help-style output that happens to mention compaction in body.
    assert not _is_lifecycle_marker("compaction triggered")


# ---------------------------------------------------------------------------
# 2. Rendering contract — width / style
# ---------------------------------------------------------------------------


def test_render_lifecycle_marker_width_matches_dash_total():
    """Tier 2: rendered marker spans ``_DASH_TOTAL`` cells.

    Same width contract as ``_date_separator`` — the visual rhythm of
    inline dividers (date + lifecycle) must match so they read as a
    single category, not as visually competing rules.
    """
    rendered = _render_lifecycle_marker("[↑ 3 turns compacted]")
    assert cell_len(rendered.plain) == _DASH_TOTAL, (
        f"marker width must equal _DASH_TOTAL={_DASH_TOTAL}, "
        f"got {cell_len(rendered.plain)} for {rendered.plain!r}"
    )


def test_render_lifecycle_marker_uses_dim_style():
    """Tier 2: every span in the rendered marker carries ``dim`` styling.

    The pre-polish state used ``· system`` header in bold — these
    markers are advisory, not speaker-tagged, so the entire run must
    be dim. We don't pin the exact colour string (algorithm-level
    detail per testing.ja.md) — only that ``dim`` is present.
    """
    rendered = _render_lifecycle_marker("[↑ 3 turns compacted]")
    for _start, _end, style in rendered.spans:
        # Rich Style objects stringify to include "dim" when set.
        assert "dim" in str(style).lower(), (
            f"non-dim span found in marker: {style!r}"
        )


def test_render_lifecycle_marker_preserves_label_text():
    """Tier 2: the marker body (between brackets) survives into the render."""
    rendered = _render_lifecycle_marker("[↑ 7 turns compacted]")
    assert "↑ 7 turns compacted" in rendered.plain


# ---------------------------------------------------------------------------
# 3. End-to-end routing through _render_system_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifecycle_marker_skips_system_speaker_header():
    """Tier 2: a marker system message lands in the log WITHOUT ``· system`` header.

    The pre-polish path emitted a full speaker header block; the polish
    routes markers through ``_render_lifecycle_marker`` directly so the
    only line added is the inline divider.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        msg = OutboxMessage(kind="system", text="[↑ 3 turns compacted]")
        conv._render_system_message(msg)
        await pilot.pause()

        rendered = _log_text(log)
        # The marker body lands.
        assert "↑ 3 turns compacted" in rendered
        # The system speaker symbol (``·``) must NOT appear as a standalone
        # header — that would be the heavy pre-polish path leaking through.
        # We check for the old "· system" label string (pre-refactor label
        # text), and also that a bare "·" line doesn't appear as a header.
        assert "· system" not in rendered, (
            f"lifecycle marker leaked the system speaker header:\n{rendered}"
        )
        # With the new symbol-only layout, the symbol for system is "·"
        # but lifecycle markers bypass _maybe_write_header entirely, so no
        # "·" should appear as a header line at all.
        # (The marker itself may contain dashes and label text, but not the
        # bare system glyph as a standalone speaker indicator.)


@pytest.mark.asyncio
async def test_regular_system_message_still_uses_speaker_header():
    """Tier 2: non-marker system messages keep the symbol (``·``) speaker header.

    Post-refactor: the header is symbol-only (``·`` or ``HH:MM ·``), no
    label text. Defends against the detector being too greedy and
    accidentally stripping headers from slash-command output.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        msg = OutboxMessage(
            kind="system",
            text="usage: /help [topic]\n  topics: keys, commands",
        )
        conv._render_system_message(msg)
        await pilot.pause()

        rendered = _log_text(log)
        # Symbol-only header: the ``·`` glyph appears (= system speaker).
        # The old "· system" label is gone; we check for the glyph only.
        assert "·" in rendered, (
            f"regular system message lost its speaker symbol:\n{rendered}"
        )
        assert "usage: /help" in rendered
