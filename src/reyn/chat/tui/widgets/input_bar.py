"""InputBar — bottom input area with inline slash-command picker.

Design decision:
  - `TextArea` for multi-line input. Enter submits; Ctrl+J inserts a newline.
  - SlashPicker (Discord/Slack-style) auto-shows when the input starts with
    "/" and is filtered live as the user types. Focus stays on the TextArea
    at all times — the picker is a passive renderer driven from here.

Keybindings (handled here):
  Enter         → If picker has matches: splice in the highlighted command
                  ("/cmdname") and submit in one keypress.
                  Otherwise: submit the message as typed.
  Ctrl+J        → Insert a newline.
  Ctrl+U        → Clear the whole input (single or multi-line).
  Tab           → Confirm-without-submit: insert "/cmdname " and keep
                  focus so the user can type args. No-op when picker
                  is closed.
  Up / Down     → Picker selection when picker visible; otherwise input
                  history when the cursor is on the first/last row.
  Escape        → Hide picker (keep the typed text).
  Ctrl+L        → Clear conversation pane (fires ClearConversation).
  Ctrl+D        → Quit (fires QuitRequested).
  Ctrl+C        → Cancel in-flight task (fires CancelInFlight).
"""
from __future__ import annotations

from collections import deque

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, TextArea

from reyn.chat.slash import SlashCommand

from .slash_picker import SlashPicker

# Wave-11 C#1 — bounded + persisted input history.
#
# Two caps with distinct purposes:
#   _HISTORY_MAX                  — in-memory deque cap. The current
#                                    session can recall this many recent
#                                    entries via Up/Down. Picked at 200
#                                    so heavy users have a deep recall
#                                    surface without unbounded growth.
#   _HISTORY_PERSIST_MAX          — how many entries cross the session
#                                    boundary via prefs.json. Smaller
#                                    so the JSON file stays compact
#                                    and the first boot read is fast.
#   _HISTORY_ENTRY_PERSIST_MAX_BYTES — per-entry size gate for
#                                       persistence only. Pasted blobs
#                                       past this size stay in-memory
#                                       (= current session can recall)
#                                       but don't pollute the
#                                       persisted slice.
_HISTORY_MAX = 200
_HISTORY_PERSIST_MAX = 50
_HISTORY_ENTRY_PERSIST_MAX_BYTES = 4096


class InputBar(Widget):
    """The bottom input row: SlashPicker + TextArea + footer hint."""

    BINDINGS = [
        # All priority bindings — they fire before the TextArea consumes the key.
        Binding("enter", "submit_or_confirm", "Submit", priority=True, show=False),
        Binding("tab", "confirm_picker", "Confirm", priority=True, show=False),
        Binding("up", "key_up", "Up", priority=True, show=False),
        Binding("down", "key_down", "Down", priority=True, show=False),
        Binding("escape", "dismiss_picker", "Dismiss", priority=True, show=False),
        Binding("ctrl+j", "newline", "Newline", priority=True, show=False),
        Binding("ctrl+u", "clear_input", "Clear input", priority=True, show=False),
        Binding("ctrl+l", "clear_conversation", "Clear", priority=True, show=False),
        Binding("ctrl+d", "quit_app", "Quit", priority=True, show=False),
        Binding("ctrl+c", "cancel", "Cancel", priority=True, show=False),
    ]

    DEFAULT_CSS = """
    InputBar {
        dock: bottom;
        height: auto;
        max-height: 22;
        background: #111111;
        border-top: solid #2a2a2a;
    }
    InputBar TextArea {
        background: transparent;
        border: none;
        color: #ffffff;
        padding: 0 1;
        height: auto;
        max-height: 10;
    }
    InputBar #hints {
        height: 1;
        color: #555555;
        padding: 0 2;
    }
    InputBar.in-flight TextArea {
        color: #666666;
    }
    InputBar.in-flight #hints {
        color: #886633;
    }
    InputBar.disconnected {
        border-top: solid #553333;
    }
    InputBar.disconnected TextArea {
        color: #555555;
    }
    InputBar.disconnected #hints {
        color: #553333;
    }
    """

    # ── messages ──────────────────────────────────────────────────────────────

    class UserSubmitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class ClearConversation(Message):
        pass

    class QuitRequested(Message):
        pass

    class CancelInFlight(Message):
        pass

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def __init__(
        self,
        *,
        slash_commands: list[SlashCommand] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._slash_commands: list[SlashCommand] = list(slash_commands or [])
        # Wave-11 C#1: bounded history + cross-session persistence.
        # The deque cap prevents long sessions from bloating memory
        # (a single 4 MB pasted prompt previously stayed pinned for
        # the whole session). on_mount restores the last
        # ``_HISTORY_PERSIST_MAX`` entries from
        # ``.reyn/tui_prefs.json``; ``_submit`` writes back after
        # each append, with oversized entries
        # (> ``_HISTORY_ENTRY_PERSIST_MAX_BYTES``) excluded from the
        # persisted slice but kept in-memory so the current
        # session can still recall them.
        self._history: deque[str] = deque(maxlen=_HISTORY_MAX)
        self._history_idx: int = -1
        # Wave-4 ML4: track whether the current TextArea contents came
        # from a history restore + haven't been edited yet. When True,
        # Up/Down navigate history directly (skipping the line-by-line
        # cursor walk) so multi-line restored entries don't require
        # ``row_count`` Up presses to advance one history step.
        # Cleared on ``TextArea.Changed`` (= user edit).
        self._restore_pristine: bool = False
        # Wave-9 D-F11: lock state for the in-flight turn. While True,
        # ``_submit`` returns early without posting ``UserSubmitted`` or
        # clearing the TextArea — so a fast Enter / Enter sequence
        # doesn't dispatch two LLM calls for the same prompt. App-side
        # callers toggle it via ``set_in_flight``: True at submit
        # time, False at stream_end / cancel / slash-return.
        self._in_flight: bool = False
        # Wave-13 T1-3: disconnected state for ``--connect`` WS mode.
        # When the WS connection drops, the session is unrecoverable
        # without a TUI restart. InputBar is locked permanently (= no
        # submit) and receives the ``.disconnected`` CSS class so the
        # border dims red and text goes grey. Toggle via
        # ``set_disconnected(True)``; never reset to False in the same
        # session (= the drop is permanent until restart).
        self._disconnected: bool = False

    def compose(self) -> ComposeResult:
        # Picker docked above TextArea (compose order matters: top-down).
        yield SlashPicker(id="slash-picker")
        yield TextArea(
            id="input",
            language=None,
            show_line_numbers=False,
            soft_wrap=True,
        )
        yield Label(self._build_hint(), id="hints")

    def on_mount(self) -> None:
        try:
            ta = self.query_one("#input", TextArea)
            ta.show_line_numbers = False
        except Exception:
            pass
        # Wave-11 C#1: hydrate history from prefs.
        self._load_persisted_history()

    def _load_persisted_history(self) -> None:
        """Restore the persisted history slice into the in-memory deque.

        Best-effort: a missing / malformed prefs file degrades to an
        empty history (= startup unaffected). Entries are appended in
        their stored order; oldest first so subsequent appends + Up
        navigation behave the same as a session that never restarted.
        """
        try:
            from reyn.chat.tui.prefs import load_tui_prefs
            root = self.app._project_root_path()  # type: ignore[attr-defined]
        except Exception:
            return
        try:
            prefs = load_tui_prefs(root)
        except Exception:
            return
        raw = prefs.get("input_history") if isinstance(prefs, dict) else None
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, str) and item:
                self._history.append(item)

    def _save_persisted_history(self) -> None:
        """Write the last ``_HISTORY_PERSIST_MAX`` entries to prefs.

        Filters out oversized entries (>
        ``_HISTORY_ENTRY_PERSIST_MAX_BYTES``) so a one-off 4 MB paste
        doesn't dominate the persisted slice. The in-memory deque
        still holds the oversized entry — only the persisted view
        excludes it. Best-effort: a write failure (= read-only FS)
        leaves the in-memory state intact.
        """
        try:
            from reyn.chat.tui.prefs import load_tui_prefs, save_tui_prefs
            root = self.app._project_root_path()  # type: ignore[attr-defined]
        except Exception:
            return
        if root is None:
            return
        # Build the persisted slice — last N entries that pass the
        # size gate. Walking from the right (newest) keeps recency
        # bias if the size gate drops mid-history entries.
        persisted: list[str] = []
        for item in reversed(self._history):
            if not isinstance(item, str):
                continue
            if len(item.encode("utf-8", errors="ignore")) > _HISTORY_ENTRY_PERSIST_MAX_BYTES:
                continue
            persisted.append(item)
            if len(persisted) >= _HISTORY_PERSIST_MAX:
                break
        persisted.reverse()  # restore chronological order (oldest first)
        try:
            prefs = load_tui_prefs(root) or {}
        except Exception:
            prefs = {}
        prefs["input_history"] = persisted
        try:
            save_tui_prefs(root, prefs)
        except Exception:
            pass

    # ── public API ────────────────────────────────────────────────────────────

    def update_slash_commands(self, commands: list[SlashCommand]) -> None:
        """Receive the full SlashCommand list from the registry."""
        self._slash_commands = [c for c in commands if not c.hidden]
        # If picker is currently visible, refresh it
        try:
            ta = self.query_one("#input", TextArea)
            self._update_picker(ta.text)
        except Exception:
            pass

    def focus_input(self) -> None:
        try:
            self.query_one("#input", TextArea).focus()
        except Exception:
            pass

    def set_in_flight(self, in_flight: bool) -> None:
        """Toggle the in-flight lock (D-F11, wave-9).

        While in-flight, ``_submit`` returns early so a second Enter
        keypress during an LLM response in flight doesn't dispatch a
        duplicate ``UserSubmitted`` message (= double LLM call, doubled
        cost, conv-pane noise). The ``.in-flight`` CSS class dims the
        TextArea text and the hint footer so the lock is visible.

        Idempotent — calling with the same value is a no-op. The lock
        is set by ``_submit`` immediately after a successful post, and
        cleared by the App at lifecycle points (stream_end, skill done
        with empty queue, action_cancel_inflight, slash-command return).
        """
        if self._in_flight == in_flight:
            return
        self._in_flight = in_flight
        if in_flight:
            self.add_class("in-flight")
        else:
            self.remove_class("in-flight")

    def set_disconnected(self, disconnected: bool) -> None:
        """Mark the InputBar as permanently disconnected (Wave-13 T1-3).

        Called when the WS connection drops in ``--connect`` mode.  The
        session cannot recover without a TUI restart, so:
          - ``_disconnected`` is set True (permanent guard in ``_submit``).
          - ``set_in_flight(True)`` is applied so the existing in-flight
            check also fires for any code path that reads ``_in_flight``.
          - ``.disconnected`` CSS class mounts, dimming the border and text
            as a visual cue that the input is permanently disabled.

        Idempotent — calling again with the same value is a no-op.
        """
        if self._disconnected == disconnected:
            return
        self._disconnected = disconnected
        if disconnected:
            self.add_class("disconnected")
            # Also hold the in-flight lock so all existing submit guards
            # fire — belt-and-suspenders, since _submit checks
            # _disconnected first.
            self.set_in_flight(True)
        else:
            self.remove_class("disconnected")

    def append_text(self, text: str) -> None:
        """Append text to the current input (used by voice dictation).

        Adds a single space separator if existing text doesn't already end
        with whitespace, so successive F2 sessions concatenate naturally.
        """
        if not text:
            return
        try:
            ta = self.query_one("#input", TextArea)
        except Exception:
            return
        existing = ta.text
        sep = "" if (not existing or existing[-1].isspace()) else " "
        new_text = existing + sep + text
        ta.load_text(new_text)
        # Move cursor to end so Enter sends or user can continue typing
        lines = new_text.split("\n")
        last_row = len(lines) - 1
        ta.move_cursor((last_row, len(lines[last_row])))

    # ── input events ─────────────────────────────────────────────────────────

    @on(TextArea.Changed, "#input")
    def on_textarea_changed(self, event: TextArea.Changed) -> None:
        self._update_picker(event.text_area.text)
        # ML4: any text edit clears the restore-pristine flag so the
        # next Up/Down resumes line-by-line cursor walk (= edit mode).
        # ``_load_history_entry`` re-sets the flag after this fires,
        # so restore → flag True flow is preserved.
        self._restore_pristine = False

    def on_paste(self, event: events.Paste) -> None:
        """Insert pasted text at the cursor, normalising line endings.

        Wave-4 ML1 + ML2 combined fix:

        - **ML1** (P1): without an explicit Paste handler, environments
          where the terminal does NOT emit bracket-paste escape codes
          (``\\x1b[200~…\\x1b[201~``) send the pasted text as raw
          keystrokes. The first ``\\n`` in the paste is consumed by
          the priority Enter binding (``action_submit_or_confirm``)
          and submits only the first line; the rest stays in the
          TextArea or is silently lost. Common breakage path on
          plain SSH / minimal tmux / SecureCRT.
        - **ML2** (P2): Windows-origin clipboards and many code
          snippets use ``\\r\\n`` line endings. The TextArea inserts
          them as literal ``\\r`` + ``\\n`` pairs, producing visible
          empty lines between every "real" line in the input.

        Both gone with one handler: intercept the Paste event,
        normalise ``\\r\\n`` / ``\\r`` → ``\\n``, and insert the text
        via ``TextArea.insert`` (= cursor-respecting). Stop the
        event so the default raw-keystroke path doesn't also fire.
        """
        text = event.text
        if not text:
            return
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")
        try:
            ta = self.query_one("#input", TextArea)
        except Exception:
            return
        ta.insert(normalised)
        # Prevent the default Paste handling from also delivering the
        # raw text as keystrokes (which would trigger the Enter binding
        # at the first newline — exactly the ML1 bug).
        event.stop()

    # ── action handlers (priority-bound keys) ────────────────────────────────

    def action_submit_or_confirm(self) -> None:
        """Enter — submit the message, expanding the picker selection in
        one keypress when the picker is open.

        Previously Enter only confirmed (inserted) the picker selection, so
        the user had to press Enter twice to actually send a slash command.
        Now: with the picker open we splice in the highlighted match and
        submit immediately. Tab still does insert-without-submit, so users
        who want to type args after the command name can use it.
        """
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            cmd = picker.selected_command()
            if cmd is not None:
                ta.load_text(f"/{cmd.name}")
                picker.hide()
                self._submit(ta)
                return
        self._submit(ta)

    def action_confirm_picker(self) -> None:
        """Tab — confirm picker selection. No-op when picker is closed."""
        picker = self._picker()
        ta = self._textarea()
        if picker is not None and picker.visible_ and picker.has_matches and ta is not None:
            self._confirm_picker(picker, ta)

    def action_key_up(self) -> None:
        """Up — picker selection if open, else input history at top edge.

        Wave-4 ML4: while ``_restore_pristine`` is True (= the current
        text came from a history restore and hasn't been edited), Up
        jumps to the previous history entry directly — skipping the
        line-by-line cursor walk that previously required N Up presses
        for an N-line restored entry to advance one history step. Any
        text edit clears the flag (see ``on_textarea_changed``), so
        once the user starts modifying the restored entry, Up resumes
        cursor-up navigation within the buffer.
        """
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            picker.move_selection(-1)
            return
        if self._restore_pristine:
            self._history_up(ta)
            return
        row, _ = ta.cursor_location
        if row == 0:
            self._history_up(ta)
            return
        # Multi-line: move cursor up within TextArea
        ta.action_cursor_up()

    def action_key_down(self) -> None:
        """Down — picker selection if open, else input history at bottom edge.

        Wave-4 ML4 mirror: while ``_restore_pristine`` is True, Down
        navigates history forward; otherwise it's cursor-down within
        the buffer.
        """
        picker = self._picker()
        ta = self._textarea()
        if ta is None:
            return
        if picker is not None and picker.visible_ and picker.has_matches:
            picker.move_selection(+1)
            return
        if self._restore_pristine:
            self._history_down(ta)
            return
        last_row = ta.text.count("\n")
        row, _ = ta.cursor_location
        if row >= last_row:
            self._history_down(ta)
            return
        ta.action_cursor_down()

    def action_dismiss_picker(self) -> None:
        """Escape — abandon slash entry: hide picker AND clear the prefix.

        The picker is only visible when the input is ``/<name-partial>``
        with no space or newline (see ``_update_picker``), so the entire
        text is the slash prefix being discovered. Leaving it behind made
        Esc feel like a no-op and forced the user to backspace before
        typing anything else — Slack/Discord clear the prefix on Esc.
        """
        self.dismiss_slash_prefix()

    def dismiss_slash_prefix(self) -> bool:
        """Hide the picker and clear the slash prefix it was tracking.

        Returns True if there was a slash-entry state to abandon (and it
        got cleared). The App's ``action_cancel_inflight`` calls this as
        an early branch on Ctrl+C so an open picker dismisses instead of
        producing a misleading "nothing in-flight" message.

        Trigger surface — clear in any of these states:
          * picker has matches and is ``visible_`` (= the user is still
            choosing a command — original Slack/Discord-style dismiss)
          * the buffer holds a typo-shaped slash prefix (= ``/<token>``
            with no space/newline) that produced **zero** matches and
            therefore left ``picker.visible_`` false. Without this case,
            ``/attch<Esc>`` was a silent no-op and the stale prefix
            concatenated with the user's next keystrokes into garbage
            like ``/attch/attach default``.
        """
        ta = self._textarea()
        text = ta.text if ta is not None else ""
        in_slash_prefix = (
            text.startswith("/") and " " not in text and "\n" not in text
        )
        picker = self._picker()
        picker_visible = picker is not None and picker.visible_
        if not picker_visible and not in_slash_prefix:
            return False
        if picker is not None:
            picker.hide()
        if ta is not None:
            ta.load_text("")
        return True

    def action_newline(self) -> None:
        ta = self._textarea()
        if ta is not None:
            ta.insert("\n")

    def action_clear_input(self) -> None:
        """Ctrl+U — wipe the whole input.

        The TextArea's default Ctrl+U only deletes from the cursor back
        to the start of the current line, which is unintuitive for a
        multi-line composer. Operators expect Ctrl+U to clear the entire
        buffer (matching readline-on-a-single-line semantics for the
        common case).
        """
        ta = self._textarea()
        if ta is not None:
            ta.clear()
        picker = self._picker()
        if picker is not None and picker.visible_:
            picker.hide()

    def action_clear_conversation(self) -> None:
        self.post_message(self.ClearConversation())

    def action_quit_app(self) -> None:
        self.post_message(self.QuitRequested())

    def action_cancel(self) -> None:
        self.post_message(self.CancelInFlight())

    # ── widget accessors ─────────────────────────────────────────────────────

    def _picker(self) -> SlashPicker | None:
        try:
            return self.query_one("#slash-picker", SlashPicker)
        except Exception:
            return None

    def _textarea(self) -> TextArea | None:
        try:
            return self.query_one("#input", TextArea)
        except Exception:
            return None

    # ── picker logic ─────────────────────────────────────────────────────────

    def _update_picker(self, text: str) -> None:
        try:
            picker = self.query_one("#slash-picker", SlashPicker)
        except Exception:
            return
        # Picker shows only when input starts with "/" and contains no newline
        # (multi-line input is not a command).
        if not text.startswith("/") or "\n" in text:
            picker.hide()
            return
        token = text[1:]
        # Once the user types a space, they've entered args mode. Look up
        # the command they picked and surface its summary as a single-row
        # hint — the picker stays out of the keyboard path so Enter
        # submits the typed args instead of replacing them with /cmdname.
        if " " in token:
            cmd_name, _, arg_partial = token.partition(" ")
            cmd = next(
                (c for c in self._slash_commands if c.name == cmd_name),
                None,
            )
            if cmd is None:
                picker.hide()
                return
            # If the command supplies a CompleterFn, run it and surface
            # filtered matches (e.g. /attach <name> → agent names). Falls
            # back to plain hint when there's no completer or no session.
            completions = (
                self._run_completer(cmd, arg_partial)
                if cmd.completer is not None
                else None
            )
            if completions:
                picker.set_completions(cmd, completions)
            else:
                picker.set_hint(cmd)
            return
        matches = [
            c for c in self._slash_commands
            if c.name.startswith(token)
        ]
        # Unknown-command in-input feedback: when the token is
        # non-empty (= the user is actively typing a command name)
        # but no command matches the prefix, surface a dim "did you
        # mean /<sug>?" row instead of silently hiding the picker.
        # Without this the user only learns the command is invalid
        # after pressing Enter, when the backend returns "unknown
        # command". ``suggest_for_unknown`` already gives us up to 3
        # fuzzy matches + /help as the escape hatch.
        if token and not matches:
            from reyn.chat.slash import suggest_for_unknown
            picker.set_unknown_hint(
                token,
                suggest_for_unknown(
                    token, names=[c.name for c in self._slash_commands],
                ),
            )
            return
        # Alphabetical so muscle memory built from /help (which lists
        # alphabetically) transfers to the picker. The previous
        # ``(len(name), name)`` ordering pushed common-but-longer
        # commands like /attach, /budget, /cancel below shorter ones
        # like /copy, /cost, /help on the empty-token open — a user
        # who stopped reading after the first few rows never saw the
        # high-value entries.
        matches.sort(key=lambda c: c.name)
        # Wave-6 SL1: on the bare "/" open (= no filter token), promote
        # /help to the top of the visible list. The picker shows only
        # the first ``_MAX_VISIBLE`` rows (= 8), so alphabetical order
        # alone pushed /help into the "+15 more — keep typing to
        # filter" overflow, invisible to users who don't know the
        # command name. Filtered opens (= token non-empty) keep the
        # alphabetical order so muscle memory transfers from /help.
        if not token:
            help_cmd = next(
                (c for c in matches if c.name == "help"), None,
            )
            if help_cmd is not None:
                matches = [help_cmd] + [c for c in matches if c is not help_cmd]
        picker.set_matches(matches)

    def _run_completer(
        self, cmd: SlashCommand, arg_partial: str,
    ) -> list[str]:
        """Resolve ``cmd.completer(session, arg_partial)`` and filter by partial.

        Reaches up to ``self.app`` to fetch the active session — a small
        layer leak that avoids threading a session-getter through every
        widget constructor for the single arg-completion path. Returns
        an empty list (= caller falls back to plain hint mode) on any
        exception so a broken completer can't break the picker.

        Filtering uses the LAST whitespace-delimited token of
        ``arg_partial`` rather than the whole string, so multi-arg
        commands like ``/plan discard ab`` filter the completions
        (= plan_ids) by ``"ab"`` and not by ``"discard ab"`` — the
        subcommand prefix is consumed in choosing the completer's
        context, not in the prefix match.
        """
        try:
            session = self.app._get_session()  # type: ignore[attr-defined]
        except Exception:
            return []
        if session is None or cmd.completer is None:
            return []
        try:
            all_completions = cmd.completer(session, arg_partial)
        except Exception:
            return []
        # Empty trailing space (= "discard " with nothing after) → no
        # filter, show everything. Otherwise filter by the last word.
        if not arg_partial or arg_partial.endswith(" "):
            return list(all_completions)
        last_word = arg_partial.rsplit(" ", 1)[-1]
        return [c for c in all_completions if c.startswith(last_word)]

    def _confirm_picker(self, picker: SlashPicker, ta: TextArea) -> None:
        cmd = picker.selected_command()
        if cmd is None:
            return
        new_text = f"/{cmd.name} "
        ta.load_text(new_text)
        ta.move_cursor((0, len(new_text)))
        picker.hide()

    def on_slash_picker_clicked(self, event: SlashPicker.Clicked) -> None:
        """Click on a picker row — insert the highlighted command.

        The picker's ``on_click`` already moved its own ``_selected`` to
        the clicked row before posting the message, so this just routes
        the existing confirm path. Mirrors the Tab key flow exactly:
        ``/<name> `` lands in the TextArea with the cursor past the
        trailing space, and the picker hides itself.
        """
        picker = self._picker()
        ta = self._textarea()
        if picker is not None and ta is not None:
            self._confirm_picker(picker, ta)
            # Re-focus the TextArea — the click may have implicitly
            # shifted focus context even though SlashPicker is
            # ``can_focus = False``, and the user expects to keep typing.
            ta.focus()

    # ── submit / history ─────────────────────────────────────────────────────

    def _submit(self, ta: TextArea) -> None:
        text = ta.text.strip()
        if not text:
            return
        # Wave-13 T1-3: WS disconnected — session is unrecoverable.
        # Swallow silently (the sticky already explains why). Checking
        # before ``_in_flight`` so the disconnected state is the
        # canonical early-exit path; ``_in_flight`` is also True when
        # disconnected (set by ``set_disconnected``), but the explicit
        # guard documents the intent clearly.
        if self._disconnected:
            return
        # Wave-9 D-F11: while a prior turn is still in flight, swallow
        # the Enter without posting / clearing. The typed text stays so
        # the user can edit and re-submit once the lock releases (e.g.
        # via stream_end). Without this guard, hitting Enter twice
        # quickly while an LLM call is in progress dispatches the same
        # prompt twice → doubled spend + duplicated reply in the conv
        # pane.
        if self._in_flight:
            return
        if not self._history or self._history[-1] != text:
            self._history.append(text)
            # Wave-11 C#1: persist after a fresh entry lands (= LRU
            # update would also benefit, but the simple "append-only"
            # contract is the common case; dedup of last-entry is
            # already handled above). Best-effort; failures are silent.
            self._save_persisted_history()
        self._history_idx = -1
        ta.clear()
        # Hide picker on submit (in case it was somehow visible)
        try:
            self.query_one("#slash-picker", SlashPicker).hide()
        except Exception:
            pass
        self.post_message(self.UserSubmitted(text))
        # Lock immediately — the App handler will unlock at the next
        # lifecycle boundary (stream end / cancel / slash return).
        # Setting after ``post_message`` is fine because Textual
        # processes message queues serially: a second Enter pressed
        # before this line lands would have already been routed to the
        # same handler and exited at ``if self._in_flight`` above.
        self.set_in_flight(True)

    def _history_up(self, ta: TextArea) -> None:
        if not self._history:
            return
        if self._history_idx == -1:
            self._history_idx = len(self._history) - 1
        elif self._history_idx > 0:
            self._history_idx -= 1
        self._load_history_entry(ta, self._history[self._history_idx])

    def _history_down(self, ta: TextArea) -> None:
        if self._history_idx < 0:
            return
        self._history_idx += 1
        if self._history_idx >= len(self._history):
            self._history_idx = -1
            ta.clear()
        else:
            self._load_history_entry(ta, self._history[self._history_idx])

    def _load_history_entry(self, ta: TextArea, text: str) -> None:
        ta.load_text(text)
        lines = text.split("\n")
        last_row = len(lines) - 1
        ta.move_cursor((last_row, len(lines[last_row])))
        # Wave-4 ML4: signal that the buffer now holds a verbatim
        # history entry. Subsequent Up/Down navigates history while
        # this flag is set; the first text edit (via
        # ``on_textarea_changed``) clears it. ``load_text`` above
        # fires ``TextArea.Changed`` which would normally clear the
        # flag — but on_textarea_changed runs SYNCHRONOUSLY before
        # we get here, so setting the flag at this point lands after
        # the changed-event clear. Tested via tmux smoke.
        self._restore_pristine = True

    # ── hint rendering ────────────────────────────────────────────────────────

    def _build_hint(self) -> str:
        # Fits in 80 cols (= ≤72 cells incl. 2-space left margin) so the
        # tail key doesn't get clipped on default terminals. Ctrl+O /
        # Ctrl+R / Ctrl+\ / Ctrl+P/N remain discoverable via the Keys
        # tab (Ctrl+B).
        #
        # Wave-2 K4: ``Ctrl+C cancel`` swapped in for ``Ctrl+D quit`` —
        # cancel is the highest-frequency interactive key during active
        # skill runs (= every time a user notices a long-running call
        # they want to abort) while Ctrl+D is the well-known
        # terminal-EOF convention users carry in from the shell. Both
        # remain in the Keys tab; the always-visible footer should
        # advertise the in-session-frequent key, not the universal one.
        #
        # Wave-9 D-F8: ``Ctrl+J nl`` surfaced next to ``Enter send`` so a
        # first-time user can see HOW to enter a multi-line prompt
        # without first opening the Keys tab. Previously the footer
        # advertised only ``Enter send`` and users hit Enter expecting
        # newline (= the natural assumption for code/instruction
        # blocks), submitting half-typed prompts. ``Ctrl+P/N turn`` is
        # dropped from the footer — turn navigation is a power-user
        # convenience, multi-line entry is a daily first-encounter
        # need.
        return (
            "  Enter send │ Ctrl+J nl │ Ctrl+C cancel │ "
            "Ctrl+L clear │ Ctrl+B panel"
        )
