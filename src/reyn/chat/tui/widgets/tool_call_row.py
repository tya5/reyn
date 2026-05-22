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
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import _CORAL

_TICK_INTERVAL_S = 0.5  # elapsed-time refresh rate

# Match SkillActivityRow's amber / red thresholds so users build one
# mental model for "this is taking a while" across both widgets.
_ELAPSED_AMBER_S = 30.0
_ELAPSED_RED_S = 60.0

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


class ToolCallRow(Widget):
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
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._tool_name = tool_name
        self._args_repr = args_repr
        self._result_snippet: str = ""

        # Running state
        self._start = time.monotonic()
        self._running = True

        # Terminal state
        self._finished = False
        self._success = True
        self._aborted = False
        self._reason: str = ""
        self._frozen_elapsed: float | None = None

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
        self.set_interval(_TICK_INTERVAL_S, self._tick)
        self._refresh()

    # ── Public API ─────────────────────────────────────────────────────────────

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
            return "bold #ff6644"
        if secs >= _ELAPSED_AMBER_S:
            return "bold #ffaa44"
        return "dim"

    def _state_glyph(self) -> tuple[str, str]:
        """Return ``(glyph, style)`` for the current state."""
        if not self._finished:
            return ("●", _CORAL)
        if self._success:
            return ("✓", "bold green")
        if self._aborted:
            return ("⊘", "dim #888888")
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
        """``<glyph> <tool>(<args>)  · <elapsed>`` — terminal-width-adaptive."""
        t = Text()
        glyph, glyph_style = self._state_glyph()
        elapsed = self._elapsed_str()

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
        elapsed_segment = f"  · {elapsed}"
        elapsed_cells = cell_len(elapsed_segment)
        body_budget = max(8, total_width - elapsed_cells - _RIGHT_MARGIN_CELLS)

        # Body = glyph + " " + tool + "(" + args + ")"
        # Truncation order: shrink args first (= most disposable).
        glyph_with_space = f"{glyph} "
        glyph_cells = cell_len(glyph_with_space)
        tool_open = f"{self._tool_name}("
        tool_close = ")"
        tool_open_cells = cell_len(tool_open)
        tool_close_cells = cell_len(tool_close)
        args_budget = max(
            0,
            body_budget - glyph_cells - tool_open_cells - tool_close_cells,
        )
        args_display = self._truncate_to_cells(self._args_repr, args_budget)

        t.append(glyph_with_space, style=glyph_style)
        t.append(tool_open, style="bold" if not self._finished else "dim")
        t.append(args_display, style="dim")
        t.append(tool_close, style="bold" if not self._finished else "dim")
        t.append(elapsed_segment, style=self._elapsed_style())
        return t

    def _build_line2(self) -> Text:
        """``  ⎿ <result snippet>`` — empty Text when result not yet set."""
        if not self._result_snippet:
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
        snippet = self._truncate_to_cells(self._result_snippet, body_budget)
        t = Text()
        t.append(_RESULT_INDENT, style="dim #666666")
        t.append(snippet, style="dim")
        return t

    def _refresh(self) -> None:
        if self._line1 is not None:
            self._line1.update(self._build_line1())
        if self._line2 is not None:
            self._line2.update(self._build_line2())


__all__ = ["ToolCallRow"]
