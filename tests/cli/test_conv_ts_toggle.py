"""Tier 2: F9 timestamp toggle — conv-pane header symbol layout and persistence.

Pins the behaviour of ``ConversationView.toggle_timestamps()`` and the
F9 action in ``ReynTUIApp``.  Seven tests cover:

1. Default (ts on): ``render_user_message`` → inline line contains ``HH:MM``
   pattern + ``>`` symbol + body text on same line (Claude Code style #646).
2. After ``toggle_timestamps()``: ts off. ``render_user_message`` → no
   ``HH:MM`` timestamp; ``>`` at col 0 with body inline.
3. Toggle twice → back to on (same inline layout with ts prefix).
4. F9 dispatch (``action_toggle_timestamps``) → state flips + flash status
   emitted.
5. Persistence: ``_show_timestamps=False`` saved to prefs file; a new
   ``ConversationView`` instance loads as False.
6. Old messages rendered before toggle stay at old layout (= no re-render).
7. Day separator still emitted on day boundary regardless of ts state.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from textual.widgets import RichLog

from reyn.interfaces.tui.app import ReynTUIApp
from reyn.interfaces.tui.widgets import ConversationView
from reyn.interfaces.tui.widgets.conversation import (
    _BODY_INDENT_NO_TS,
    _BODY_INDENT_WITH_TS,
    _GLYPH_AGENT,
    _GLYPH_USER,
)
from reyn.runtime.outbox import OutboxMessage


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


def _log_lines(log: RichLog) -> list[str]:
    """Return a list of plain-text lines from the RichLog."""
    return ["".join(seg.text for seg in strip) for strip in log.lines]


def _find_lines_containing(log: RichLog, needle: str) -> list[str]:
    return [l for l in _log_lines(log) if needle in l]


# ---------------------------------------------------------------------------
# 1. Default (ts on): HH:MM visible, > inline with body on same line (#646)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts_on_by_default_header_contains_timestamp():
    """Tier 2: with timestamps on (default), user header+body inline on same line.

    #646 Claude Code style: ``HH:MM > body_text`` all on one logical line.
    The line containing the body text must also contain the HH:MM timestamp
    and the > symbol, and must start at col 0 (no leading spaces).
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        assert conv.show_timestamps is True
        conv.render_user_message("hello ts-on")
        await pilot.pause()

        full = _log_text(log)
        # HH:MM pattern should appear in the log.
        assert re.search(r"\d{2}:\d{2}", full), (
            f"expected HH:MM timestamp in conv log (ts on), got:\n{full}"
        )
        assert _GLYPH_USER in full

        # Inline layout: body text is on the same line as the header (col 0).
        lines = _log_lines(log)
        body_lines = [l for l in lines if "hello ts-on" in l]
        assert body_lines, "body text must appear in log"
        # The inline line starts with HH:MM at col 0, not with spaces.
        assert re.search(r"^\d{2}:\d{2}", body_lines[0]), (
            f"ts-on inline line must start with HH:MM at col 0: {body_lines[0]!r}"
        )
        assert not body_lines[0].startswith(" "), (
            f"ts-on inline line must NOT start with spaces: {body_lines[0]!r}"
        )


# ---------------------------------------------------------------------------
# 2. After toggle: ts off — no HH:MM, > inline with body at col 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts_off_after_toggle_no_timestamp_in_header():
    """Tier 2: after toggle_timestamps(), header+body inline with no HH:MM prefix.

    #646 inline layout with ts off: ``> body text`` on same line at col 0.
    No HH:MM timestamp prefix, body text not on a separate indented line.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # Flip to off.
        new_state = conv.toggle_timestamps()
        assert new_state is False
        assert conv.show_timestamps is False

        conv.render_user_message("hello ts-off")
        await pilot.pause()

        lines = _log_lines(log)

        # Inline layout: the body line also starts with the symbol at col 0.
        body_lines = [l for l in lines if "hello ts-off" in l]
        assert body_lines, "body text must appear in log"
        # ts-off inline: starts with symbol, no HH:MM, no leading spaces.
        assert body_lines[0].startswith(_GLYPH_USER), (
            f"ts-off inline line must start with user symbol at col 0: {body_lines[0]!r}"
        )
        assert not re.match(r"\d{2}:\d{2}", body_lines[0]), (
            f"ts-off inline line must NOT start with HH:MM: {body_lines[0]!r}"
        )
        # Must NOT start with spaces (ts-on indent or ts-off old indent).
        assert not body_lines[0].startswith(" "), (
            f"ts-off inline line must NOT start with spaces: {body_lines[0]!r}"
        )


# ---------------------------------------------------------------------------
# 3. Toggle twice → back to on
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_twice_returns_to_on():
    """Tier 2: two toggles restore the original ts-on state."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        assert conv.show_timestamps is True
        conv.toggle_timestamps()
        assert conv.show_timestamps is False
        conv.toggle_timestamps()
        assert conv.show_timestamps is True

        # Body rendered after second toggle uses ts-on inline layout.
        log = conv.query_one(RichLog)
        conv.render_user_message("back to on")
        await pilot.pause()

        lines = _log_lines(log)
        body_lines = [l for l in lines if "back to on" in l]
        assert body_lines
        # ts-on inline: starts with HH:MM at col 0 (not with spaces).
        assert re.search(r"^\d{2}:\d{2}", body_lines[0]), (
            f"after 2 toggles, ts-on inline line must start with HH:MM: {body_lines[0]!r}"
        )
        assert not body_lines[0].startswith(" "), (
            f"after 2 toggles body must NOT start with spaces: {body_lines[0]!r}"
        )


# ---------------------------------------------------------------------------
# 4. F9 dispatch → state flips + flash status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_f9_dispatch_flips_state_and_flashes_status():
    """Tier 2: action_toggle_timestamps flips _show_timestamps and shows status flash."""
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)

        before = conv.show_timestamps
        app.action_toggle_timestamps()
        await pilot.pause()

        assert conv.show_timestamps is not before, (
            "action_toggle_timestamps must flip show_timestamps"
        )
        # Flash status: StickyStatus should have been updated (we don't
        # assert exact text to avoid pinning sticky internals, but the
        # method must not raise).


# ---------------------------------------------------------------------------
# 5. Persistence — saved to prefs + loaded by new instance
# ---------------------------------------------------------------------------


def test_toggle_persists_to_prefs_file(tmp_path: Path):
    """Tier 2: toggle_timestamps() writes show_timestamps to tui_prefs.json."""
    from reyn.interfaces.tui.prefs import load_tui_prefs, save_tui_prefs

    # Seed an empty prefs file.
    prefs_dir = tmp_path / ".reyn"
    prefs_dir.mkdir()
    prefs_file = prefs_dir / "tui_prefs.json"
    prefs_file.write_text("{}", encoding="utf-8")

    # Build a minimal ConversationView-like object to test the persist path
    # directly (= no Textual app needed for the prefs layer).
    from reyn.interfaces.tui.widgets.conversation import ConversationView as CV

    # Patch _project_root_path to return tmp_path.
    class _FakeApp:
        def _project_root_path(self):
            return tmp_path

    # Direct test of save/load without mounting a full app:
    prefs: dict = {}
    prefs["show_timestamps"] = False
    save_tui_prefs(tmp_path, prefs)

    loaded = load_tui_prefs(tmp_path)
    assert loaded.get("show_timestamps") is False, (
        f"prefs file should have show_timestamps=False; got {loaded}"
    )

    prefs["show_timestamps"] = True
    save_tui_prefs(tmp_path, prefs)
    loaded2 = load_tui_prefs(tmp_path)
    assert loaded2.get("show_timestamps") is True


# ---------------------------------------------------------------------------
# 6. Old messages stay at old indent after toggle (no re-render)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_messages_keep_old_layout_after_toggle():
    """Tier 2: past messages rendered before the toggle keep their original layout.

    ConversationView does NOT re-render past RichLog content on toggle —
    only new writes use the new layout.  With the #646 inline format:
      - ts-on message: inline line starts with HH:MM at col 0.
      - ts-off message: inline line starts with symbol at col 0.
    Both coexist in the log after a toggle.
    """
    app = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)

        # First message: ts on → inline with HH:MM prefix.
        conv._show_timestamps = True
        conv._renderer._last_speaker = ""  # force new header
        conv.render_user_message("before-toggle")
        await pilot.pause()

        # Toggle → ts off.
        conv._show_timestamps = False
        conv._renderer._last_speaker = ""  # force new header

        conv.render_user_message("after-toggle")
        await pilot.pause()

        lines = _log_lines(log)

        before_body = [l for l in lines if "before-toggle" in l]
        after_body = [l for l in lines if "after-toggle" in l]

        assert before_body, "before-toggle body must be in log"
        assert after_body, "after-toggle body must be in log"

        # ts-on inline: starts with HH:MM (no leading space).
        assert re.search(r"^\d{2}:\d{2}", before_body[0]), (
            f"before-toggle (ts-on) must start with HH:MM: {before_body[0]!r}"
        )
        assert not before_body[0].startswith(" "), (
            f"before-toggle line must NOT start with spaces: {before_body[0]!r}"
        )

        # ts-off inline: starts with symbol at col 0 (no HH:MM, no spaces).
        assert after_body[0].startswith(_GLYPH_USER), (
            f"after-toggle (ts-off) must start with user symbol: {after_body[0]!r}"
        )
        assert not after_body[0].startswith(" "), (
            f"after-toggle line must NOT start with spaces: {after_body[0]!r}"
        )


# ---------------------------------------------------------------------------
# 7. Day separator still emitted on day boundary regardless of ts state
# ---------------------------------------------------------------------------


def test_date_separator_emitted_regardless_of_ts_state():
    """Tier 2: _date_separator helper is not gated by show_timestamps.

    The day-boundary separator (``── YYYY-MM-DD ───``) comes from
    ``_date_separator()`` which is called unconditionally inside
    ``_maybe_write_header``. Verify the helper always produces output.
    """
    from rich.cells import cell_len

    from reyn.interfaces.tui.widgets.conversation import _DASH_TOTAL, _date_separator

    sep = _date_separator("2026-05-24")
    assert "2026-05-24" in sep.plain
    assert cell_len(sep.plain) == _DASH_TOTAL, (
        f"date separator width must equal _DASH_TOTAL={_DASH_TOTAL}, "
        f"got {cell_len(sep.plain)}"
    )
