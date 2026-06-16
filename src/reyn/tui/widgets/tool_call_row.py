"""ToolCallRow — per tool_call inline widget for the conv pane (issue #427).

PoC stage: widget shape + state transitions, no production wiring yet.
A follow-up PR will:
- emit per-op events from op_runtime (= ``tool_called`` / ``tool_completed``)
- subscribe ChatLifecycleForwarder to those events
- mount + drive ToolCallRow from the chat session

Spec (= issue #427 L2):
- 2 lines fixed, no wrap, terminal-width-adaptive truncation
- Line 1: ``<state-glyph> <tool>(<args>)  · <elapsed>``
- Line 2: ``  ⎿ <result snippet>``  (= only shown when result is present)
- State transitions: running (●) → terminal (✓ / ✗ / ⊘) — frozen after
- Elapsed updates while running, frozen at terminal time

Caller contract:
- Instantiate with ``tool_name`` + ``args_repr``.
- Optionally call ``set_result(snippet)`` as preview data arrives.
- Call ``finish_success()`` / ``finish_failure(reason)`` / ``finish_aborted(reason)``
  exactly once on terminal state. After that, the widget is frozen and the
  timer stops.

Per ``feedback-tui-visibility-axis``: each ToolCallRow is a permanent chat
event (= history-side), distinct from ephemeral runtime state (= phase /
plan / current skill) which lives in the bottom strip / right panel.
"""
from __future__ import annotations

import time

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.tui._palette import _CORAL, _STATUS_ERROR, _STATUS_WARN, _TEXT_MUTED, _TEXT_NEUTRAL

from ._renderable_cache import RenderableCacheMixin

_TICK_INTERVAL_S = 0.5  # elapsed-time refresh rate

# Match SkillActivityRow's amber / red thresholds so users build one
# mental model for "this is taking a while" across both widgets.
_ELAPSED_AMBER_S = 30.0
_ELAPSED_RED_S = 60.0

# Below this threshold the terminal-state row hides the elapsed segment
# entirely (= `· 0.0s` was noise on fast file_read / cache hit / 等).
# Still-running rows always show elapsed so the user has a "this is
# alive" signal regardless of duration. Threshold is roughly the
# tick interval — below it, `0.0s` is just rounding artifact.
_ELAPSED_HIDE_THRESHOLD_S = 0.1

# Indent column for the result line (= ``  ⎿ ``). Two spaces aligns the
# result under the tool-name column of line 1 in the typical Latin glyph
# width; CJK / emoji shift this but ⎿ is unambiguous-narrow so the
# anchor stays readable.
_RESULT_INDENT = "  ⎿ "

# Reserve cells on the right edge so the elapsed-time segment doesn't
# clip behind the scrollbar / RichLog right-padding at 80-col widths.
# Empirically the same 6-cell budget the cost-suffix uses (= conv pane
# render_cost_suffix). Conservative — overrunning here would silently
# eat the seconds digit.
_RIGHT_MARGIN_CELLS = 6


def _maybe_middle_elide(name: str, max_cells: int) -> str:
    """Middle-elide a qualified tool name so head + tail stay visible.

    For ``mcp__server__tool_name`` with budget 18, returns
    ``mcp__…__tool_name``. The first and last segments carry the most
    identity signal (= provider prefix + verb); middle segments
    (= server / namespace) are the disposable part.

    Falls back to plain tail-truncate when:
    - name doesn't contain ``__`` (= no qualified shape)
    - name has fewer than 3 segments (= no middle to elide)
    - even the elided form exceeds the budget (= rare; budget very tight)
    """
    if cell_len(name) <= max_cells:
        return name
    sep = "__"
    if sep not in name:
        return name  # caller falls through to its own truncation
    parts = name.split(sep)
    if len(parts) < 3:
        return name
    elided = f"{parts[0]}{sep}…{sep}{parts[-1]}"
    if cell_len(elided) <= max_cells:
        return elided
    # Even compressed form too long — give up, let caller's tail-truncate
    # (= cell-aware ellipsis on the full name) take over.
    return name


class ToolCallRow(RenderableCacheMixin, Widget):
    """One inline tool-call row that updates in-place until terminal state.

    State machine:
        running  → success (finish_success)
        running  → failure (finish_failure)
        running  → aborted (finish_aborted)

    After any terminal call the widget freezes — further state changes are
    ignored. The internal elapsed timer stops on terminal so completed
    rows don't keep redrawing.
    """

    DEFAULT_CSS = """
    ToolCallRow {
        height: auto;
        padding: 0 0;
    }
    """

    def __init__(
        self,
        *,
        tool_name: str,
        args_repr: str = "",
        label_prefix: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._tool_name = tool_name
        self._args_repr = args_repr
        self._result_snippet: str = ""
        # F-F: when this tool_call originated from a sub-skill whose
        # SkillActivityRow is currently mounted, the conv pane passes
        # ``label_prefix="  └─ "`` so the inline rows visibly nest
        # under the parent skill row — same idiom as SkillActivityRow's
        # own ``label_prefix`` (issue #210 sub-skill nesting). Empty
        # for root-level tool_calls.
        self._label_prefix = label_prefix

        # Running state
        self._start = time.monotonic()
        self._mount_time = self._start  # updated in on_mount when widget composes
        self._running = True

        # Terminal state
        self._finished = False
        self._success = True
        self._aborted = False
        self._reason: str = ""
        self._frozen_elapsed: float | None = None

        # Expand state — when True, line 1 + line 2 render the full
        # args / result content without cell-budget truncation.
        # Long args / result get word-wrapped across multiple visual
        # lines by Static (= ``height: auto`` lets the row grow).
        # Toggled by mouse click on the row (see ``on_click``) or
        # the public ``toggle_expand`` method. Preserves across the
        # running → finished transition so the user can still drill
        # into a completed call's full args / result.
        self._expanded: bool = False

        # DOM refs — populated in compose()
        self._line1: Static | None = None
        self._line2: Static | None = None

    # ── Textual lifecycle ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._line1 = Static(id="tool_call_line1")
        self._line2 = Static(id="tool_call_line2")
        yield self._line1
        yield self._line2

    def on_mount(self) -> None:
        self._mount_time = time.monotonic()
        self.set_interval(_TICK_INTERVAL_S, self._tick)
        self._refresh()

    def mounted_for_seconds(self) -> float:
        """Wall-time elapsed since the widget actually mounted (= on_mount).

        Used by ``ConversationView._flush_tool_call_row`` to enforce a
        minimum visible duration: very fast ops (= cache hit, instant
        return) would otherwise mount + flush within the same event-loop
        tick, leaving the user with no perceptual cue that the tool was
        called. The caller defers the flush until this exceeds the
        configured minimum so each row is briefly perceivable.
        """
        return max(0.0, time.monotonic() - self._mount_time)

    # ── Public API ─────────────────────────────────────────────────────────────

    @property
    def finished(self) -> bool:
        """True once any ``finish_*`` method has been called (= terminal state)."""
        return self._finished

    @property
    def success(self) -> bool:
        """True when the terminal state is success (= ``finish_success`` was called).

        Only meaningful after ``finished`` is True; undefined while still running.
        """
        return self._success

    def render_line1(self) -> Text:
        """Return the current line-1 Rich Text for the tool-call row.

        Delegates to ``_build_line1()``. Provided as a public accessor so
        tests can assert on the rendered content via the public surface
        (= CLAUDE.md "NEVER assert on private state").
        """
        return self._build_line1()

    def render_line2(self) -> Text:
        """Return the current line-2 Rich Text for the result / reason row.

        Delegates to ``_build_line2()``. Provided as a public accessor so
        tests can assert on the rendered content via the public surface.
        """
        return self._build_line2()

    def toggle_expand(self) -> None:
        """Flip the collapsed / expanded render shape.

        Expanded form drops cell-budget truncation on both lines so
        long args / result wrap to multiple visual lines (Static
        handles wrap; ``height: auto`` lets the row grow). Useful
        for tool calls whose args / result got snipped to ``…`` in
        the default 80-cell view.
        """
        self._expanded = not self._expanded
        self._refresh()

    @property
    def is_expanded(self) -> bool:
        """True when the row is currently rendering the drill-down view."""
        return self._expanded

    def on_click(self, event: events.Click) -> None:
        """Mouse-click anywhere on the row toggles the drill-down view.

        ``event.stop()`` so the click doesn't bubble up to conv-pane
        scroll handling. Mirrors the SkillActivityRow click contract
        from PR #546 — same trigger UX for two adjacent inline
        widgets that the user reads together.
        """
        event.stop()
        self.toggle_expand()

    def set_result(self, snippet: str) -> None:
        """Update the result snippet shown on line 2.

        Called either while running (= incremental preview) or just before
        finish() (= final result preview). Empty string hides line 2.
        Ignored after terminal state.
        """
        if self._finished:
            return
        self._result_snippet = snippet
        self._refresh()

    def finish_success(self, result_snippet: str | None = None) -> None:
        """Transition to the success terminal state and stop the timer.

        If ``result_snippet`` is provided, it replaces any pending result.
        """
        self._enter_terminal(success=True, reason="", result_snippet=result_snippet)

    def finish_failure(self, reason: str = "", result_snippet: str | None = None) -> None:
        """Transition to the failure terminal state and stop the timer."""
        self._enter_terminal(success=False, reason=reason, result_snippet=result_snippet)

    def finish_aborted(self, reason: str = "") -> None:
        """Transition to the aborted terminal state and stop the timer."""
        self._enter_terminal(
            success=False,
            reason=reason or "aborted",
            result_snippet=None,
            aborted=True,
        )

    # ── Internal rendering ─────────────────────────────────────────────────────

    def _enter_terminal(
        self,
        *,
        success: bool,
        reason: str,
        result_snippet: str | None,
        aborted: bool = False,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self._running = False
        self._success = success
        self._reason = reason
        self._aborted = aborted
        self._frozen_elapsed = time.monotonic() - self._start
        if result_snippet is not None:
            self._result_snippet = result_snippet
        self._refresh()

    def _tick(self) -> None:
        if not self._running:
            return
        self._refresh()

    def _elapsed_s(self) -> float:
        if self._frozen_elapsed is not None:
            return self._frozen_elapsed
        return time.monotonic() - self._start

    def _elapsed_str(self) -> str:
        return f"{self._elapsed_s():.1f}s"

    def _elapsed_style(self) -> str:
        secs = self._elapsed_s()
        if secs >= _ELAPSED_RED_S:
            return "bold " + _STATUS_ERROR
        if secs >= _ELAPSED_AMBER_S:
            return "bold " + _STATUS_WARN
        return "dim"

    def _state_glyph(self) -> tuple[str, str]:
        """Return ``(glyph, style)`` for the current state."""
        if not self._finished:
            return ("●", _CORAL)
        if self._success:
            return ("✓", "bold green")
        if self._aborted:
            return ("⊘", "dim " + _TEXT_MUTED)
        return ("✗", "bold red")

    def _truncate_to_cells(self, text: str, max_cells: int) -> str:
        """Truncate ``text`` to fit within ``max_cells`` display cells.

        Cell-aware so CJK / emoji count as 2 cells per glyph. Appends an
        ellipsis when truncation happened. Reserves the ellipsis cell so
        the result fits within the budget.
        """
        if max_cells <= 0:
            return ""
        if cell_len(text) <= max_cells:
            return text
        ellipsis = "…"
        budget = max_cells - cell_len(ellipsis)
        if budget <= 0:
            return ellipsis
        out: list[str] = []
        used = 0
        for ch in text:
            w = cell_len(ch)
            if used + w > budget:
                break
            out.append(ch)
            used += w
        return "".join(out) + ellipsis

    def _build_line1(self) -> Text:
        """``<glyph> <tool>(<args>)  · <elapsed>`` — terminal-width-adaptive.

        Elapsed segment is hidden when the row is in a terminal state AND
        elapsed < ``_ELAPSED_HIDE_THRESHOLD_S`` (= ``0.0s`` is noise on
        sub-100ms ops). Still-running rows always show elapsed so the
        user has a "this is alive" signal regardless of duration.
        """
        t = Text()
        glyph, glyph_style = self._state_glyph()

        # Compute right-edge reservation: elapsed segment + `  · ` prefix
        # + right margin. The body (= glyph + tool + args) fills whatever
        # remains.
        try:
            total_width = int(getattr(self.size, "width", 0))
        except Exception:
            total_width = 0
        if total_width <= 0:
            # Pre-mount (= self.size.width is 0) or measurement failure
            # — fall back to a typical 80-cell terminal so the truncation
            # arithmetic produces sensible budgets instead of zeroing the
            # args field.
            total_width = 80
        if self._finished and self._elapsed_s() < _ELAPSED_HIDE_THRESHOLD_S:
            elapsed_segment = ""
        else:
            elapsed_segment = f"  · {self._elapsed_str()}"
        elapsed_cells = cell_len(elapsed_segment)
        body_budget = max(8, total_width - elapsed_cells - _RIGHT_MARGIN_CELLS)

        # Body = glyph + " " + tool + "(" + args + ")"
        # Truncation order: shrink args first (= most disposable).
        glyph_with_space = f"{glyph} "
        glyph_cells = cell_len(glyph_with_space)
        # F-E: when the tool name itself is so long that args have no
        # room to render, middle-elide qualified names
        # (``mcp__server__tool_name`` → ``mcp__…__tool_name``) so args
        # still get a usable budget. Plain names without ``__`` fall
        # through to the existing args-first truncation.
        framing_cells = glyph_cells + 2  # 2 = "(" + ")"
        # Reserve at least 8 cells for args so very long tool names
        # trigger middle-elide before args get fully ejected.
        max_tool_budget = max(0, body_budget - framing_cells - 8)
        tool_display = self._tool_name
        if max_tool_budget > 0 and cell_len(tool_display) > max_tool_budget:
            tool_display = _maybe_middle_elide(tool_display, max_tool_budget)
        tool_open = f"{tool_display}("
        tool_close = ")"
        tool_open_cells = cell_len(tool_open)
        tool_close_cells = cell_len(tool_close)
        args_budget = max(
            0,
            body_budget - glyph_cells - tool_open_cells - tool_close_cells,
        )
        args_display = self._truncate_to_cells(self._args_repr, args_budget)

        # Expand mode: drop the cell-budget cap on args so the full
        # repr surfaces. Static wraps the line to fit; ``height: auto``
        # grows the row. Tool name stays un-elided too — when
        # drilling in, the user wants the qualified name in full.
        if self._expanded:
            tool_display = self._tool_name
            tool_open = f"{tool_display}("
            tool_close = ")"
            args_display = self._args_repr

        if self._label_prefix:
            t.append(self._label_prefix, style="dim " + _TEXT_NEUTRAL)
        t.append(glyph_with_space, style=glyph_style)
        t.append(tool_open, style="bold" if not self._finished else "dim")
        t.append(args_display, style="dim")
        t.append(tool_close, style="bold" if not self._finished else "dim")
        t.append(elapsed_segment, style=self._elapsed_style())
        return t

    def _build_line2(self) -> Text:
        """``  ⎿ <result snippet>`` or ``  ⎿ <error reason>`` (terminal-failed).

        Line-2 content priority:
          1. ``_result_snippet`` (= success / explicit body)
          2. ``_reason`` (= failure / abort cause) when terminal state
             is non-success — surfaces *why* the tool call failed so
             the user doesn't have to switch to the events tab. Same
             ``  ⎿ `` indent as success, but ``✗ <reason>`` body
             prefixed with the failure glyph + styled in dim red /
             dim grey (= aborted) so the visual cue carries across
             both line 1 and line 2.
          3. Nothing (= still running, or terminal-success with empty
             result preview)
        """
        body, body_style = self._line2_body_and_style()
        if not body:
            return Text("")
        try:
            total_width = int(getattr(self.size, "width", 0))
        except Exception:
            total_width = 0
        if total_width <= 0:
            # Pre-mount (= self.size.width is 0) or measurement failure
            # — fall back to a typical 80-cell terminal so the truncation
            # arithmetic produces sensible budgets instead of zeroing the
            # args field.
            total_width = 80
        body_budget = max(
            8, total_width - cell_len(_RESULT_INDENT) - _RIGHT_MARGIN_CELLS,
        )
        # Expand mode: surface the full body without cell-budget
        # truncation. Static wraps; row grows via height: auto.
        snippet = body if self._expanded else self._truncate_to_cells(
            body, body_budget,
        )
        t = Text()
        t.append(_RESULT_INDENT, style="dim " + _TEXT_NEUTRAL)
        t.append(snippet, style=body_style)
        return t

    def _line2_body_and_style(self) -> tuple[str, str]:
        """Decide what (if anything) to show on line 2 + its style.

        Priority: result_snippet wins (= explicit positive body) over
        the failure reason fallback. For aborted state, prefix the
        reason with the ⊘ glyph so the line carries the same shape
        cue as line 1's state-glyph.
        """
        if self._result_snippet:
            return self._result_snippet, "dim"
        if self._finished and not self._success and self._reason:
            if self._aborted:
                return f"⊘ {self._reason}", "dim " + _TEXT_MUTED
            return f"✗ {self._reason}", "dim " + _STATUS_ERROR
        return "", "dim"

    def _refresh(self) -> None:
        l1 = self._build_line1()
        l2 = self._build_line2()
        if self._line1 is not None:
            self._line1.update(l1)
        if self._line2 is not None:
            self._line2.update(l2)
        # Concatenate for the renderable-cache mixin — tests read
        # both lines via ``rendered_text()`` as a single string. Line
        # 2 is often empty (= running with no result yet); join with
        # a newline only when both have content.
        combined = Text()
        combined.append_text(l1)
        if l2.plain:
            combined.append("\n")
            combined.append_text(l2)
        self._set_rendered_cache(combined)


__all__ = ["ToolCallRow"]
