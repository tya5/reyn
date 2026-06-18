"""ConversationView — scrollable conversation pane.

Composition (top → bottom):
  - RichLog (1fr) — the main append-only log of user/agent messages
  - Per-stream / inline widgets mounted as children: StreamingRow,
    SkillActivityRow, InterventionWidget — all height:auto so they stack
    naturally below the log as it streams.
  - StickyStatus (dock: bottom, h:1) — pins at the very bottom; replaces
    inline `⟳ thinking…` log lines.

Message-kind routing:
  agent       → header (timestamp + label, optionally suppressed when
                consecutive turns are within _GROUP_WINDOW_S) followed
                by full inline Markdown (Claude-Code-style, no collapse).
  status      → routed to StickyStatus (sticky 1-line, never logged).
  error       → inline Rich Text written into the conv RichLog (ephemeral,
                scroll-away). Severity color applied inline.
  intervention→ InterventionWidget (mount_intervention).
  trace       → suppressed in the conv pane; the App's outbox loop drives
                a SkillActivityRow instead. Right panel events tab still
                shows the full picture.
  skill_done  → suppressed; SkillActivityRow.finish() handles the visible
                completion line.

Empty state:
  The pane mounts a single dim hint ("Type / for commands · …") that auto-
  removes on the first user/agent message.

Turn navigation (B4):
  ConversationView records the RichLog line index at the start of every
  agent header. ReynTUIApp's Ctrl+P / Ctrl+N actions call jump_prev_turn /
  jump_next_turn to scroll the log to the previous/next turn anchor.

Cost suffix (A4):
  When the App enables cost-inline mode, _render_agent_cost_suffix() is
  called after a turn ends and writes a dim "⌁ Δ tokens │ $0.XXXX" line.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Literal

from rich.cells import cell_len
from rich.console import RenderableType
from rich.markdown import Markdown as RichMarkdown
from rich.padding import Padding
from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import RichLog, Static

# Wave-10 follow-up G-F15: module logger for the one-shot
# Textual-API-change warning in ``_richlog_start_line``.
logger = logging.getLogger(__name__)

from reyn.interfaces.tui._palette import (
    _AMBER,
    _CORAL,
    _EVENT_INTERVENTION,
    _HINT_ACTION,
    _RED_MUTED,
    _SEV_HIGH,
    _SEV_MED,
    _TEXT_DIM,
    _TEXT_MID,
    _TEXT_MUTED,
    _TEXT_NEUTRAL,
)
from reyn.runtime.outbox import OutboxMessage

from ._inline_row_manager import _InlineRowManager
from .async_stack_panel import AsyncStackPanel
from .inline_thinking_row import InlineThinkingRow
from .intervention import InterventionWidget
from .reasoning_block import ReasoningBlock
from .skill_activity import SkillActivityRow
from .sticky_status import StickyStatus
from .streaming_row import StreamingRow
from .tool_call_row import ToolCallRow

_DASH_TOTAL = 38  # matches the banner separator width
_GROUP_WINDOW_S = 60.0  # consecutive turns within this window share a header
# Speaker-identity symbols — 1-char shape cues for turn boundaries.
# These replace the old "▶ you" / "◆ reyn" labels; the symbol alone
# differentiates speaker without the redundant label text.
_GLYPH_USER = ">"
_GLYPH_AGENT = "⏺"
_GLYPH_SYSTEM = "·"
# Hanging indent constants — two modes gated by ``_show_timestamps``.
# ON  (ts shown):  ``HH:MM <sym>`` = 5 (ts) + 1 (space) + 1 (sym) + 1 (space)
#                  → body starts col 8.
# OFF (ts hidden): ``<sym>`` = 1 (sym) + 1 (space) → body starts col 2.
_BODY_INDENT_WITH_TS = 8
_BODY_INDENT_NO_TS = 2
# Legacy alias kept so external code that imports _BODY_INDENT_COLS
# directly (e.g. streaming_row.py, tests) still compiles. Points at the
# ts-on value (= the default).
_BODY_INDENT_COLS = _BODY_INDENT_WITH_TS
# Public alias for cross-module invariant tests that pin
# ``streaming_row.BODY_INDENT_COLS == conversation.BODY_INDENT_COLS``.
BODY_INDENT_COLS = _BODY_INDENT_COLS
_BODY_WIDTH_FALLBACK = 73  # estimated body width when size.width is 0 (= 80 - _BODY_INDENT_WITH_TS)
# /copy ring-buffer depth. Far enough back that the user can grab "the
# reply two turns ago" — the typical "wait, that one was useful" recovery
# pattern — without growing memory unboundedly across long sessions.
_RECENT_REPLIES_MAX = 10
# RichLog ring-buffer size. Bumped from the historical 5000 → 20000 to push
# the truncation boundary well past realistic session lengths (~400-800
# turns at typical reply sizes). Storage stays modest at average line width;
# turn anchors below are drop-aware so even pathological sessions that DO
# cross the boundary don't break Ctrl+P/N navigation.
_RICHLOG_MAX_LINES = 20_000
# F-H: minimum visible duration for an inline ToolCallRow before the conv
# pane flushes it into the RichLog scroll history + unmounts the live
# widget. Cache hits / instant returns would otherwise mount + flush
# within a single event-loop tick, leaving no perceptual cue. 0.3s
# matches typical perceptual threshold (= "I saw something happen").
_TOOL_CALL_MIN_DISPLAY_S = 0.3

def _pad_to_cells(s: str, target_cells: int) -> str:
    """Right-pad ``s`` with spaces so its terminal column width >= target.

    ``str.ljust`` counts code points, but terminal columns count display
    cells — a CJK character (or full-width punctuation, or an emoji)
    occupies 2 columns per glyph. Using ``ljust(4)`` on an agent name
    like ``"アリア"`` leaves it at 6 cells when the next column expects
    a fixed 4-cell offset, breaking the dash-rule alignment between
    user (``"you "``) and agent headers.

    Returns ``s`` unchanged when it already meets or exceeds the target;
    truncation would split a wide glyph and is left to the caller.
    """
    width = cell_len(s)
    if width >= target_cells:
        return s
    return s + " " * (target_cells - width)


def _indent_body(renderable: RenderableType, indent: int = _BODY_INDENT_WITH_TS) -> RenderableType:
    """Wrap ``renderable`` in a left-only Padding for the body indent column.

    Used at every body write site (agent markdown, user text, system text,
    fallback formatted lines). Header writes intentionally bypass this
    helper so the timestamp / symbol stay anchored at column 0.

    ``indent`` defaults to the ts-on value (= 8). Callers that track
    ``_show_timestamps`` pass the dynamic value via
    ``ConversationView._current_body_indent()``.
    """
    return Padding(renderable, (0, 0, 0, indent))


# Regex for leading Markdown sigil characters (headers, emphasis, code, blockquote).
_MD_LEADING_SIGILS = re.compile(r"^[#*`_~> ]+")
# Paired inline markers: **bold**, *em*, `code`.  Order matters — longest
# pattern first so ``**x**`` is handled before the single-``*`` pass.
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_EM = re.compile(r"\*([^*]+)\*")
_MD_CODE = re.compile(r"`([^`]+)`")


def _plain_first_line(raw: str) -> str:
    """Strip Markdown markup from the first line of ``raw`` for inline display.

    Goal: the ``⏺ HH:MM <first-line>`` inline header shows clean prose
    even when the reply opens with ``**Key finding:** …``, ``## Heading``,
    or `` `code` ``.

    Steps:
    1. Take only the first line.
    2. Remove paired inline emphasis / code markers so ``**x**`` → ``x``,
       ``*x*`` → ``x``, `` `x` `` → ``x``.  Done BEFORE leading-sigil
       strip so opening markers are still present for pair matching.
    3. Strip leading sigil chars (``#``, ``*``, `` ` ``, ``_``, ``~``,
       ``>``, space) that open Markdown headers / emphasis / blockquote.
    4. Strip surrounding whitespace.

    Non-markdown text like ``2 * 3 + 1`` is left unchanged — the inline
    patterns require matching pairs and at least one non-``*`` character
    between them, so they don't mangle bare arithmetic operators.
    """
    line = raw.splitlines()[0].strip() if raw else ""
    # Apply paired-marker removal first (while the opening markers are intact).
    line = _MD_BOLD.sub(r"\1", line)
    line = _MD_EM.sub(r"\1", line)
    line = _MD_CODE.sub(r"\1", line)
    # Strip any remaining leading sigil chars (headers, blockquote, stray markers).
    line = _MD_LEADING_SIGILS.sub("", line)
    return line.strip()


def _msg_header(symbol: str, name_style: str, show_ts: bool = True) -> Text:
    """Symbol-only header for a new message turn.

    Layout when ``show_ts=True``:
        ``HH:MM <symbol>``  (5 ts + 1 space + 1 symbol)
        body starts at col 8 (= _BODY_INDENT_WITH_TS)

    Layout when ``show_ts=False``:
        ``<symbol>``
        body starts at col 2 (= _BODY_INDENT_NO_TS)

    The dash rule and label text (``▶ you`` / ``◆ reyn``) are intentionally
    dropped — the 1-char symbol already differentiates speaker, and blank
    lines between turns provide turn separation without horizontal noise.
    """
    t = Text()
    if show_ts:
        t.append(time.strftime("%H:%M"), style="dim " + _TEXT_NEUTRAL)
        t.append(" ")
    t.append(symbol, style=name_style)
    return t


def _build_header_prefix(symbol: str, name_style: str, show_ts: bool = True) -> Text:
    """Build the ``HH:MM <symbol> `` (trailing space) inline prefix Text.

    Same glyph/timestamp logic as ``_msg_header`` but appends a trailing
    space so the body can be concatenated directly to produce the Claude
    Code-style ``HH:MM > body text`` inline layout.

    Used by ``_write_inline_header_body`` to combine the header and first
    body line into a single RichLog write, which puts the speaker symbol
    and message content on the same visual line with col-8 hanging indent
    for wrap continuations.
    """
    t = Text()
    if show_ts:
        t.append(time.strftime("%H:%M"), style="dim " + _TEXT_NEUTRAL)
        t.append(" ")
    t.append(symbol, style=name_style)
    t.append(" ")  # trailing space so body starts immediately after symbol
    return t


def _is_lifecycle_marker(text: str) -> bool:
    """Heuristic: ``ChatLifecycleForwarder`` marker text starts with ``[↑``
    and ends with ``]``, and is single-line.

    Lifecycle markers (= compaction, future attach-budget signals) are
    state-change announcements, not speech — rendering them as a full
    speaker-tagged system block (= timestamp + ``· system`` header + dash
    rule + indented body) gives them more visual weight than they
    deserve. The conv pane routes them through ``_render_lifecycle_marker``
    so they appear as a dim inline divider, matching the date-separator
    style.
    """
    t = text.strip()
    return t.startswith("[↑") and t.endswith("]") and "\n" not in t


def _render_lifecycle_marker(text: str) -> Text:
    """Dim ``── ↑ <body> ────…`` inline divider for a lifecycle marker.

    Shape mirrors ``_date_separator`` — same total cell width (``_DASH_TOTAL``)
    and same ``dim #666666`` styling so the visual rhythm stays consistent
    with day-boundary markers and other inline dividers.
    """
    stripped = text.strip().lstrip("[").rstrip("]").strip()
    label = f" {stripped} "
    t = Text()
    lead_dashes = 2
    label_cells = cell_len(label)
    trail_dashes = max(1, _DASH_TOTAL - lead_dashes - label_cells)
    t.append("─" * lead_dashes, style="dim " + _TEXT_NEUTRAL)
    t.append(label, style="dim " + _TEXT_NEUTRAL)
    t.append("─" * trail_dashes, style="dim " + _TEXT_NEUTRAL)
    return t


def _date_separator(date_str: str) -> Text:
    """Dim ``── YYYY-MM-DD ───…`` line for day boundaries between turns.

    Same total cell width as a regular turn header (_DASH_TOTAL) so the
    layout stays consistent. Rendered in the same dim grey as the
    timestamp column so the visual weight is below speaker headers but
    above body text.
    """
    t = Text()
    label = f" {date_str} "
    # Two leading dashes to balance the line visually; remaining dashes
    # extend to _DASH_TOTAL.
    lead_dashes = 2
    label_cells = cell_len(label)
    trail_dashes = max(1, _DASH_TOTAL - lead_dashes - label_cells)
    t.append("─" * lead_dashes, style="dim " + _TEXT_NEUTRAL)
    t.append(label, style="dim " + _TEXT_NEUTRAL)
    t.append("─" * trail_dashes, style="dim " + _TEXT_NEUTRAL)
    return t


def _meta_prefix(meta: dict) -> str:
    """Build [skill#abcd] prefix from meta, same logic as renderer.py."""
    skill = meta.get("skill_name")
    short = meta.get("run_id_short")
    if skill and short:
        return f"[{skill}#{short}] "
    if skill:
        return f"[{skill}] "
    if short:
        return f"[#{short}] "
    return ""


# W13 A#3 — soft severity classifier for the TUI seam (no engine changes).
# HIGH: terminal failures the operator must act on immediately.
# MED:  recoverable / transient — default for unclassified errors.
# LOW:  user-input mistakes that self-resolve on the next valid command.
_HIGH_TEXT_MARKERS: tuple[str, ...] = (
    "[budget exceeded]",
    "[auth error]",
    "[permission denied]",
)
_HIGH_META_SOURCE_SUFFIXES: tuple[str, ...] = ("_failed", "_aborted")
_LOW_TEXT_PREFIXES: tuple[str, ...] = ("usage:", "unknown command")


def _classify_error_severity(
    message: str,
    meta: dict,
) -> Literal["high", "med", "low"]:
    """Classify an error message into a 3-tier severity.

    HIGH — terminal failure (budget / auth / permission) or a meta source
    that ends with ``_failed`` / ``_aborted``.

    LOW — user input mistake: message starts with ``usage:`` or
    ``unknown command`` (case-insensitive).

    MED — everything else (recoverable / transient / unclassified default).

    TUI-internal helper only — no engine imports, no OS changes (P7).
    """
    lower = message.lower().lstrip()
    # Meta source suffix check (HIGH).
    source = str((meta or {}).get("source") or "")
    if source and any(source.endswith(s) for s in _HIGH_META_SOURCE_SUFFIXES):
        return "high"
    # Text-marker check (HIGH).
    msg_lower = message.lower()
    if any(marker in msg_lower for marker in _HIGH_TEXT_MARKERS):
        return "high"
    # User-input mistake (LOW).
    if any(lower.startswith(p) for p in _LOW_TEXT_PREFIXES):
        return "low"
    return "med"


class _StatusSpinnerController:
    """Manages StickyStatus + InlineThinkingRow lifecycle for ConversationView.

    Extracted from ConversationView (refactor tui-pr2). ConversationView
    instantiates one instance and delegates via thin wrappers, keeping the
    external API unchanged.
    """

    _THINKING_ROW_ID = "inline-thinking-row"

    def __init__(self, parent: "ConversationView") -> None:
        self._parent = parent

    def show_status(self, text: str, kind: str = "general", *, terminal: bool = False) -> None:
        s = self._parent._sticky()
        if s is not None:
            s.show(text, kind=kind, terminal=terminal)

    def update_status(self, text: str) -> None:
        s = self._parent._sticky()
        if s is not None:
            s.update_text(text)

    def hide_status(self) -> None:
        s = self._parent._sticky()
        if s is not None:
            s.hide()

    def start_thinking(self) -> None:
        """Idempotent: second call is a no-op when the row is already mounted."""
        try:
            self._parent.query_one(f"#{self._THINKING_ROW_ID}", InlineThinkingRow)
        except Exception:
            row = InlineThinkingRow(
                id=self._THINKING_ROW_ID,
                indent=self._parent._current_body_indent(),
            )
            self._parent.mount(row)

    def stop_thinking(self) -> None:
        """Idempotent: calling without a prior start_thinking is a no-op."""
        try:
            row = self._parent.query_one(f"#{self._THINKING_ROW_ID}", InlineThinkingRow)
            row.remove()
        except Exception:
            pass


class _ScrollController:
    """Manages scroll-lock, turn navigation, and the '↓ N new' indicator.

    Extracted from ConversationView (refactor tui-pr3). Owns all scroll/
    viewport state and the methods that operate on it. ConversationView
    retains public methods as thin delegates and Textual reactive handlers
    as 1-line forwarders.

    Dependencies injected via parent reference:
      parent._log()        — the RichLog widget
      parent._write_log()  — permanent log line write
      parent._sticky()     — StickyStatus widget
      parent.show_status() — route to StickyStatus
      parent.query_one()   — Textual widget query
    """

    def __init__(self, parent: "ConversationView") -> None:
        self._parent = parent
        # Scroll-lock state
        self._user_scrolled: bool = False
        self._new_below_baseline: int = -1
        self._new_below_count: int = 0
        # Turn navigation anchors (absolute, drop-aware)
        self._turn_anchors: list[int] = []
        # One-shot warning flags
        self._trim_warned: bool = False
        self._start_line_warned: bool = False
        # Flash dedup for turn-position + boundary cues
        self._last_turn_flash: tuple[int, int] | None = None
        self._last_boundary_flash: str | None = None

    # ── read-only state accessors ────────────────────────────────────────────

    @property
    def user_scrolled(self) -> bool:
        return self._user_scrolled

    @property
    def new_below_count(self) -> int:
        return self._new_below_count

    @property
    def trim_warned(self) -> bool:
        return self._trim_warned

    def turn_anchors_snapshot(self) -> tuple[int, ...]:
        return tuple(self._turn_anchors)

    def richlog_start_line(self, log: "RichLog") -> int:
        return self._richlog_start_line(log)

    def absolute_line_position(self, log: "RichLog") -> int:
        return self._absolute_line_position(log)

    # ── public scroll methods ────────────────────────────────────────────────

    def snap_to_bottom(self) -> None:
        """Force the log to the tail and re-arm auto-scroll.

        Called from 're-engaging' entry points (render_user_message, clear,
        scroll_to_bottom) so prior scroll-up state doesn't pin the user.
        """
        try:
            log = self._parent._log()
        except Exception:
            return
        try:
            log.auto_scroll = True
            log.scroll_end(animate=False)
        except Exception:
            pass
        self._user_scrolled = False
        self._new_below_baseline = -1
        self._update_new_below(0)

    def jump_prev_turn(self) -> None:
        self._jump_to_relative_anchor(-1)

    def jump_next_turn(self) -> None:
        self._jump_to_relative_anchor(+1)

    def scroll_page_up(self) -> None:
        """Scroll up one page; sets user-scrolled so auto-scroll doesn't snap back."""
        log = self._parent._log()
        try:
            log.scroll_page_up(animate=False)
        except Exception:
            try:
                log.scroll_relative(y=-log.size.height, animate=False)
            except Exception:
                pass
        self._user_scrolled = True

    def scroll_to_top(self) -> None:
        """Jump to the oldest content; sets user-scrolled."""
        log = self._parent._log()
        try:
            log.scroll_home(animate=False)
        except Exception:
            try:
                log.scroll_to(y=0, animate=False)
            except Exception:
                pass
        self._user_scrolled = True

    def scroll_page_down(self) -> None:
        """Scroll down one page; re-arms auto-scroll only if we land at the tail."""
        log = self._parent._log()
        try:
            log.scroll_page_down(animate=False)
        except Exception:
            try:
                log.scroll_relative(y=log.size.height, animate=False)
            except Exception:
                pass
        try:
            if log.scroll_y >= log.max_scroll_y - 1:
                self._user_scrolled = False
            else:
                self._user_scrolled = True
        except Exception:
            pass

    def scroll_line_up(self) -> None:
        """Scroll up one line; sets user-scrolled."""
        log = self._parent._log()
        try:
            log.scroll_relative(y=-1, animate=False)
        except Exception:
            pass
        self._user_scrolled = True

    def scroll_line_down(self) -> None:
        """Scroll down one line; re-arms auto-scroll only at the tail."""
        log = self._parent._log()
        try:
            log.scroll_relative(y=1, animate=False)
        except Exception:
            pass
        try:
            if log.scroll_y >= log.max_scroll_y - 1:
                self._user_scrolled = False
            else:
                self._user_scrolled = True
        except Exception:
            pass

    def scroll_to_bottom(self) -> None:
        self.snap_to_bottom()

    # ── bridge methods (called from non-scroll ConversationView methods) ─────

    def record_turn_anchor(self, log: "RichLog") -> None:
        """Append the current absolute write-position as a turn anchor."""
        self._turn_anchors.append(self._absolute_line_position(log))

    def maybe_warn_trim(self, log: "RichLog") -> None:
        """Emit one-shot trim warning when the ring buffer has dropped lines."""
        if not self._trim_warned and self._richlog_start_line(log) > 0:
            self._maybe_warn_about_trimmed_history(log)

    def reset_for_clear(self) -> None:
        """Reset all scroll/nav state for Ctrl+L; caller handles log.auto_scroll."""
        self._last_turn_flash = None
        self._last_boundary_flash = None
        self._turn_anchors.clear()
        self._trim_warned = False
        self._user_scrolled = False
        self._new_below_baseline = -1
        self._update_new_below(0)

    # ── reactive handler bodies (called from ConversationView watchers) ──────

    def on_log_scroll_y(self, old: float, new: float) -> None:
        """Flip auto_scroll and _user_scrolled based on at-bottom check."""
        try:
            log = self._parent._log()
        except Exception:
            return
        at_bottom = new >= log.max_scroll_y - 1
        if at_bottom:
            if not log.auto_scroll:
                log.auto_scroll = True
            if self._user_scrolled:
                self._user_scrolled = False
            self._new_below_baseline = -1
            self._update_new_below(0)
        else:
            if log.auto_scroll:
                log.auto_scroll = False
            if not self._user_scrolled:
                self._user_scrolled = True
            if self._new_below_baseline < 0:
                self._new_below_baseline = int(log.max_scroll_y)
                self._update_new_below(0)

    def on_log_content_grew(self, old: object, new: object) -> None:
        """RichLog virtual_size changed — refresh the '↓ N new' count."""
        if self._new_below_baseline < 0:
            return
        try:
            log = self._parent._log()
        except Exception:
            return
        delta = int(log.max_scroll_y) - self._new_below_baseline
        self._update_new_below(max(0, delta))

    # ── private helpers ──────────────────────────────────────────────────────

    def _update_new_below(self, count: int) -> None:
        self._new_below_count = max(0, count)
        try:
            strip = self._parent.query_one("#new-below", Static)
        except Exception:
            return
        if count <= 0:
            if "hidden" not in strip.classes:
                strip.add_class("hidden")
            return
        noun = "new line" if count == 1 else "new lines"
        strip.update(f"↓ {count} {noun} below · Alt+End to jump")
        if "hidden" in strip.classes:
            strip.remove_class("hidden")

    def _richlog_start_line(self, log: "RichLog") -> int:
        """Return log._start_line defensively (G-F15 one-shot warning on API change)."""
        value = getattr(log, "_start_line", None)
        if value is None:
            if not self._start_line_warned:
                self._start_line_warned = True
                logger.warning(
                    "RichLog._start_line is missing — Textual API may "
                    "have changed; turn-navigation anchors will degrade "
                    "to treating all history as never-trimmed. Update "
                    "the helper at conversation._richlog_start_line.",
                )
            return 0
        return value

    def _absolute_line_position(self, log: "RichLog") -> int:
        return self._richlog_start_line(log) + len(log.lines)

    def _resolve_anchors_to_current_view(self, log: "RichLog") -> list[int]:
        start = self._richlog_start_line(log)
        return [a - start for a in self._turn_anchors if a - start >= 0]

    def _maybe_warn_about_trimmed_history(self, log: "RichLog") -> None:
        if self._trim_warned:
            return
        start = self._richlog_start_line(log)
        if start <= 0:
            return
        self._trim_warned = True
        warning_text = f"↑ earlier history trimmed ({start:,} lines)"
        try:
            self._parent._write_log(Text(f"  {warning_text}", style="dim italic " + _TEXT_MUTED))
        except Exception:
            pass
        sticky = self._parent._sticky()
        if sticky is not None:
            try:
                sticky.show(warning_text, kind="general")
            except Exception:
                pass

    def _jump_to_relative_anchor(self, delta: int) -> None:
        if not self._turn_anchors:
            return
        log = self._parent._log()
        anchors = self._resolve_anchors_to_current_view(log)
        if not anchors:
            self._maybe_warn_about_trimmed_history(log)
            return
        if len(anchors) < len(self._turn_anchors):
            self._maybe_warn_about_trimmed_history(log)
        cur_y = log.scroll_y
        if delta < 0:
            target = None
            for a in reversed(anchors):
                if a < cur_y - 1:
                    target = a
                    break
            hit_boundary = target is None
            if target is None:
                target = anchors[0]
        else:
            target = None
            for a in anchors:
                if a > cur_y + 1:
                    target = a
                    break
            hit_boundary = target is None
            if target is None:
                target = anchors[-1]
        if hit_boundary and abs(cur_y - target) <= 1:
            self._flash_boundary_hint("start" if delta < 0 else "end")
            return
        try:
            log.scroll_to(y=target, animate=False)
        except Exception:
            pass
        self._user_scrolled = True
        try:
            idx = anchors.index(target)
        except ValueError:
            return
        self._flash_turn_position(idx + 1, len(anchors), delta=delta)

    def _flash_boundary_hint(self, direction: str) -> None:
        if direction == "start":
            text = "↑ beginning of history"
        else:
            text = "↓ end of history"
        if self._last_boundary_flash == direction:
            return
        self._last_boundary_flash = direction
        self._last_turn_flash = None
        self._parent.show_status(text, kind="general")

    def _flash_turn_position(self, n: int, total: int, delta: int = -1) -> None:
        if self._last_turn_flash == (n, total):
            return
        self._last_turn_flash = (n, total)
        self._last_boundary_flash = None
        arrow = "↑" if delta < 0 else "↓"
        self._parent.show_status(f"{arrow} turn {n} / {total}", kind="general")


class _MessageRenderer:
    """Owns all message-rendering state and logic (tui-pr5).

    State owned:
      parent._renderer._last_speaker          — current-turn speaker id
      parent._renderer._last_speaker_at       — wall-clock time of last header
      parent._renderer._last_header_date      — YYYY-MM-DD of last header date
      parent._renderer._recent_replies        — /copy ring buffer (newest last)
      parent._renderer._has_first_message     — empty-hint latch

    Public surface used by ConversationView delegates:
      render_message / render_user_message
      last_reply_text / reply_at / recent_reply_count
      find_in_buffer / dump_buffer_text
      write_error / render_cost_suffix

    Bridge methods for streaming (pr4):
      set_speaker(speaker, at)  — anchor grouping at stream-start
      record_reply(text)        — append to /copy ring buffer with cap
    """

    def __init__(self, parent: "ConversationView") -> None:
        self._parent = parent
        self._last_speaker: str = ""
        self._last_speaker_at: float = 0.0
        self._last_header_date: str = ""
        self._recent_replies: list[str] = []
        self._has_first_message: bool = False

    # ── read-only accessors (public) ──────────────────────────────────────────

    @property
    def last_speaker_at(self) -> float:
        """Wall-clock timestamp of the last header write (for timestamp tests).

        Tests and streaming callers write ``conv._last_speaker_at`` directly
        for setup rather than accessing ``_last_speaker_at`` directly.
        """
        return self._last_speaker_at

    @property
    def last_header_date(self) -> str:
        """YYYY-MM-DD string of the most recently written date separator.

        Used by toggle_timestamps tests rather than accessing
        ``_last_header_date`` directly.
        """
        return self._last_header_date

    # ── bridge methods ────────────────────────────────────────────────────────

    def reset_for_clear(self) -> None:
        """Reset all renderer state on Ctrl+L (mirrors _scroll_ctrl.reset_for_clear)."""
        self._last_speaker = ""
        self._last_speaker_at = 0.0
        # Preserve today's date so same-day Ctrl+L doesn't re-emit date separator.
        self._last_header_date = time.strftime("%Y-%m-%d")
        self._recent_replies.clear()
        self._has_first_message = False

    def set_speaker(self, speaker: str, at: float) -> None:
        """Anchor grouping state at stream-start (pr4 / _StreamController bridge)."""
        self._last_speaker = speaker
        self._last_speaker_at = at

    def record_reply(self, text: str) -> None:
        """Append to /copy ring buffer with cap (pr4 / _StreamController bridge)."""
        self._recent_replies.append(text)
        if len(self._recent_replies) > _RECENT_REPLIES_MAX:
            self._recent_replies = self._recent_replies[-_RECENT_REPLIES_MAX:]

    # ── write primitives ──────────────────────────────────────────────────────

    def _write_log(self, text: "Text") -> None:
        log = self._parent._log()
        log.write(text)
        # Surface the trim warning the first time it's earned, even when
        # the user hasn't pressed Ctrl+P/N yet. The previous wiring only
        # called this from ``_jump_to_relative_anchor`` — turn navigation
        # — so a user who let the session auto-scroll past the
        # ``_RICHLOG_MAX_LINES`` boundary never saw the "earlier history
        # trimmed" signal until they happened to hit Ctrl+P. The
        # ``_trim_warned`` flag keeps it strictly one-shot per session.
        self._parent._scroll_ctrl.maybe_warn_trim(log)

    def _write_body(self, renderable: "RenderableType") -> None:
        """Append a body renderable at the dynamic hanging-indent column.

        Wraps ``renderable`` in left-only ``Padding`` so wrap continuations
        line up under the speaker symbol and stay visually distinct from the
        column-0 turn header. The indent is 8 when timestamps are shown
        (= col 0-4 ts + col 5 space + col 6 symbol + col 7 space + col 8+
        body) and 2 when hidden (= col 0 symbol + col 1 space + col 2+ body).
        """
        self._parent._log().write(
            _indent_body(renderable, self._parent._current_body_indent())
        )

    def _write_reasoning(self, reasoning: str) -> None:
        """#1652 non-streaming path: write the model's reasoning as static dim
        text into the RichLog, BEFORE the reply (correct order). Dim italic
        ``💭 reasoning`` marker + dim body so it reads as the model's thoughts,
        visually subordinate to the answer. The streaming path uses the
        interactive ReasoningBlock instead; this is the correct-order static
        treatment for the monolithic-RichLog reply path (design A)."""
        self._write_body(Text("💭 reasoning", style="italic dim " + _TEXT_MUTED))
        self._write_body(Text(reasoning, style="dim " + _TEXT_MUTED))

    # ── empty state ───────────────────────────────────────────────────────────

    def _consume_empty_hint(self) -> None:
        if self._has_first_message:
            return
        self._has_first_message = True
        try:
            # Hide instead of remove so /clear can bring it back.
            self._parent.query_one("#empty-hint", Static).add_class("hidden")
        except Exception:
            pass

    # ── header grouping (B1) ──────────────────────────────────────────────────

    def _check_new_turn(self, speaker: str, log: "RichLog") -> bool:
        """Return True if this is a new turn (different speaker or window expired).

        Side-effects when a new turn starts:
          - Emits a date-separator when the calendar day has changed.
          - Records the current absolute line position as a turn anchor.
        Does NOT write any header line — the caller decides what to write.

        ``_last_speaker`` / ``_last_speaker_at`` are updated by the caller
        AFTER the header/body write (so anchors are recorded before the write
        that advances the line counter).

        Wave-10 follow-up G-F7: ``time.time()`` (wall clock) matches the
        visible ``HH:MM`` timestamp and grouping decision — see
        ``_maybe_write_header`` docstring for full rationale.
        """
        now = time.time()
        same_speaker = (speaker == self._last_speaker)
        within_window = (now - self._last_speaker_at) < _GROUP_WINDOW_S
        if same_speaker and within_window:
            return False
        # Day boundary marker.
        today = time.strftime("%Y-%m-%d")
        if today != self._last_header_date:
            log.write(_date_separator(today))
            self._last_header_date = today
        # Record a turn anchor.
        self._parent._scroll_ctrl.record_turn_anchor(log)
        return True

    def _maybe_write_header(self, speaker: str, symbol: str,
                             name_style: str) -> None:
        """Write a symbol-only header when the speaker changes or the gap exceeds _GROUP_WINDOW_S.

        The header is ``HH:MM <symbol>`` (ts on) or ``<symbol>`` (ts off).
        A blank line separates turns; the dash rule from the old layout is
        intentionally absent.

        Wave-10 follow-up G-F7: ``time.time()`` (wall clock) rather
        than ``time.monotonic()``. The header's visible HH:MM
        timestamp uses ``time.strftime`` (= wall clock), so grouping
        the *same* timeline keeps the displayed timestamp and the
        grouping decision in lockstep. ``monotonic`` doesn't advance
        during system sleep on every platform (= CLOCK_MONOTONIC vs
        CLOCK_BOOTTIME differ across Linux / macOS) — after a
        sleep/wake cycle, two messages can show wall-clock timestamps
        an hour apart yet share a grouping bucket simply because
        monotonic registered no progress. Wall clock matches the
        user-visible timeline exactly.
        """
        now = time.time()
        log = self._parent._log()
        if self._check_new_turn(speaker, log):
            # No cap. The previous 200-entry cap silently dropped the
            # oldest anchors in long sessions, so Ctrl+P/N's "N / M"
            # readout showed an M smaller than the real turn count and
            # the user thought they had walked the entire history when
            # they had not. ``_resolve_anchors_to_current_view`` already
            # filters anchors whose line position fell below
            # ``log._start_line`` (= dropped by the RichLog ring
            # buffer), so the effective navigation list stays bounded
            # by ``_RICHLOG_MAX_LINES`` / typical-lines-per-turn even
            # though the raw ``_turn_anchors`` list grows unbounded.
            # The raw list cost is ~8 bytes per turn — a 24h session
            # generating one turn per second is ~700 KB, negligible
            # next to the RichLog itself.
            log.write(_msg_header(symbol, name_style, show_ts=self._parent._show_timestamps))
        self._last_speaker = speaker
        self._last_speaker_at = now

    def _maybe_write_inline_header_body(
        self,
        speaker: str,
        symbol: str,
        name_style: str,
        body_text: "Text",
        is_new_turn: "bool | None" = None,
    ) -> None:
        """Write header + body inline (#646 Claude Code style) with col-8 hanging indent.

        When a new turn starts (new speaker or _GROUP_WINDOW_S expired):
          - Emits ``HH:MM <sym> <body_first_line>`` at column 0.
          - Emits each body-wrap continuation at ``_current_body_indent()``
            columns so the wrap visually nests under the body text, not the
            symbol.

        When within the same speaker's grouping window (= header suppressed):
          - Emits body only via ``_write_body`` (= hanging-indent Padding),
            same as the original 2-line path.  The symbol is intentionally
            absent so successive messages from the same speaker don't pile up
            repeated headers.

        Body wrap is computed by splitting ``body_text`` at
        ``pane_width - indent`` cell-width.  The pane width falls back to
        ``_BODY_WIDTH_FALLBACK + indent`` when ``self.size.width`` is 0
        (= widget not yet laid-out or test harness with ``size=(0,0)``).

        This is the load-bearing writer for ``render_user_message`` and
        ``_render_agent_markdown`` (plain-text first-line path).

        ``is_new_turn`` (#1253 Plan A guardrail ①):
          - ``None`` (default): call ``_check_new_turn`` + update
            ``_last_speaker`` / ``_last_speaker_at`` as usual (non-streaming
            callers: ``render_user_message``, ``_render_agent_markdown``).
          - A bool: use it directly. Do NOT call ``_check_new_turn`` (skip its
            side-effects) and do NOT update ``_last_speaker`` /
            ``_last_speaker_at`` (the streaming caller already anchored them in
            ``begin_stream``).
        """
        log = self._parent._log()
        if is_new_turn is None:
            now = time.time()
            is_new_turn = self._check_new_turn(speaker, log)
            self._last_speaker = speaker
            self._last_speaker_at = now

        indent = self._parent._current_body_indent()

        if not is_new_turn:
            # Grouped turn: no header, just body at hanging-indent.
            self._write_body(body_text)
            return

        # New turn: build inline header prefix.
        prefix = _build_header_prefix(symbol, name_style, show_ts=self._parent._show_timestamps)
        prefix_cells = cell_len(prefix.plain)

        # Compute body wrap width = pane_width minus the indent column.
        # The first body line starts at col ``prefix_cells`` (= same as
        # ``indent`` by design: both are 8 with ts-on, 2 with ts-off).
        try:
            pane_width = self._parent.size.width
            if pane_width <= 0:
                pane_width = _BODY_WIDTH_FALLBACK + indent
        except Exception:
            pane_width = _BODY_WIDTH_FALLBACK + indent
        # RichLog has 1-cell padding on each side (see CSS: padding: 0 1).
        body_width = max(10, pane_width - indent - 2)

        # Split body_text into wrap lines at body_width.
        try:
            from rich.console import Console as _Console
            _buf_console = _Console(width=body_width, highlight=False)
            wrapped_lines = list(body_text.wrap(_buf_console, body_width))
        except Exception:
            wrapped_lines = [body_text]

        if not wrapped_lines:
            # Empty body: emit just the header prefix as a standalone line.
            log.write(prefix)
            return

        # First line: header prefix + first body-wrap line inline.
        first_line = prefix + wrapped_lines[0]
        log.write(first_line)

        # Remaining wrap lines: indented to ``indent`` col via Padding.
        for cont in wrapped_lines[1:]:
            if cont.plain.strip():  # skip blank-only continuation lines
                log.write(_indent_body(cont, indent))

    # ── non-streaming message rendering ───────────────────────────────────────

    def render_message(self, msg: "OutboxMessage") -> None:
        """Append an OutboxMessage to the log (or route via dedicated widget).

        Routing:
          agent      → header + Markdown inline (with B3 fold for >30 lines)
          system     → header + plain-text inline (persistent slash output)
          intervention/trace/status/skill_done → suppressed (handled elsewhere)
          error      → inline Rich Text written into the RichLog
          others     → plain Rich Text line
        """
        if msg.kind == "intervention":
            # Interventions are handled via mount_intervention, not here.
            self._consume_empty_hint()
            self._write_log(_format_intervention_line(msg))
            return

        if msg.kind in {"trace", "status", "skill_done"}:
            # Suppressed — these are handled by:
            #   trace       → start/update_skill_row (driven from app.py)
            #   status      → show_status (sticky)
            #   skill_done  → finish_skill_row (driven from app.py)
            return

        if msg.kind == "agent":
            self._render_agent_markdown(msg)
            return

        if msg.kind == "system":
            self._render_system_message(msg)
            return

        if msg.kind == "error":
            # W13 A#2: derive short keys from full meta when missing.
            # forwarder.py populates meta["skill_name"] + meta["run_id_short"]
            # for skill-context errors; direct router emissions (classify path,
            # chain_timeout, chain_peer_discarded) only set meta["skill"]
            # (full name) and meta["run_id"] (full id). Deriving here at the
            # TUI seam restores the [skill#abcd] prefix + re-enables the
            # Ctrl+B trace hint footer for all router-emitted errors.
            skill_name = str(
                msg.meta.get("skill_name") or msg.meta.get("skill", "")
            )
            run_id_raw = str(msg.meta.get("run_id", ""))
            run_id_short = str(
                msg.meta.get("run_id_short") or (run_id_raw[-4:] if run_id_raw else "")
            )
            details = str(msg.meta.get("details", ""))
            # W13 A#1: when details is empty, build context lines from
            # well-known meta keys so the expand region surfaces structured
            # provenance rather than just repeating the header message.
            context_lines: list[str] = []
            if not details:
                for key in ("chain_id", "skill", "run_id", "dimension"):
                    val = msg.meta.get(key)
                    if val:
                        context_lines.append(f"{key}={val}")
            self.write_error(
                message=msg.text,
                details=details,
                run_id_short=run_id_short,
                skill_name=skill_name,
                context_lines=context_lines,
                meta=msg.meta,
            )
            return

        text = _format_message(msg)
        if text is not None:
            self._consume_empty_hint()
            self._write_body(text)

    def render_user_message(self, text: str) -> None:
        """Render a freshly submitted user message with grouped header.

        Submitting a message is an "I'm re-engaging" signal — even if the
        user had previously scrolled up to read history, they now want to
        see the conversation continue. Snap back to the bottom and re-arm
        auto-scroll so subsequent agent reply chunks track the tail.

        #646 Claude Code-style inline layout: ``HH:MM > message text`` on the
        same logical line, with wrap continuations landing at col 8
        (``_BODY_INDENT_WITH_TS``) so they nest visually under the body text.
        """
        self._parent._scroll_ctrl.snap_to_bottom()
        self._consume_empty_hint()
        self._maybe_write_inline_header_body(
            "you", _GLYPH_USER, "bold #4abbb5", Text(text),
        )
        self._write_log(Text(""))

    def _render_system_message(self, msg: "OutboxMessage") -> None:
        """Render a slash-command (or other OS-generated) message persistently.

        Distinct from ``agent`` so the log doesn't claim the LLM produced
        these lines, and distinct from ``status`` so prior outputs survive
        when running multiple commands in a row.

        Rendered as plain text (newlines preserved, no Markdown) under a
        neutral ``system`` header in dim grey. **Exception**: lifecycle
        markers (= ``[↑ ... ]`` shape from ``ChatLifecycleForwarder``)
        skip the speaker header and render as a dim inline divider —
        they're state-change announcements, not speech, and don't
        deserve the same visual weight as a slash-command output.
        """
        self._consume_empty_hint()
        self._parent.stop_thinking()
        self._parent.hide_status()
        text = msg.text or ""
        if _is_lifecycle_marker(text):
            self._write_log(_render_lifecycle_marker(text))
            return
        self._maybe_write_header("system", _GLYPH_SYSTEM, "bold " + _TEXT_MUTED)
        for line in text.splitlines() or [""]:
            self._write_body(Text(line))
        self._write_log(Text(""))

    def _render_agent_markdown(self, msg: "OutboxMessage") -> None:
        """Render a non-streaming agent message inline in the log.

        Writes Markdown directly into the RichLog (as a Rich renderable) so
        agent replies appear under their header instead of being pushed to
        the bottom of the pane. Hides any sticky "thinking…" indicator that
        was active for this turn.

        #646 Claude Code-style inline layout: ``HH:MM ⏺ [meta] first_line``
        appears on the same logical line.  The first plain-text line of the
        message body (or the meta prefix when present) is extracted and
        inlined with the header via ``_maybe_write_inline_header_body``.
        The remaining Markdown body (if any) is written at col-8 indent.

        Implementation note (structural vs simple tradeoff):
          - Markdown formatting on the first line is rendered as plain text
            (e.g. ``**bold**`` shows as plain ``bold``). For the typical
            agent reply, the first line is prose — this is an acceptable
            trade-off that avoids reimplementing Rich's Markdown renderer.
          - When no body text is present (= header-only turn signal), only
            the header prefix is emitted.
        """
        self._consume_empty_hint()
        self._parent.stop_thinking()  # turn finished — unmount inline spinner
        self._parent.hide_status()    # also clear any sticky status
        # #1652: a pending reasoning signal → write it as static text into the
        # RichLog BEFORE the reply (correct order). The non-streaming reply lands
        # in the monolithic RichLog, so a mounted ReasoningBlock would sit below
        # it (wrong order) — static log text is the correct-order treatment here
        # (the interactive ReasoningBlock is reserved for the streaming path).
        reasoning = self._parent.consume_pending_reasoning()
        if reasoning:
            self._write_reasoning(reasoning)
        meta_pfx = _meta_prefix(msg.meta)
        body_text = msg.text or ""

        # Build the inline first-line text: meta prefix (if any) + first body
        # line stripped of Markdown markup.  For an agent reply that begins
        # "**Plan**:" this reads "Plan:" in the inline header — acceptable.
        if meta_pfx:
            first_line_plain = meta_pfx.rstrip()
            if body_text:
                # Append first source line (stripped of leading # / * / `) to
                # keep the header line short and readable.
                src_first = _plain_first_line(body_text)
                if src_first:
                    first_line_plain = f"{first_line_plain} {src_first}"
        elif body_text:
            first_line_plain = _plain_first_line(body_text)
        else:
            first_line_plain = ""

        inline_body = Text(first_line_plain, style="dim " + _TEXT_MUTED if meta_pfx else "")

        # _AMBER for agent identity — distinct from _CORAL (interactive
        # affordances).
        self._maybe_write_inline_header_body(
            "reyn", _GLYPH_AGENT, "bold " + _AMBER, inline_body,
        )

        # Write the full Markdown body (all lines) at hanging-indent.
        # This causes the first line to appear twice when body_text has one
        # line, but for Markdown replies the full render at col 8 gives
        # correct formatting (= bold, code, lists) for lines 2+.
        # When body has only one line, suppress the duplicate body write.
        if body_text:
            body_lines = body_text.splitlines()
            if len(body_lines) > 1:
                # Remaining lines as Markdown (preserving formatting).
                rest = "\n".join(body_lines[1:])
                self._write_agent_markdown(rest)
            # Single-line body: already rendered inline; no extra write needed.
        self._write_log(Text(""))

    def _write_agent_markdown(self, text: str) -> None:
        """Write ``text`` as full inline Markdown (Claude-Code-style, no collapse).

        All replies — regardless of length — are rendered directly into the
        RichLog via ``_write_body``. Fold machinery removed per user direction
        ("会話 reply は fold しなくて良い、claude code みたいに").

        Side effect: appends ``text`` to ``self._recent_replies`` (capped
        at ``_RECENT_REPLIES_MAX``) so the /copy slash command can hand
        any of the last N replies to the system clipboard.
        """
        self._recent_replies.append(text)
        if len(self._recent_replies) > _RECENT_REPLIES_MAX:
            self._recent_replies = self._recent_replies[-_RECENT_REPLIES_MAX:]
        self._write_body(RichMarkdown(text))

    def last_reply_text(self) -> "str | None":
        """Return the full text of the most recent agent reply (any length).

        Used by the /copy slash command. Returns None when there has been no
        agent reply in this session yet.
        """
        return self._recent_replies[-1] if self._recent_replies else None

    def reply_at(self, n: int) -> "str | None":
        """Return the n-th most recent agent reply (1-indexed; n=1 is latest).

        Returns None when ``n`` is out of range (≤ 0 or beyond the buffered
        history). The /copy slash uses this to surface older replies that the
        single-slot predecessor silently lost on every new turn.
        """
        if n <= 0 or n > len(self._recent_replies):
            return None
        return self._recent_replies[-n]

    def recent_reply_count(self) -> int:
        """Number of agent replies currently held in the /copy ring buffer."""
        return len(self._recent_replies)

    def find_in_buffer(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> "list[tuple[int, str]]":
        """Return ``(line_idx, line_text)`` for every RichLog line matching ``query``.

        Search modes (off → on, two independent flags):
          - ``regex=False`` (default): substring match — fast, no
            metacharacters interpreted
          - ``regex=True``: compiled regex search via ``re.search``;
            invalid patterns raise ``re.error`` so the caller can
            surface a clear error status
          - ``case_sensitive=False`` (default): match across cases
            (= ``"Foo"`` query matches ``"foo bar"``)
          - ``case_sensitive=True``: exact-case match

        Scope: the live RichLog buffer (= what's currently
        scrollable). Lines past the ``_RICHLOG_MAX_LINES`` ring-
        buffer trim are NOT searched — for older history the user
        has the right-panel Events tab + the agent-side
        ``.reyn/events/agents/<name>/`` skill-run directories.

        Empty ``query`` returns an empty list (= caller treats this
        as "nothing to search for", usually with a usage hint).

        Each tuple's ``line_text`` carries the line's rendered
        plain text (via the Strip's ``.text`` property) so callers
        can surface a short preview alongside the line number.
        """
        q = (query or "").strip()
        if not q:
            return []
        log = self._parent._log()
        out: list[tuple[int, str]] = []

        if regex:
            import re
            # ``re.error`` (invalid pattern) bubbles up — callers
            # catch it and surface a status; silent suppression
            # would leave the user wondering why ``/find -r foo(``
            # silently matched nothing.
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(q, flags)
            for idx, strip in enumerate(getattr(log, "lines", []) or []):
                text = getattr(strip, "text", "") or ""
                if pattern.search(text):
                    out.append((idx, text))
            return out

        # Substring path — same lookup whether case-sensitive or
        # not; only the comparison case differs.
        needle = q if case_sensitive else q.lower()
        for idx, strip in enumerate(getattr(log, "lines", []) or []):
            text = getattr(strip, "text", "") or ""
            haystack = text if case_sensitive else text.lower()
            if needle in haystack:
                out.append((idx, text))
        return out

    def dump_buffer_text(self) -> "list[str]":
        """Return the plain-text rendering of every line in the RichLog buffer.

        Sibling to :meth:`find_in_buffer` — same buffer scope (= the
        live, scrollable RichLog content; not historical events past
        ``_RICHLOG_MAX_LINES``), but returns the full ordered list
        without filtering. Used by ``/save`` to materialise the conv
        pane to a text file. Each entry is a single line (= no
        embedded newlines), already stripped of ANSI / Rich markup
        by Strip's ``.text`` property.
        """
        log = self._parent._log()
        return [
            getattr(strip, "text", "") or ""
            for strip in (getattr(log, "lines", []) or [])
        ]

    # ── inline error rendering ────────────────────────────────────────────────

    def write_error(
        self,
        *,
        message: str,
        details: str = "",
        run_id_short: str = "",
        skill_name: str = "",
        context_lines: "list[str] | None" = None,
        meta: "dict | None" = None,
    ) -> None:
        """Render an error as inline Rich Text into the conv RichLog.

        Errors flow as plain log lines — ephemeral, scroll-away (= Claude
        Code style).  Severity colour is applied inline.  No widget is
        mounted; no dismiss mechanic is needed.

        Lines written:
          1. ``✗ [prefix]: <first-line>``  in the severity colour.
          2. ``  • <inline-hint>`` in _HINT_ACTION  (when present).
          3. Detail lines (first 5 + ``… N more``) dim, OR context_lines.
          4. ``Ctrl+B → events for full trace`` dim  (when has_trace).
          5. A blank separator line.
        """
        from rich.markup import escape as _markup_escape
        from rich.text import Text as _RichText

        self._consume_empty_hint()
        self._parent.stop_thinking()  # turn ended in error

        # Scrolled-up cue — show "error below" when user is reading history.
        if self._parent._scroll_ctrl.user_scrolled:
            self._parent.show_status("✗ error below ↓", kind="general")
        else:
            self._parent.hide_status()

        # Severity → colour.
        severity = _classify_error_severity(message, meta or {})
        if severity == "high":
            sev_color = _SEV_HIGH
        elif severity == "med":
            sev_color = _SEV_MED
        else:
            sev_color = _TEXT_MUTED  # low

        # Build prefix: [skill#run_id] / [skill] / [#run_id] / "".
        if skill_name and run_id_short:
            prefix = f"[{skill_name}#{run_id_short}]"
        elif skill_name:
            prefix = f"[{skill_name}]"
        elif run_id_short:
            prefix = f"[#{run_id_short}]"
        else:
            prefix = ""

        # Extract trailing " • <hint>" from the first message line.
        first_line, _sep, _rest = message.partition("\n")
        if " • " in first_line:
            detail_part, _bullet, hint_part = first_line.partition(" • ")
            inline_hint = hint_part.strip()
            first_line_for_header = detail_part
        else:
            inline_hint = ""
            first_line_for_header = first_line

        # Header line: ✗ [prefix]: message-first-line.
        header = _RichText()
        if prefix:
            header.append(
                f"✗ {_markup_escape(prefix)}: {_markup_escape(first_line_for_header)}",
                style="bold " + sev_color,
            )
        else:
            header.append(
                f"✗ {_markup_escape(first_line_for_header)}",
                style="bold " + sev_color,
            )
        self._write_log(header)

        # Inline hint line.
        if inline_hint:
            hint_t = _RichText(f"  • {_markup_escape(inline_hint)}", style=_HINT_ACTION)
            self._write_log(hint_t)

        # Detail / context lines.
        has_trace = bool(skill_name or run_id_short)
        if details:
            lines = details.splitlines()
            visible = lines[:5]
            overflow = len(lines) - 5
            for ln in visible:
                self._write_log(_RichText(f"  {_markup_escape(ln)}", style=_TEXT_MID))
            if overflow > 0:
                self._write_log(_RichText(f"  … {overflow} more", style=_TEXT_MID))
        elif context_lines:
            for ln in context_lines:
                self._write_log(_RichText(f"  {_markup_escape(ln)}", style=_TEXT_MID))

        # Ctrl+B trace pointer.
        if has_trace:
            self._write_log(
                _RichText("  Ctrl+B → events for full trace", style="dim " + _TEXT_DIM)
            )

        # Trailing blank separator.
        self._write_log(_RichText(""))

    # ── cost suffix (A4) ──────────────────────────────────────────────────────

    def render_cost_suffix(
        self,
        tokens: int,
        cost_usd: float,
        elapsed_s: float,
        *,
        partial: bool = False,
    ) -> None:
        """Append a dim per-turn cost suffix, right-aligned. Caller decides when (opt-in).

        Three pieces are load-bearing:

        - ``Text(..., justify="right")`` only right-aligns when the
          renderer is told a width to fill, and ``RichLog.write`` defaults
          to ``expand=False`` — without ``expand=True`` the suffix
          silently renders at column 0.
        - Wrapping the Text in ``Padding(text, (0, RIGHT_PAD, 0, 0))``
          reserves explicit right-margin cells. Without that margin the
          right-justified end of the suffix landed under the vertical
          scrollbar + the small right-padding RichLog reserves and the
          trailing ``N.Ns`` segment was clipped at 80-col terminal widths
          (the ``expand=True`` fill width didn't subtract the scrollbar
          column or RichLog's own right-padding from the right edge of
          the right-justified text).
        - Separator is ``│`` (U+2502, narrow, unambiguous width) rather
          than ``·`` (U+00B7, East Asian Width "Ambiguous") so Rich's
          cell-width accounting matches what the terminal actually paints.

        ``partial=True`` prefixes each numeric segment with ``~`` and
        appends ``  (skill still running)``. Wave-6 ST3 + wave-7 C-F6:
        when the cost-suffix deferral cap fires while a skill is still
        spinning, the snapshot under-reports the eventual total. Without
        a visual marker, the user sees ``⌁ Nt │ $X.XXXX │ Ys`` and
        treats it as the final number. The ``~`` + suffix make the
        partial nature visible at a glance; a subsequent terminal-state
        emit overrides this line with the final number.
        """
        from rich.padding import Padding
        if partial:
            body = (
                f"⌁ ~{tokens}t │ ~${cost_usd:.4f} │ ~{elapsed_s:.1f}s"
                "  (skill still running)"
            )
        else:
            body = f"⌁ {tokens}t │ ${cost_usd:.4f} │ {elapsed_s:.1f}s"
        t = Text(
            body,
            style="dim " + _TEXT_NEUTRAL,
            justify="right",
        )
        # ``(top=0, right=6, bottom=0, left=0)`` — reserve 6 cells on
        # the right so the right-justified text stays inside the
        # scrollbar + widget right-padding. Empirical calibration at
        # 80-col widths: 2 cells still clipped (``17.``), 3 still clipped
        # (``17.1.``), 6 leaves the full ``N.Ns`` segment + a visible gap
        # from the scrollbar. The ``expand=True`` fill width doesn't
        # subtract the scrollbar column or RichLog's own right-padding
        # from the right edge of the right-justified text, so we have to
        # reserve it manually.
        self._parent._log().write(Padding(t, (0, 6, 0, 0)), expand=True)


class _StreamController:
    """Owns the in-flight streaming state and all stream lifecycle methods.

    Extracted from ConversationView (tui-pr4). ConversationView retains
    begin_stream / append_stream / end_stream / end_stream_cancelled as
    thin 1-line delegates so the external call-site API is unchanged.

    State owned:
        _stream_rows      — msg_id → StreamingRow widget
        _stream_new_turn  — msg_id → is_new_turn bool (stream-start grouping
                            decision, #1253 Plan A guardrail ①)
    """

    def __init__(self, parent: "ConversationView") -> None:
        self._parent = parent
        self._stream_rows: dict[str, StreamingRow] = {}
        self._stream_new_turn: dict[str, bool] = {}

    @property
    def stream_rows(self) -> "dict[str, StreamingRow]":
        """Shallow copy of the in-flight stream-row registry (msg_id → row)."""
        return dict(self._stream_rows)

    def reset_for_clear(self) -> None:
        """Seal all in-flight rows and clear both dicts (called by clear())."""
        for row in self._stream_rows.values():
            row.seal()
        self._stream_rows.clear()
        self._stream_new_turn.clear()

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        """Start a streaming agent message row. Returns the row widget.

        #1253 Plan A (guardrail ①): capture the grouping decision at START
        so that the inline header written at seal-time reflects the stream-start
        timing, not the (potentially different) seal timing.  The date-separator
        and turn-anchor side-effects of ``_check_new_turn`` fire here; the
        actual header write is deferred to ``end_stream`` via
        ``_commit_stream_inline``.
        """
        self._parent._consume_empty_hint()
        log = self._parent._log()
        is_new_turn = self._parent._check_new_turn("reyn", log)
        self._parent._renderer.set_speaker("reyn", time.time())
        row = StreamingRow(
            prefix="",
            id=f"stream_{msg_id[:8]}",
            indent=self._parent._current_body_indent(),
        )
        self._stream_rows[msg_id] = row
        self._stream_new_turn[msg_id] = is_new_turn
        # #1652: a pending reasoning signal (emitted just before this reply) →
        # mount the interactive ReasoningBlock FIRST so it renders above the
        # StreamingRow (DOM order: RichLog history, ReasoningBlock, StreamingRow).
        reasoning = self._parent.consume_pending_reasoning()
        if reasoning:
            self._parent.mount(ReasoningBlock(reasoning=reasoning))
        self._parent.mount(row)
        return row

    def append_stream(self, msg_id: str, text: str) -> None:
        row = self._stream_rows.get(msg_id)
        if row is not None:
            row.append(text)

    def _commit_stream_inline(self, full: str, is_new_turn: bool) -> None:
        """Commit the sealed streaming reply as inline ``HH:MM ⏺ first-line`` form.

        #1253 Plan A: called from ``end_stream`` with the grouping decision
        captured at stream-START.

        /copy preservation: records the FULL reply in ``_recent_replies`` here
        (NOT just the rest after the first line) so ``last_reply_text()``
        returns the whole thing.  Using ``_write_agent_markdown(rest)`` would
        record only the body minus the first line, which would truncate the
        /copy result.
        """
        self._parent._renderer.record_reply(full)
        first_line_plain = _plain_first_line(full)
        self._parent._maybe_write_inline_header_body(
            "reyn", _GLYPH_AGENT, "bold " + _AMBER, Text(first_line_plain),
            is_new_turn=is_new_turn,
        )
        body_lines = full.splitlines()
        if len(body_lines) > 1:
            rest = "\n".join(body_lines[1:])
            self._parent._write_body(RichMarkdown(rest))

    def end_stream(self, msg_id: str) -> str:
        """Seal the stream and flush the final content INTO the RichLog inline,
        then remove the transient StreamingRow widget so the bottom of the
        pane stays empty (or holds the next streaming row).

        Replies render inline as ``HH:MM ⏺ <first-line>`` with grouping
        captured at stream-start (#1253 Plan A).
        """
        row = self._stream_rows.pop(msg_id, None)
        if row is None:
            return ""
        full = row.full_text()
        row.seal()
        self._parent.stop_thinking()
        self._parent.hide_status()
        is_new_turn = self._stream_new_turn.pop(msg_id, True)
        if full:
            try:
                self._commit_stream_inline(full, is_new_turn)
            except Exception:
                self._parent._log().write(Text(full))
        self._parent._log().write(Text(""))
        try:
            row.remove()
        except Exception:
            pass
        return full

    def end_stream_cancelled(self, msg_id: str) -> str:
        """Seal a cancelled stream and write a visually-differentiated partial.

        Wave-9 F-F7: the previous cancel path called ``end_stream``
        which committed the partial text via the same
        ``_write_agent_markdown`` formatting used for
        complete replies, then appended a separate ``"  ⌁ cancelled"``
        suffix line. Scrolling back through history the user couldn't
        tell which was a cancelled fragment vs a real reply — the
        partial rendered with full Markdown styling (= bold / headers /
        code blocks / etc.), and the dim suffix was easy to miss
        because it sat below the visible region for any reply taller
        than the viewport.

        The cancelled path now:
          - emits a clear ``✗ cancelled (partial reply):`` header BEFORE
            the partial text, in bold dim-red so it sits in the
            user's eyeline at the top of the fragment
          - renders the partial body as plain dim italic text (no
            Markdown). Partial text usually has half-closed code
            fences / unclosed lists / broken bold spans, so Markdown
            rendering produced wrong styling anyway — the dim plain
            text reads as "incomplete fragment".

        The normal ``end_stream`` path is unchanged; only the explicit
        cancel call site in ``action_cancel_inflight`` routes through
        here.
        """
        row = self._stream_rows.pop(msg_id, None)
        if row is None:
            return ""
        full = row.full_text()
        row.seal()
        self._parent.stop_thinking()
        self._parent.hide_status()
        self._stream_new_turn.pop(msg_id, None)
        if full:
            self._parent._renderer.record_reply(full)
            try:
                self._parent._log().write(
                    Text("✗ cancelled (partial reply):", style=f"bold {_RED_MUTED}"),
                )
                self._parent._write_body(Text(full, style="dim italic " + _TEXT_MUTED))
            except Exception:
                self._parent._log().write(Text(full))
        self._parent._log().write(Text(""))
        try:
            row.remove()
        except Exception:
            pass
        return full


class ConversationView(Widget):
    """Main conversation pane: RichLog + inline widgets + sticky status.

    Streaming:
      - `begin_stream(msg_id, agent_name)` → mounts a StreamingRow.
      - `append_stream(msg_id, text)` → calls StreamingRow.append().
      - `end_stream(msg_id)` → seals the row.

    Skill activity (replaces trace lines):
      - `start_skill_row(run_id, skill_name)` → mounts a SkillActivityRow.
      - `update_skill_phase(run_id, phase, visit)` → set_phase on the row.
      - `finish_skill_row(run_id, success, reason)` → finish the row.

    Sticky status:
      - `show_status(text, kind)` / `hide_status()` → drive StickyStatus.

    Errors:
      - `write_error(message, details, ...)` → inline Rich Text in the log
        (ephemeral, scrolls away naturally like any other log content).
    """

    DEFAULT_CSS = """
    ConversationView {
        height: 1fr;
        padding: 0 0;
    }
    ConversationView RichLog {
        background: transparent;
        height: 1fr;
        border: none;
        /* Dim by default; only the active/hover scrollbar uses the coral
           highlight. Avoids the full-track coral block when content fits
           in one viewport. */
        scrollbar-color: #2a2a2a;  /* _BORDER_DIM */
        scrollbar-color-hover: $primary;
        scrollbar-color-active: $primary;
        scrollbar-background: transparent;
        scrollbar-background-hover: transparent;
        scrollbar-background-active: transparent;
        scrollbar-corner-color: transparent;
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        padding: 0 1;
    }
    ConversationView #empty-hint {
        color: #555555;  /* _TEXT_DIM */
        padding: 0 1;
        height: auto;
    }
    ConversationView #empty-hint.hidden {
        display: none;
    }
    /* New-messages-below affordance: a 1-line dim-coral strip docked just
       above the sticky/async area, shown ONLY while the user is scrolled up
       AND content arrived after they locked. ``.hidden`` collapses it to
       zero height in the (default) following case so the layout is unchanged.
       Coral = the action-accent (= "jump here"). */
    ConversationView #new-below {
        dock: bottom;
        height: auto;
        color: #C8553D;  /* _CORAL */
        background: #1a1a1a;  /* _BG_HEADER — subtle strip so it reads as chrome */
        padding: 0 1;
    }
    ConversationView #new-below.hidden {
        display: none;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Stream controller: owns _stream_rows, _stream_new_turn, and all
        # stream lifecycle methods (refactor tui-pr4).
        self._stream_ctrl = _StreamController(self)
        # Inline row manager: owns _skill_rows, _tool_call_rows,
        # _last_failed_tool_row and all methods that operate on them
        # (refactor tui-pr1).
        self._row_mgr = _InlineRowManager(self)
        # Status/spinner controller: owns StickyStatus + InlineThinkingRow
        # methods (refactor tui-pr2).
        self._status_ctrl = _StatusSpinnerController(self)
        # Scroll controller: owns scroll-lock, turn-nav, '↓ N new' indicator
        # (refactor tui-pr3).
        self._scroll_ctrl = _ScrollController(self)
        # Message renderer: owns header-grouping state, recent-replies ring,
        # empty-hint latch, and all rendering logic (refactor tui-pr5).
        self._renderer = _MessageRenderer(self)
        # F9 timestamp toggle — default on. Loaded from tui_prefs.json in
        # on_mount; callers use toggle_timestamps() / show_timestamps property.
        # New messages rendered after a toggle use the new indent; past
        # messages stay at whatever indent they had when rendered (= no
        # full re-render of scroll history).
        self._show_timestamps: bool = True
        # #1652: reasoning text from the discrete ``kind="reasoning"`` outbox
        # signal (emitted immediately before its reply). Stored on receipt; the
        # NEXT reply render consumes it path-appropriately — streaming mounts an
        # interactive ReasoningBlock before the StreamingRow, non-streaming writes
        # static reasoning text before the reply in the RichLog. Deferring the
        # consume (vs mounting on receipt) is load-bearing: a widget mounted at
        # signal time sits BELOW the monolithic RichLog, so the non-streaming
        # reply (RichLog text) would render above it — wrong order.
        self._pending_reasoning: str | None = None

    # ── #1652 reasoning ────────────────────────────────────────────────────────

    def set_pending_reasoning(self, reasoning: str) -> None:
        """Store the reasoning text for the upcoming reply (from the discrete
        ``kind="reasoning"`` outbox signal). Consumed by the next reply render."""
        self._pending_reasoning = reasoning or None

    def consume_pending_reasoning(self) -> "str | None":
        """Return + clear any pending reasoning. The reply-render path calls this
        and renders the result path-appropriately (mounted block / static text).
        Returns None when there is nothing pending (no reasoning this turn / off)."""
        pending, self._pending_reasoning = self._pending_reasoning, None
        return pending

    # ── composition ──────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # ``can_focus = False`` is the load-bearing piece: RichLog inherits
        # ``can_focus = True`` from ScrollView, so a stray click on the conv
        # pane (or Shift+Tab from the input bar) silently shifts focus here.
        # The log is append-only and accepts no typed input, so the user then
        # types into nothing until they click the input bar again. Ctrl+P/N
        # turn-nav already calls ``log.scroll_to`` without needing focus, so
        # disabling focus loses no real capability.
        log = RichLog(highlight=False, markup=False, wrap=True,
                      max_lines=_RICHLOG_MAX_LINES, id="log")
        log.can_focus = False
        yield log
        # B5: empty-state hint, removed on first message.
        # The hint is the first thing a new user sees, so it has to do double
        # duty: announce the core key-binds AND describe the side panel — the
        # most sophisticated piece of the TUI, which is hidden by default and
        # otherwise has zero discoverability on first run.
        yield Static(
            # Three-line layout (each ~40-50 cells) so the hint stays
            # legibly stacked at the narrowest reachable conv-pane
            # width. The previous single line 2 carried the slash
            # mechanic + tab list + keybind set on one line at ~95
            # cells, which wrapped to 4–5 ragged lines when the right
            # panel was expanded toward its max width and the conv pane
            # shrank to ~50 cols. Splitting into purpose / contents /
            # keybind preserves the side-panel discoverability the test
            # at ``test_empty_hint_discoverability.py`` defends, while
            # making each line short enough that wrap is at worst 2
            # clean lines per stanza on any terminal we expect.
            "  Type [bold]/[/] for commands  ·  [bold]/help[/] for a guide\n"
            "  [bold]Ctrl+B[/] side panel ·  [bold]Ctrl+L[/] clear  ·  [bold]Ctrl+P/N[/] turn  ·  [bold]Ctrl+R[/] voice\n"
            "  [dim]panel tabs: keys · events · agents · memory · cost · docs · pending"
            "  ([bold]Tab[/]/[bold]Ctrl+W[/] cycle)[/]",
            id="empty-hint",
            markup=True,
        )
        # A3: sticky status pinned to bottom of conv pane (= 1-line
        # ``⟳ thinking…`` / ``⚑ awaiting answer`` live indicator).
        # AsyncStackPanel docks ABOVE the sticky (= the 0-5-line
        # "currently running tasks" overview). Per Textual's
        # ``dock: bottom`` stacking rule (= multiple bottom-docked
        # siblings stack in declaration order, with each new sibling
        # placed further FROM the bottom edge), yielding StickyStatus
        # first pins it to the very bottom; the subsequent
        # AsyncStackPanel sits above it. Result from bottom-up:
        # ``input bar → sticky → async stack → conv log``.
        # AsyncStackPanel collapses to zero height when no tasks are
        # active so the input bar layout is unchanged in the
        # cold-default case.
        yield StickyStatus(id="sticky-status")
        yield AsyncStackPanel(id="async-stack")
        # New-messages-below affordance. Yielded LAST so the dock:bottom
        # stacking rule places it ABOVE sticky + async-stack (= right at the
        # bottom edge of the scrollable log, where new content lands).
        # Starts hidden; ``_update_new_below`` toggles it while the user is
        # scrolled up and content arrives below their locked viewport.
        yield Static("", id="new-below", classes="hidden", markup=True)

    def _log(self) -> RichLog:
        return self.query_one("#log", RichLog)

    def _sticky(self) -> StickyStatus | None:
        try:
            return self.query_one("#sticky-status", StickyStatus)
        except Exception:
            return None

    def _async_stack(self) -> AsyncStackPanel | None:
        """Return the bottom-docked AsyncStackPanel, if mounted.

        Mirrors ``_sticky()`` — the panel may not exist during
        composition in test harnesses that mount ConversationView
        manually without going through the full compose path.
        """
        try:
            return self.query_one("#async-stack", AsyncStackPanel)
        except Exception:
            return None

    def add_async_task(self, task_id: str, summary: str) -> None:
        """Add or update an attached-agent task in the bottom stack panel.

        Wiring helper for the production hook (= app.py's
        ``_handle_trace_for_skill_row`` calls this on the
        ``"phase started:"`` first-trace branch and on plan
        spawn events). ``task_id`` is the canonical task identity
        — ``run_id`` for skill spawns or ``plan_id`` for plan
        spawns; both flow through this single entry point.

        Silent no-op when the panel isn't mounted (= test
        harness path), matching the ``show_status`` / ``hide_status``
        defensive style.
        """
        panel = self._async_stack()
        if panel is None or not task_id:
            return
        panel.add(task_id, summary)

    def remove_async_task(
        self,
        task_id: str,
        *,
        terminal: str = "ok",
    ) -> None:
        """Drop ``task_id``'s entry from the bottom stack panel.

        Called on ``"skill done:"`` from the trace flow + the
        corresponding plan-completion path. Idempotent (= silent
        no-op if the panel isn't mounted or the task is unknown).

        Wave-13 T2-2: ``terminal`` is threaded through to
        ``AsyncStackPanel.remove`` so aborted / interrupted lifecycle
        events can trigger the red flash-before-unmount behaviour.
        Valid values: ``"ok"`` (default, immediate unmount),
        ``"aborted"``, ``"interrupted"``.
        """
        panel = self._async_stack()
        if panel is None or not task_id:
            return
        # Coerce to the Literal values AsyncStackPanel.remove accepts.
        # Anything unrecognised falls back to "ok" (= existing safe default).
        if terminal in ("aborted", "interrupted"):
            panel.remove(task_id, terminal=terminal)  # type: ignore[arg-type]
        else:
            panel.remove(task_id)

    def clear_async_tasks(self) -> None:
        """Reset the bottom stack panel to empty.

        Called from ``clear()`` so a Ctrl+L wipes the running-task
        overview alongside the conv log. The visible stack would
        otherwise survive the clear and look like "ghost rows"
        attached to a fresh-looking pane.
        """
        panel = self._async_stack()
        if panel is None:
            return
        panel.clear()

    # ── timestamp toggle (F9) ─────────────────────────────────────────────────

    @property
    def show_timestamps(self) -> bool:
        """True when the ``HH:MM`` prefix is prepended to speaker headers."""
        return self._show_timestamps

    def _current_body_indent(self) -> int:
        """Return the dynamic body-indent column based on ``_show_timestamps``.

        ON  → ``_BODY_INDENT_WITH_TS`` (8) — body under the symbol when ts shown.
        OFF → ``_BODY_INDENT_NO_TS``  (2) — symbol at col 0, body at col 2.
        """
        return _BODY_INDENT_WITH_TS if self._show_timestamps else _BODY_INDENT_NO_TS

    def toggle_timestamps(self) -> bool:
        """Flip the timestamp-visibility state and persist to ``tui_prefs.json``.

        Returns the NEW state (True = ts now visible, False = now hidden).
        The toggle applies to NEW messages only (= no re-render of past
        history). Past messages stay at whatever indent they were rendered
        with — a full re-render would be expensive and confusing mid-session.
        """
        self._show_timestamps = not self._show_timestamps
        try:
            from reyn.interfaces.tui.prefs import load_tui_prefs, save_tui_prefs
            root = None
            try:
                root = self.app._project_root_path()  # type: ignore[attr-defined]
            except Exception:
                pass
            prefs = load_tui_prefs(root)
            prefs["show_timestamps"] = self._show_timestamps
            save_tui_prefs(root, prefs)
        except Exception:
            pass
        return self._show_timestamps

    # ── Public state accessors (Tier C Path C — replaces private-attr asserts) ─

    @property
    def user_scrolled(self) -> bool:
        """True when the user has manually scrolled away from the tail."""
        return self._scroll_ctrl.user_scrolled

    @property
    def new_below_count(self) -> int:
        """Rows of new content below the locked viewport (0 = following tail)."""
        return self._scroll_ctrl.new_below_count

    @property
    def trim_warned(self) -> bool:
        """True when the one-shot ring-buffer-trim warning has been emitted."""
        return self._scroll_ctrl.trim_warned

    @property
    def last_speaker_at(self) -> float:
        """Wall-clock timestamp (``time.time()`` epoch seconds) of the last header write."""
        return self._renderer.last_speaker_at

    @property
    def last_header_date(self) -> str:
        """``YYYY-MM-DD`` string of the last date-separator written, or ``""`` before any message."""
        return self._renderer.last_header_date

    def turn_anchors_snapshot(self) -> tuple[int, ...]:
        """Return a snapshot of the current turn-anchor list as an immutable tuple."""
        return self._scroll_ctrl.turn_anchors_snapshot()

    @property
    def tool_call_row_ids(self) -> frozenset:
        """Frozenset of op_id strings for currently-tracked in-flight tool-call rows.

        Supports membership tests (``op_id in conv.tool_call_row_ids``),
        emptiness checks (``not conv.tool_call_row_ids``), and equality
        (``conv.tool_call_row_ids == frozenset()``). Tests should use this
        rather than accessing ``_tool_call_rows`` directly.
        """
        return frozenset(self._row_mgr._tool_call_rows)

    def richlog_start_line(self, log: "RichLog") -> int:
        """log._start_line (drop-aware counter); one-shot warning on API change."""
        return self._scroll_ctrl.richlog_start_line(log)

    def absolute_line_position(self, log: "RichLog") -> int:
        """Drop-aware absolute write position: richlog_start_line + len(log.lines)."""
        return self._scroll_ctrl.absolute_line_position(log)

    def async_stack_snapshot(self) -> list:
        """Return the AsyncStackPanel's current snapshot list, or ``[]`` if absent.

        Mirrors the ``AsyncStackPanel.snapshot()`` return shape: a list of
        dicts with at minimum ``agent_id``, ``summary``, and ``is_overflow``
        keys. Returns an empty list when the panel is not mounted (= test
        harness path or pre-compose). Tests should call this rather than
        accessing ``_async_stack().snapshot()`` directly.
        """
        panel = self._async_stack()
        if panel is None:
            return []
        return panel.snapshot()

    @property
    def stream_rows(self) -> "dict[str, StreamingRow]":
        """Shallow copy of the in-flight stream-row registry (msg_id → row).

        Tests that need to assert on stream routing (= which msg_ids are
        live, which have been sealed and removed) call this rather than
        accessing ``_stream_rows`` directly — per CLAUDE.md testing
        policy.  Returns a snapshot; do not mutate the returned dict.
        """
        return self._stream_ctrl.stream_rows

    def on_mount(self) -> None:
        """Wire a scroll watcher so user scroll-up suppresses auto-scroll.

        Previously the ``_user_scrolled`` flag existed but nothing ever
        set it: every ``log.write(...)`` snapped the view back to the
        bottom even mid-read. Watching ``scroll_y`` from a single reactive
        callback distinguishes "user is reading old content" (= scroll_y
        below max) from "stream just appended" (= scroll_y already at
        max because Textual's auto_scroll moved it there). The watcher
        only flips ``auto_scroll`` on the boundary crossing, so writes
        during user-read keep their place and writes after user-return
        immediately auto-scroll again.

        Also loads ``show_timestamps`` from ``tui_prefs.json`` so the
        F9 toggle state survives a restart.
        """
        log = self._log()
        try:
            self.watch(log, "scroll_y", self._on_log_scroll_y)
        except Exception:
            # If Textual's cross-widget watch API changes, fail open
            # (= keep historic auto-scroll behaviour) rather than crash mount.
            pass
        try:
            # Content-growth signal for the ``↓ N new`` affordance: while the
            # user is scrolled up, writes don't move ``scroll_y`` (so the
            # scroll watcher above never fires), but they DO grow the log's
            # ``virtual_size``. Watching it is a single chokepoint that
            # covers every write path without instrumenting each one.
            self.watch(log, "virtual_size", self._on_log_content_grew)
        except Exception:
            # Fail open — the indicator is a non-critical affordance; a
            # watch-API change must not break the conv pane.
            pass
        # Load persisted timestamp-toggle state. The app instance holds
        # the project root; ConversationView reaches it defensively via
        # app.app (= the Textual ``app`` property on every widget).
        try:
            from reyn.interfaces.tui.prefs import load_tui_prefs
            root = None
            try:
                root = self.app._project_root_path()  # type: ignore[attr-defined]
            except Exception:
                pass
            prefs = load_tui_prefs(root)
            self._show_timestamps = bool(prefs.get("show_timestamps", True))
        except Exception:
            pass

    def _on_log_scroll_y(self, old: float, new: float) -> None:
        """Textual reactive — delegates to _ScrollController."""
        self._scroll_ctrl.on_log_scroll_y(old, new)

    def _on_log_content_grew(self, old: object, new: object) -> None:
        """Textual reactive — delegates to _ScrollController."""
        self._scroll_ctrl.on_log_content_grew(old, new)

    def on_click(self, event: object) -> None:
        """Click on the ``↓ N new`` strip jumps to the bottom (= Alt+End)."""
        try:
            target_id = getattr(getattr(event, "widget", None), "id", None)
        except Exception:
            target_id = None
        if target_id == "new-below":
            self._scroll_ctrl.snap_to_bottom()

    # ── empty state ───────────────────────────────────────────────────────────

    def _consume_empty_hint(self) -> None:
        self._renderer._consume_empty_hint()

    # ── header grouping (B1) — delegated to _MessageRenderer (tui-pr5) ────────

    def _check_new_turn(self, speaker: str, log: "RichLog") -> bool:
        return self._renderer._check_new_turn(speaker, log)

    def _maybe_write_header(self, speaker: str, symbol: str, name_style: str) -> None:
        self._renderer._maybe_write_header(speaker, symbol, name_style)

    def _maybe_write_inline_header_body(
        self,
        speaker: str,
        symbol: str,
        name_style: str,
        body_text: Text,
        is_new_turn: bool | None = None,
    ) -> None:
        self._renderer._maybe_write_inline_header_body(
            speaker, symbol, name_style, body_text, is_new_turn=is_new_turn,
        )

    # ── non-streaming message rendering — delegated to _MessageRenderer ────────

    def render_message(self, msg: OutboxMessage) -> None:
        self._renderer.render_message(msg)

    def render_user_message(self, text: str) -> None:
        self._renderer.render_user_message(text)

    def _render_agent_markdown(self, msg: OutboxMessage) -> None:
        self._renderer._render_agent_markdown(msg)

    def _render_system_message(self, msg: OutboxMessage) -> None:
        self._renderer._render_system_message(msg)

    def _write_agent_markdown(self, text: str) -> None:
        self._renderer._write_agent_markdown(text)

    def last_reply_text(self) -> str | None:
        return self._renderer.last_reply_text()

    def reply_at(self, n: int) -> str | None:
        return self._renderer.reply_at(n)

    def recent_reply_count(self) -> int:
        return self._renderer.recent_reply_count()

    def find_in_buffer(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
    ) -> list[tuple[int, str]]:
        return self._renderer.find_in_buffer(query, regex=regex, case_sensitive=case_sensitive)

    def dump_buffer_text(self) -> list[str]:
        return self._renderer.dump_buffer_text()

    def _write_log(self, text: Text) -> None:
        self._renderer._write_log(text)

    def _write_body(self, renderable: RenderableType) -> None:
        self._renderer._write_body(renderable)

    # ── streaming support (delegated to _StreamController, tui-pr4) ─────────

    def begin_stream(self, msg_id: str, agent_name: str = "") -> StreamingRow:
        return self._stream_ctrl.begin_stream(msg_id, agent_name)

    def append_stream(self, msg_id: str, text: str) -> None:
        self._stream_ctrl.append_stream(msg_id, text)

    def end_stream(self, msg_id: str) -> str:
        return self._stream_ctrl.end_stream(msg_id)

    def end_stream_cancelled(self, msg_id: str) -> str:
        return self._stream_ctrl.end_stream_cancelled(msg_id)

    # ── skill + tool-call rows (delegated to _InlineRowManager, tui-pr1) ──────

    def start_skill_row(
        self, run_id: str, skill_name: str, *, parent_run_id: str = "",
    ) -> SkillActivityRow:
        return self._row_mgr.start_skill_row(
            run_id, skill_name, parent_run_id=parent_run_id,
        )

    def update_skill_phase(self, run_id: str, phase: str, visit: int = 1) -> None:
        self._row_mgr.update_skill_phase(run_id, phase, visit=visit)

    def update_skill_detail(self, run_id: str, detail: str) -> None:
        self._row_mgr.update_skill_detail(run_id, detail)

    def in_flight_skill_rows(self) -> list[SkillActivityRow]:
        return self._row_mgr.in_flight_skill_rows()

    def finish_skill_row(
        self, run_id: str, *, success: bool = True, reason: str = "", aborted: bool = False,
    ) -> None:
        self._row_mgr.finish_skill_row(run_id, success=success, reason=reason, aborted=aborted)

    def in_flight_tool_call_rows(self) -> list[ToolCallRow]:
        return self._row_mgr.in_flight_tool_call_rows()

    def start_tool_call_row(
        self, op_id: str, tool_name: str, *, args_repr: str = "", parent_run_id: str = "",
    ) -> "ToolCallRow | None":
        return self._row_mgr.start_tool_call_row(
            op_id, tool_name, args_repr=args_repr, parent_run_id=parent_run_id,
        )

    def complete_tool_call_row(self, op_id: str, *, result_snippet: str = "") -> None:
        self._row_mgr.complete_tool_call_row(op_id, result_snippet=result_snippet)

    def fail_tool_call_row(self, op_id: str, *, error: str = "") -> None:
        self._row_mgr.fail_tool_call_row(op_id, error=error)

    def abort_tool_call_rows(self, reason: str = "cancelled") -> int:
        return self._row_mgr.abort_tool_call_rows(reason)

    def latest_failed_tool_row(self) -> "ToolCallRow | None":
        return self._row_mgr.latest_failed_tool_row()

    # ── sticky status (A3) ────────────────────────────────────────────────────

    def show_status(self, text: str, kind: str = "general", *, terminal: bool = False) -> None:
        self._status_ctrl.show_status(text, kind=kind, terminal=terminal)

    def update_status(self, text: str) -> None:
        self._status_ctrl.update_status(text)

    def hide_status(self) -> None:
        self._status_ctrl.hide_status()

    # ── inline thinking spinner (A3b) ─────────────────────────────────────────

    def start_thinking(self) -> None:
        self._status_ctrl.start_thinking()

    def stop_thinking(self) -> None:
        self._status_ctrl.stop_thinking()

    # ── inline error rendering — delegated to _MessageRenderer (tui-pr5) ────────

    def write_error(
        self,
        *,
        message: str,
        details: str = "",
        run_id_short: str = "",
        skill_name: str = "",
        context_lines: list[str] | None = None,
        meta: dict | None = None,
    ) -> None:
        self._renderer.write_error(
            message=message,
            details=details,
            run_id_short=run_id_short,
            skill_name=skill_name,
            context_lines=context_lines,
            meta=meta,
        )

    # ── intervention mounting ─────────────────────────────────────────────────

    def mount_intervention(
        self,
        *,
        question: str,
        choices: list[tuple[str, str] | dict] | None = None,
        answer_callback=None,
        iv_id: str = "",
        queued_extra: int = 0,
        detail: str | None = None,
        source_agent: str | None = None,
    ) -> InterventionWidget:
        self._consume_empty_hint()
        # The run is now blocked on the user's answer; "thinking…" is no
        # longer accurate. When the user is at the tail they'll see the
        # widget on the next render — bare ``hide_status`` suffices.
        # When they're scrolled up, the sticky was their only signal
        # something was happening, and ``hide_status`` would silently
        # erase it. Replace with a "⚑ intervention below ↓" cue so the
        # user knows the run is waiting for them. The natural
        # ``intervention_resolved`` outbox handler (in app_outbox.py)
        # calls ``hide_status`` on both answer paths, so the cue clears
        # itself when the user responds.
        if self._scroll_ctrl.user_scrolled:
            self.show_status("⚑ intervention below ↓", kind="general")
        else:
            self.hide_status()
        widget = InterventionWidget(
            question=question,
            choices=choices,
            answer_callback=answer_callback,
            iv_id=iv_id,
            queued_extra=queued_extra,
            detail=detail,
            source_agent=source_agent,
        )
        self.mount(widget)
        # Only yank the user down to the new widget when they were
        # already at the tail. If they've scrolled up to read history,
        # the sticky just got replaced with "⚑ intervention below ↓"
        # (above) so they have a clear signal the run is waiting; the
        # widget itself becomes visible on their next scroll-down.
        if not self._scroll_ctrl.user_scrolled:
            try:
                widget.scroll_visible()
            except Exception:
                pass
        return widget

    def mount_rewind_menu(
        self,
        tree_rows: list[dict],
        *,
        rel_time_fn=None,
    ) -> "RewindMenuWidget":
        """Mount the inline time-travel fork picker (ADR-0038 2b, always-tree).

        ``tree_rows`` are rows from ``build_branch_tree_rows`` (the only mode
        since #1561; the Phase-1 flat timeline was removed in #1563). The widget
        is passive (``can_focus = False``) — the App drives navigation and
        removes it via ``widget.remove()`` on selection / Esc (decoupled from
        the intervention unmount path). Returns the mounted widget so the App
        can hold a reference for nav + dismiss.
        """
        from .rewind_menu import RewindMenuWidget
        self._consume_empty_hint()
        # Parity with mount_intervention (#1550): when the user has scrolled up
        # to read history, the menu renders at the tail where they can't see it.
        # Surface a "⏪ rewind menu below ↓" cue (same mechanism as the
        # intervention "⚑ … below ↓" hint) so they know the picker is waiting.
        # At the tail, a bare hide_status suffices — the widget is visible.
        if self._scroll_ctrl.user_scrolled:
            self.show_status("⏪ rewind menu below ↓", kind="general")
        else:
            self.hide_status()
        widget = RewindMenuWidget.from_tree_rows(tree_rows, rel_time_fn=rel_time_fn)
        self.mount(widget)
        if not self._scroll_ctrl.user_scrolled:
            try:
                widget.scroll_visible()
            except Exception:
                pass
        return widget

    # ── cost suffix (A4) ──────────────────────────────────────────────────────

    def render_cost_suffix(
        self,
        tokens: int,
        cost_usd: float,
        elapsed_s: float,
        *,
        partial: bool = False,
    ) -> None:
        self._renderer.render_cost_suffix(tokens, cost_usd, elapsed_s, partial=partial)

    # ── turn navigation (B4) ──────────────────────────────────────────────────

    def jump_prev_turn(self) -> None:
        self._scroll_ctrl.jump_prev_turn()

    def jump_next_turn(self) -> None:
        self._scroll_ctrl.jump_next_turn()

    def scroll_page_up(self) -> None:
        self._scroll_ctrl.scroll_page_up()

    def scroll_to_top(self) -> None:
        self._scroll_ctrl.scroll_to_top()

    def scroll_page_down(self) -> None:
        self._scroll_ctrl.scroll_page_down()

    def scroll_line_up(self) -> None:
        self._scroll_ctrl.scroll_line_up()

    def scroll_line_down(self) -> None:
        self._scroll_ctrl.scroll_line_down()

    def scroll_to_bottom(self) -> None:
        self._scroll_ctrl.scroll_to_bottom()

    def clear(self) -> None:
        """Ctrl+L: clear the log + reset state. Does not affect engine state."""
        self._log().clear()
        # Seal + clear in-flight stream rows via controller (tui-pr4).
        self._stream_ctrl.reset_for_clear()
        # Sweep skill + tool-call rows via manager (tui-pr1).
        self._row_mgr.clear()
        # AsyncStackPanel wiring: drop all bottom-stack entries so a
        # Ctrl+L leaves the panel empty alongside the conv log.
        # Without this the running-task overview would survive the
        # clear and look like ghost rows attached to an otherwise-
        # fresh pane. Same pattern as the ToolCallRow sweep directly above.
        self.clear_async_tasks()
        # Wave-10 G-F2: sweep any pending InterventionWidget children too.
        # ``mount_intervention`` adds the widget via ``self.mount(widget)``
        # with no tracking list, so the only way to find them at clear()
        # time is ``self.query(InterventionWidget)``. Without this sweep,
        # a Ctrl+L while an intervention modal is open leaves the chip
        # buttons floating on a blank pane — the user can still "answer"
        # the question, firing the answer_callback against a session
        # context the user just cleared (= acting on stale UI state). The
        # sticky ``⚑ intervention below ↓`` set by ``mount_intervention``
        # is already hidden by the ``hide_status`` call below; this loop
        # removes the visible widget too.
        for widget in list(self.query(InterventionWidget)):
            try:
                widget.remove()
            except Exception:
                pass
        # Reset all renderer state (header-grouping, recent-replies, empty-hint
        # latch) via controller (tui-pr5).
        self._renderer.reset_for_clear()
        # Reset all scroll/nav state via controller (tui-pr3).
        self._scroll_ctrl.reset_for_clear()
        try:
            self._log().auto_scroll = True
        except Exception:
            pass
        # Stop inline spinner and hide sticky status
        self.stop_thinking()
        self.hide_status()
        # Restore the empty-state hint so the next session looks fresh.
        try:
            self.query_one("#empty-hint", Static).remove_class("hidden")
        except Exception:
            pass


# ── formatting helpers ─────────────────────────────────────────────────────────

def _format_message(msg: OutboxMessage) -> Text | None:
    """Convert an OutboxMessage to a Rich Text renderable.

    NOTE: agent / status / error / trace / skill_done / intervention are all
    handled in render_message() with dedicated widgets and never reach here.
    This helper only formats unknown / fallback kinds.
    """
    meta_pfx = _meta_prefix(msg.meta)
    body = f"{meta_pfx}{msg.text}"

    if msg.kind in {"__end__", "__attach_request__", "intervention",
                    "agent", "system", "status", "error", "trace", "skill_done",
                    # Issue #192: hot_list_updated is a data signal (= meta
                    # carries the full ranking); no display copy in the conv
                    # pane. Consumed by the Memory tab augmentation
                    # (tui-coder follow-up).
                    "hot_list_updated"}:
        return None
    # Unknown kind — show raw with subtle prefix
    t = Text()
    t.append(f"[{msg.kind}] ", style="dim")
    t.append(body)
    return t


def _format_intervention_line(msg: OutboxMessage) -> Text:
    """Fallback inline line for intervention when no widget callback set."""
    t = Text()
    # Intervention prefix is agent-identity (= the agent asking a question),
    # so it matches the agent header colour (_AMBER), not the action colour.
    t.append("  Aria asks  ", style="bold " + _AMBER)
    t.append(msg.text, style=_EVENT_INTERVENTION)
    return t
