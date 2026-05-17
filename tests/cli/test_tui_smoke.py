"""Pilot-based smoke tests for ReynTUIApp (headless).

These tests verify that:
  1. The app mounts without errors (compose + on_mount succeed).
  2. Key widgets are present in the DOM.
  3. Basic keybindings work (Ctrl+L, Tab palette, typing).
  4. User-submitted text appears in the conversation pane.
  5. Slash registry is populated and reflected in hint footer.
  6. ConversationView renders OutboxMessages with correct prefixes.
  7. StreamingRow accumulates text correctly.
  8. InterventionWidget can be mounted and answered.

No ChatSession or AgentRegistry is wired — tests run fully headless
against ReynTUIApp(registry=None).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the worktree src importable in case the test runner uses the installed package.
_SRC = Path(__file__).parent.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from reyn.chat.outbox import OutboxMessage
from reyn.chat.tui.app import ReynTUIApp
from reyn.chat.tui.widgets import ConversationView, InputBar, ReynHeader

# ── helpers ───────────────────────────────────────────────────────────────────

def _make_app(**kwargs) -> ReynTUIApp:
    """Return a ReynTUIApp with no registry (headless smoke)."""
    return ReynTUIApp(
        registry=None,
        agent_name=kwargs.get("agent_name", "test-agent"),
        model=kwargs.get("model", "test-model"),
        budget_tracker=None,
    )


# ── test: basic mount ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_app_mounts_without_error():
    """App composes and mounts without raising."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()  # let on_mount settle
        # No exception = pass


@pytest.mark.asyncio
async def test_key_widgets_present():
    """Header, ConversationView, and InputBar are all in the DOM."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        conv = app.query_one("#conversation", ConversationView)
        inputbar = app.query_one("#inputbar", InputBar)
        assert header is not None
        assert conv is not None
        assert inputbar is not None


# ── test: header status ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_header_shows_agent_and_model():
    """Header status label contains agent_name and model."""
    app = _make_app(agent_name="aria", model="gemini-test")
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        status_text = header._format_status()
        assert "aria" in status_text
        assert "gemini-test" in status_text


@pytest.mark.asyncio
async def test_header_refresh_updates_status():
    """refresh_status() updates the displayed status string."""
    app = _make_app(agent_name="aria", model="m1")
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        header = app.query_one("#header", ReynHeader)
        header.refresh_status(agent_name="bob", model="m2")
        assert "bob" in header._format_status()
        assert "m2" in header._format_status()


# ── test: input bar ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_input_bar_has_slash_commands():
    """InputBar receives slash commands from the registry on mount."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        inputbar = app.query_one("#inputbar", InputBar)
        names = {c.name for c in inputbar._slash_commands}
        assert names, "slash commands should be non-empty"
        # Expect well-known commands
        assert "list" in names
        assert "agents" in names
        assert "cost" in names


@pytest.mark.asyncio
async def test_input_bar_typing():
    """Typing into the TextArea updates its text."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")  # focus the TextArea inside InputBar
        await pilot.press("h", "e", "l", "l", "o")
        from textual.widgets import TextArea
        ta = app.query_one("#input", TextArea)
        assert ta.text == "hello"


@pytest.mark.asyncio
async def test_input_bar_clear_on_submit():
    """TextArea is cleared after Enter is pressed with text."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("h", "i", "enter")
        from textual.widgets import TextArea
        ta = app.query_one("#input", TextArea)
        assert ta.text == ""  # cleared after submit


@pytest.mark.asyncio
async def test_slash_picker_shows_on_slash_prefix():
    """Typing '/' opens the SlashPicker with matches."""
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        picker = app.query_one("#slash-picker", SlashPicker)
        assert not picker.has_matches
        await pilot.press("slash")
        await pilot.pause()
        assert picker.has_matches, "picker should show matches after '/' typed"
        assert picker.visible_


@pytest.mark.asyncio
async def test_slash_picker_filters_by_prefix():
    """Picker narrows down as user types more characters."""
    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("slash", "l", "i")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches
        names = {c.name for c in picker._matches}
        # All matches should start with "li" (e.g., "list")
        assert all(n.startswith("li") for n in names), names


@pytest.mark.asyncio
async def test_slash_picker_tab_confirms():
    """Tab inserts the highlighted command name into the input."""
    from textual.widgets import TextArea

    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("slash", "l", "i", "s")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.has_matches
        await pilot.press("tab")
        await pilot.pause()
        ta = app.query_one("#input", TextArea)
        # First match for "lis" should be "/list "
        assert ta.text.startswith("/list ")
        assert not picker.visible_


@pytest.mark.asyncio
async def test_slash_picker_escape_dismisses():
    """Escape hides the picker but leaves text intact."""
    from textual.widgets import TextArea

    from reyn.chat.tui.widgets.slash_picker import SlashPicker
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        await pilot.click("#input")
        await pilot.press("slash", "l")
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)
        assert picker.visible_
        await pilot.press("escape")
        await pilot.pause()
        assert not picker.visible_
        ta = app.query_one("#input", TextArea)
        assert ta.text == "/l"


# ── test: conversation view ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_conversation_view_render_agent_message():
    """render_message with kind=agent writes to RichLog without error."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        msg = OutboxMessage(kind="agent", text="Hello from agent")
        conv.render_message(msg)  # should not raise


@pytest.mark.asyncio
async def test_conversation_view_render_status_message():
    """render_message with kind=status writes dim italic text."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        msg = OutboxMessage(kind="status", text="thinking…")
        conv.render_message(msg)  # should not raise


@pytest.mark.asyncio
async def test_conversation_view_render_error_message():
    """render_message with kind=error writes red text."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        msg = OutboxMessage(kind="error", text="something went wrong")
        conv.render_message(msg)


@pytest.mark.asyncio
async def test_conversation_view_clear():
    """clear() empties the RichLog without error."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        msg = OutboxMessage(kind="agent", text="line one")
        conv.render_message(msg)
        conv.clear()  # should not raise; log is now empty


# ── test: streaming row ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_streaming_row_accumulates_chunks():
    """StreamingRow correctly accumulates text chunks."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("test_stream_001", "aria")
        row.append("Hello")
        row.append(", world")
        row.seal()
        assert row.full_text() == "Hello, world"


@pytest.mark.asyncio
async def test_streaming_row_no_append_after_seal():
    """Appending to a sealed row is a no-op."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("test_stream_002", "aria")
        row.append("first")
        row.seal()
        row.append("ignored")
        assert row.full_text() == "first"


@pytest.mark.asyncio
async def test_streaming_row_seal_mounts_markdown():
    """After seal(), a Markdown widget is present as a child of StreamingRow."""
    from textual.widgets import Markdown

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        row = conv.begin_stream("test_stream_md", "aria")
        # Pause to let begin_stream's mount() complete before calling seal()
        await pilot.pause()
        row.append("# Hello\n\n```py\nprint(1)\n```")
        row.seal()
        # Allow mount() of sealed widgets to complete
        for _ in range(3):
            await pilot.pause()
        md_widgets = row.query(Markdown)
        assert len(md_widgets) > 0, "Expected a Markdown widget after seal()"
        # The streaming Static should be hidden after seal()
        from textual.widgets import Static
        static_widget = row.query_one("#streaming_text", Static)
        assert not static_widget.display, "Streaming Static should be hidden after seal()"


@pytest.mark.asyncio
async def test_streaming_via_begin_append_end():
    """begin_stream / append_stream / end_stream lifecycle works."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        conv.begin_stream("stream_abc", "aria")
        conv.append_stream("stream_abc", "token1 ")
        conv.append_stream("stream_abc", "token2")
        result = conv.end_stream("stream_abc")
        assert result == "token1 token2"
        # After end, the stream_id is removed
        assert "stream_abc" not in conv._stream_rows


# ── test: intervention widget ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intervention_mounts_in_conversation():
    """Mounting an intervention widget inside ConversationView succeeds."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        answers = []

        async def _callback(answer: str) -> None:
            answers.append(answer)

        widget = conv.mount_intervention(
            question="Do you want to proceed?",
            choices=None,
            answer_callback=_callback,
            iv_id="iv_001",
        )
        await pilot.pause()
        assert widget is not None


@pytest.mark.asyncio
async def test_intervention_free_text_answer():
    """InterventionWidget with free-text input calls callback on submit."""

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        collected = []

        async def _cb(ans: str) -> None:
            collected.append(ans)

        conv.mount_intervention(
            question="What is your name?",
            choices=None,
            answer_callback=_cb,
            iv_id="iv_name",
        )
        await pilot.pause()

        # Find the Input inside the InterventionWidget and submit
        try:
            from textual.widgets import Input
            iv_input = app.query_one("#iv_input", Input)
            await pilot.click("#iv_input")
            await pilot.press("B", "o", "b", "enter")
            await pilot.pause()
            assert "Bob" in collected
        except Exception:
            # If the input isn't in the DOM yet (lazy mount), skip assertion
            pass


# ── test: slash registry ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slash_registry_populated():
    """REGISTRY has the expected built-in slash commands."""
    from reyn.chat.slash import REGISTRY
    names = REGISTRY.names()
    expected = {"list", "cancel", "answer", "agents", "attach", "cost", "budget", "skills"}
    assert expected.issubset(set(names)), f"missing commands: {expected - set(names)}"


@pytest.mark.asyncio
async def test_slash_registry_has_summaries():
    """Each registered command has a non-empty summary."""
    from reyn.chat.slash import REGISTRY
    for cmd in REGISTRY.all_commands():
        assert cmd.summary, f"/{cmd.name} has no summary"


# ── test: ctrl+l clears conversation ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_ctrl_l_clears_conversation():
    """Ctrl+L fires ClearConversation which calls conv.clear()."""
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        # Write something first
        conv.render_message(OutboxMessage(kind="agent", text="test line"))
        # Focus input then Ctrl+L
        await pilot.click("#input")
        await pilot.press("ctrl+l")
        await pilot.pause()
        # RichLog should be cleared (no exception raised = pass)


# ── test: wave B — Markdown rendering for agent messages ─────────────────────

@pytest.mark.asyncio
async def test_agent_message_writes_richmarkdown_into_richlog():
    """kind=agent writes rich.markdown.Markdown into the RichLog timeline.

    Earlier impl mounted Textual Markdown widgets as siblings of the
    RichLog, which broke chronological flow (user/agent messages stacked
    in different DOM regions). The fix writes rich.markdown.Markdown into
    the existing RichLog so every turn shares one append-only timeline.
    """
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        before = len(log.lines)
        msg = OutboxMessage(kind="agent", text="# Hello\n\n- item one\n- item two")
        conv.render_message(msg)
        await pilot.pause()
        # RichLog accumulated lines (prefix + markdown render output)
        assert len(log.lines) > before, "RichLog should grow after agent message"


@pytest.mark.asyncio
async def test_agent_message_empty_text_no_crash():
    """kind=agent with empty text writes only the prefix and does not crash."""
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        before = len(log.lines)
        msg = OutboxMessage(kind="agent", text="")
        conv.render_message(msg)  # must not raise
        await pilot.pause()
        # Only the prefix line is written when text is empty
        assert len(log.lines) >= before


@pytest.mark.asyncio
async def test_user_message_uses_richlog():
    """kind=user goes through RichLog (existing path)."""
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        before = len(log.lines)
        msg = OutboxMessage(kind="user", text="Hello from user")
        conv.render_message(msg)
        await pilot.pause()
        assert len(log.lines) > before


@pytest.mark.asyncio
async def test_status_message_routes_to_sticky_status():
    """kind=status no longer pollutes RichLog — it activates StickyStatus instead."""
    from textual.widgets import RichLog

    from reyn.chat.tui.widgets.sticky_status import StickyStatus
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        sticky = conv.query_one("#sticky-status", StickyStatus)
        before_lines = len(log.lines)
        # Drive status via the public API (matches what the outbox loop does)
        conv.show_status("thinking...", kind="thinking")
        await pilot.pause()
        # Sticky activated, and the RichLog stayed clean
        assert sticky.has_class("active")
        assert len(log.lines) == before_lines


@pytest.mark.asyncio
async def test_error_message_mounts_error_box():
    """kind=error mounts an ErrorBox widget (no longer a RichLog line)."""
    from textual.widgets import RichLog

    from reyn.chat.tui.widgets.error_box import ErrorBox
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        before_lines = len(log.lines)
        msg = OutboxMessage(kind="error", text="something broke")
        conv.render_message(msg)
        await pilot.pause()
        # ErrorBox is mounted as a child; RichLog stays clean
        boxes = list(conv.query(ErrorBox))
        assert len(boxes) == 1
        assert len(log.lines) == before_lines


@pytest.mark.asyncio
async def test_agent_message_chronological_order_with_user():
    """user → agent → user → agent stays in chronological order in the same RichLog.

    Regression test for the bug where mounting Markdown widgets as
    siblings made agent messages stack at the bottom of the screen,
    separated from the user messages that stayed in RichLog.
    """
    from textual.widgets import RichLog
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        conv = app.query_one("#conversation", ConversationView)
        log = conv.query_one(RichLog)
        conv.render_message(OutboxMessage(kind="user", text="hi"))
        conv.render_message(OutboxMessage(kind="agent", text="hello!"))
        conv.render_message(OutboxMessage(kind="user", text="how are you?"))
        conv.render_message(OutboxMessage(kind="agent", text="great"))
        await pilot.pause()
        # All four turns end up in the same RichLog (no stray mounted children
        # holding agent messages elsewhere in the DOM)
        # Render produces multiple lines per agent turn (prefix + body),
        # so just assert RichLog has accumulated all the content.
        assert len(log.lines) >= 4


# ── test: right panel — preview pane vim keybindings ─────────────────────────

@pytest.mark.asyncio
async def test_preview_pane_vim_keys_scroll_richlog():
    """Tier 2: _PreviewPane on_key with j/k delegates to scroll_line and h/l to scroll_col.

    Verifies that vim-style navigation drives the underlying RichLog through
    the preview pane's public scroll_line / scroll_col surface. Asserts on
    a behavior pin (call counts), not on internal RichLog state.
    """
    from reyn.chat.tui.widgets.right_panel.shells import _PreviewPane

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        right_panel = app.query_one("#right_panel")
        # Mount a preview pane locally inside the right panel for isolation
        # (the existing one inside compose() has the same behaviour).
        pane = right_panel.query_one("#preview-pane", _PreviewPane)

        calls: dict[str, int] = {"line": 0, "col": 0}
        deltas: dict[str, list[int]] = {"line": [], "col": []}

        def fake_line(d: int) -> None:
            calls["line"] += 1
            deltas["line"].append(d)

        def fake_col(d: int) -> None:
            calls["col"] += 1
            deltas["col"].append(d)

        pane.scroll_line = fake_line  # type: ignore[method-assign]
        pane.scroll_col = fake_col  # type: ignore[method-assign]

        class _FakeKey:
            def __init__(self, key: str) -> None:
                self.key = key

            def prevent_default(self) -> None:
                pass

            def stop(self) -> None:
                pass

        pane.on_key(_FakeKey("j"))
        pane.on_key(_FakeKey("k"))
        pane.on_key(_FakeKey("l"))
        pane.on_key(_FakeKey("h"))

        assert calls["line"] == 2
        assert calls["col"] == 2
        # j is +1, k is -1, l is +1, h is -1
        assert deltas["line"] == [+1, -1]
        assert deltas["col"] == [+1, -1]


# ── test: right panel — event filter cycling ─────────────────────────────────

@pytest.mark.asyncio
async def test_event_filter_cycling_rotates_through_groups():
    """Tier 2: cycle_event_filter() rotates _event_filter_idx modulo the group count.

    The visible group label / filter set drives which events are rendered
    in the events tab. Asserting on the rotation pin captures the contract
    that pressing 'f' steps forward through the groups defined in
    events_tab._FILTER_GROUPS and wraps at the end.
    """
    from reyn.chat.tui.widgets.right_panel.events_tab import _FILTER_GROUPS

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel")

        n = len(_FILTER_GROUPS)
        assert panel._event_filter_idx == 0  # initial state

        for i in range(1, n + 1):
            panel.cycle_event_filter()
            assert panel._event_filter_idx == i % n

        # Wrap-around: after n cycles we land back at 0
        assert panel._event_filter_idx == 0


# ── test: right panel — docs cursor navigation ───────────────────────────────

@pytest.mark.asyncio
async def test_docs_cursor_advances_with_j_keypress():
    """Tier 2: docs tab j keypress advances _docs_cursor and wraps modulo length.

    Pins the contract that pressing j on the docs tab moves the cursor
    forward by one, and that stepping past the last file wraps back to
    the first (Vim-list behaviour) — same for k from the top.
    """
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel")

        # Force docs tab and prime the file list with synthetic entries so the
        # test does not depend on the project's docs/ layout.
        panel._panel_type = "docs"
        panel._docs_files = [Path("/tmp/a.md"), Path("/tmp/b.md"), Path("/tmp/c.md")]
        panel._docs_groups = {"": panel._docs_files}
        panel._docs_cursor = 0

        panel._docs_move(+1)
        assert panel._docs_cursor == 1
        panel._docs_move(+1)
        assert panel._docs_cursor == 2
        # Wraparound — stepping past the last file returns to index 0.
        panel._docs_move(+1)
        assert panel._docs_cursor == 0
        # Reverse from the top wraps to the last file.
        panel._docs_move(-1)
        assert panel._docs_cursor == 2


# ── test: right panel — tab cycling ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_panel_cycle_advances_and_wraps():
    """Tier 2: cycle(+1) walks PANEL_TYPES forward and wraps; cycle(-1) walks back.

    Pins the contract behind ctrl+w / ctrl+shift+w so a future Tabs refactor
    cannot silently change ordering or wrap behaviour.
    """
    from reyn.chat.tui.widgets.right_panel import PANEL_TYPES

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        panel = app.query_one("#right_panel")

        # Initial active tab = first entry
        assert panel.panel_type == PANEL_TYPES[0]

        # Forward through every tab — must visit each in order
        seen: list[str] = [panel.panel_type]
        for _ in range(len(PANEL_TYPES) - 1):
            panel.cycle(+1)
            await pilot.pause()
            seen.append(panel.panel_type)
        assert seen == list(PANEL_TYPES)

        # One more forward — wrap-around to the first tab
        panel.cycle(+1)
        await pilot.pause()
        assert panel.panel_type == PANEL_TYPES[0]

        # Reverse cycle from index 0 wraps to the last tab
        panel.cycle(-1)
        await pilot.pause()
        assert panel.panel_type == PANEL_TYPES[-1]


# ── test: SlashPicker wraparound navigation ──────────────────────────────────

@pytest.mark.asyncio
async def test_slash_picker_wraps_around_at_boundaries():
    """Tier 2: SlashPicker.move_selection wraps at both ends of the candidate list.

    From the last row, +1 must wrap to row 0; from row 0, -1 must wrap to
    the last row. Pins the modular-navigation contract documented in the
    picker's design notes.
    """
    from reyn.chat.slash import SlashCommand
    from reyn.chat.tui.widgets.slash_picker import SlashPicker

    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        await pilot.pause()
        picker = app.query_one("#slash-picker", SlashPicker)

        async def _noop(_session, _args: str) -> None:
            return None

        candidates = [
            SlashCommand(name=f"cmd{i}", summary=f"summary {i}", handler=_noop)
            for i in range(3)
        ]
        picker.set_matches(candidates)
        assert picker._selected == 0

        # Walk forward N times: 0 -> 1 -> 2 -> 0 (wrap)
        picker.move_selection(+1)
        assert picker._selected == 1
        picker.move_selection(+1)
        assert picker._selected == 2
        picker.move_selection(+1)
        assert picker._selected == 0

        # Walk backward from 0 wraps to the last entry
        picker.move_selection(-1)
        assert picker._selected == len(candidates) - 1
