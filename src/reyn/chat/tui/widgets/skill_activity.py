"""SkillActivityRow — ambient in-place skill progress widget.

Design:
  During a skill run, one SkillActivityRow replaces the noisy per-phase trace
  lines. It updates in-place as phases advance, showing elapsed time via a
  lightweight 0.5 s set_interval tick. On finish(), the running line transitions
  to a compact completed (✓) or failed (✗) summary and the timer stops.

  Rendering during run::

      ▶ skill_name#abcd  · current_phase  ●●○  2.1s

  Rendering after finish(success=True, reason="3 phases")::

      ✓ skill_name#abcd  · 4.2s · 3 phases            [B→agents]

  Rendering after finish(success=False, reason="timeout")::

      ✗ skill_name#abcd  · failed: timeout            [B→events]

Caller contract:
  - Instantiate with run_id + skill_name.
  - Call set_phase() as each OS transition fires.
  - Call set_progress() when step counts are known.
  - Call finish() once on skill completion or abort.
  - No messages are posted; caller owns lifetime and mounting.
"""
from __future__ import annotations

_CORAL = "#C8553D"  # primary theme colour — matches Theme(primary=...)

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

_TICK_INTERVAL_S = 0.5  # elapsed-time refresh rate


class SkillActivityRow(Widget):
    """One ambient skill-progress row that updates in-place.

    Transitions from a live running line to a compact completion line on
    finish(). The elapsed-time counter ticks every 0.5 s via set_interval
    and stops as soon as finish() is called.
    """

    DEFAULT_CSS = """
    SkillActivityRow {
        height: auto;
        padding: 0 0;
    }
    SkillActivityRow Static {
        height: auto;
        padding: 0 0;
    }
    """

    def __init__(
        self,
        *,
        run_id: str,
        skill_name: str,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._run_id = run_id
        self._skill_name = skill_name
        self._short_id = run_id[:4]

        # Running state
        self._phase: str = ""
        self._visit: int = 1
        self._progress: tuple[int, int] | None = None  # (current, total)

        # Finish state
        self._finished = False
        self._success = True
        self._reason = ""

        # Timer
        self._start = time.monotonic()
        self._running = True

        # DOM ref
        self._static: Static | None = None

    # ── Textual lifecycle ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        self._static = Static(id="skill_activity_text")
        yield self._static

    def on_mount(self) -> None:
        self.set_interval(_TICK_INTERVAL_S, self._tick)
        self._refresh()

    # ── Public API ─────────────────────────────────────────────────────────────

    def set_phase(self, phase: str, visit: int = 1) -> None:
        """Update the currently active phase name (and visit count)."""
        self._phase = phase
        self._visit = visit
        self._refresh()

    def set_progress(self, current: int, total: int) -> None:
        """Set progress dot counts (e.g. 2 of 3 steps done)."""
        self._progress = (current, total)
        self._refresh()

    def finish(self, success: bool = True, reason: str = "") -> None:
        """Transition to the completed line and stop the timer."""
        if self._finished:
            return
        self._finished = True
        self._running = False
        self._success = success
        self._reason = reason
        self._refresh()

    # ── Internal rendering ─────────────────────────────────────────────────────

    def _tick(self) -> None:
        """Called every 0.5 s by Textual. Stops once _running is False."""
        if not self._running:
            return
        self._refresh()

    def _elapsed(self) -> str:
        """Format elapsed seconds as e.g. '2.1s'."""
        secs = time.monotonic() - self._start
        return f"{secs:.1f}s"

    def _dots(self) -> str:
        """Build progress dots string, e.g. '●●○'."""
        if self._progress is None:
            return ""
        current, total = self._progress
        if total <= 0:
            return ""
        filled = min(current, total)
        empty = total - filled
        return "●" * filled + "○" * empty

    def _build_running(self) -> Text:
        t = Text()
        # ▶ coral
        t.append("▶ ", style=_CORAL)
        # skill_name#abcd — normal weight
        t.append(f"{self._skill_name}#{self._short_id}", style="bold")
        t.append("  · ", style="dim")
        # phase — italic coral
        if self._phase:
            t.append(self._phase, style=f"italic {_CORAL}")
            if self._visit > 1:
                t.append(f" v{self._visit}", style="dim")
        # progress dots
        dots = self._dots()
        if dots:
            t.append(f"  {dots}", style="dim")
        # elapsed
        t.append(f"  {self._elapsed()}", style="dim")
        return t

    def _build_finished(self) -> Text:
        t = Text()
        if self._success:
            t.append("✓ ", style="bold green")
            t.append(
                f"{self._skill_name}#{self._short_id}",
                style="dim",
            )
            t.append("  · ", style="dim")
            t.append(self._elapsed(), style="dim")
            if self._reason:
                t.append(f" · {self._reason}", style="dim")
            # Pad and append [B→agents]
            t.append("            ", style="")
            t.append("[B→agents]", style="dim")
        else:
            t.append("✗ ", style="bold red")
            t.append(
                f"{self._skill_name}#{self._short_id}",
                style="dim",
            )
            t.append("  · ", style="dim")
            failed_msg = f"failed: {self._reason}" if self._reason else "failed"
            t.append(failed_msg, style="dim")
            t.append("            ", style="")
            t.append("[B→events]", style="dim")
        return t

    def _refresh(self) -> None:
        if self._static is None:
            return
        if self._finished:
            self._static.update(self._build_finished())
        else:
            self._static.update(self._build_running())
