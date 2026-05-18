"""SkillActivityRow — ambient in-place skill progress widget.

Design:
  During a skill run, one SkillActivityRow replaces the noisy per-phase trace
  lines. It updates in-place as phases advance, showing elapsed time via a
  lightweight 0.5 s set_interval tick. On finish(), the running line transitions
  to a compact completed (✓) or failed (✗) summary and the timer stops.

  Rendering during run::

      ▶ skill_name#abcd  · current_phase  ●●○  2.1s

  Rendering after finish(success=True, reason="3 phases")::

      ✓ skill_name#abcd  · 4.2s · 3 phases            Ctrl+B → agents

  Rendering after finish(success=False, reason="timeout")::

      ✗ skill_name#abcd  · failed: timeout            Ctrl+B → events

Caller contract:
  - Instantiate with run_id + skill_name.
  - Call set_phase() as each OS transition fires.
  - Call set_progress() when step counts are known.
  - Call finish() once on skill completion or abort.
  - No messages are posted; caller owns lifetime and mounting.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import _CORAL

_TICK_INTERVAL_S = 0.5  # elapsed-time refresh rate

# Braille-dot spinner frames. Cycles once every (len(_SPINNER_FRAMES) *
# _TICK_INTERVAL_S) = 5 s, which is slow enough to be calming and fast
# enough to make "still alive" obvious at a glance.
_SPINNER_FRAMES: tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏",
)

# Elapsed thresholds for dynamic colour: green = quick, amber = taking
# a while, red = long-tail / probably blocked. Picked to match a typical
# Reyn skill — most finish under 30 s, anything past 60 s is unusual.
_ELAPSED_AMBER_S = 30.0
_ELAPSED_RED_S = 60.0


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
        label_prefix: str = "",
    ) -> None:
        super().__init__(id=id)
        self._run_id = run_id
        self._skill_name = skill_name
        self._short_id = run_id[:4]
        # ``label_prefix`` (e.g. ``"  └─ "``) renders before the spinner /
        # ✓ glyph so a sub-skill row visibly nests under its parent in
        # the conv pane. Empty (= root skill) keeps the original layout.
        # Parents and children share the same widget — only the prefix
        # differs — so spinner / detail / finish styling stay consistent
        # across the hierarchy.
        self._label_prefix = label_prefix

        # Running state
        self._phase: str = ""
        self._visit: int = 1
        self._progress: tuple[int, int] | None = None  # (current, total)
        # Optional in-phase detail (= what's happening WITHIN the phase right
        # now — "calling llm", "running act op", etc.). Without this the row
        # showed only the phase name during a 10–30 s LLM call inside that
        # phase, with no signal whether the skill was making progress or
        # stuck. The detail is replaced each event and cleared on phase
        # change (= the new phase's detail context starts fresh).
        self._detail: str = ""

        # Finish state
        self._finished = False
        self._success = True
        self._reason = ""

        # Timer
        self._start = time.monotonic()
        self._running = True
        # Spinner tick — increments on every _tick(), modulo wrapped to
        # cycle through `_SPINNER_FRAMES` for the running indicator.
        self._spin_idx: int = 0

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
        """Update the currently active phase name (and visit count).

        Also clears any in-phase detail — the previous phase's "llm:
        <model>" / "act: <op>" context is no longer relevant once the
        phase advances.
        """
        self._phase = phase
        self._visit = visit
        self._detail = ""
        self._refresh()

    def set_detail(self, detail: str) -> None:
        """Update the in-phase detail text shown after the elapsed counter.

        Detail is ephemeral: each call replaces the previous text, and
        ``set_phase`` clears it on phase advance. Empty string hides
        the detail segment. Typical sources: forwarder ``on_llm_called``
        (= ``"llm: <model>"``) or ``on_act_executed``
        (= ``"act: <N> ops"``).
        """
        self._detail = detail
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
        self._spin_idx = (self._spin_idx + 1) % len(_SPINNER_FRAMES)
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
        if self._label_prefix:
            t.append(self._label_prefix, style="dim #666666")
        # Animated braille spinner — replaces the static ▶ so "still
        # alive" is obvious even when the response was streamed and the
        # user is wondering "is this skill still doing things?".
        spinner = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
        t.append(f"{spinner} ", style=_CORAL)
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
        # Elapsed — colour-coded so a slow / stuck skill stands out:
        #   < 30 s → dim (= normal)
        #   30–60s → amber (= "taking a while")
        #   ≥ 60 s → red (= "this is unusual; might be blocked")
        secs = time.monotonic() - self._start
        if secs >= _ELAPSED_RED_S:
            elapsed_style = "bold #ff6644"
        elif secs >= _ELAPSED_AMBER_S:
            elapsed_style = "bold #ffaa44"
        else:
            elapsed_style = "dim"
        t.append(f"  {secs:.1f}s", style=elapsed_style)
        # In-phase detail ("llm: opus-4-5", "act: 3 ops", etc.). Dim so
        # it doesn't compete with the phase name, separated from elapsed
        # by ``  ⤷`` so the eye can grok "this is the inner-most thing
        # happening right now".
        if self._detail:
            t.append("  ⤷ ", style="dim")
            t.append(self._detail, style="dim")
        return t

    def _build_finished(self) -> Text:
        t = Text()
        if self._label_prefix:
            t.append(self._label_prefix, style="dim #666666")
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
            # Use the same dot separator as the rest of the row rather
            # than a fixed 12-space gap — the gap pushed the line past
            # the conv pane width (~70 cols with the right panel open),
            # causing "Ctrl+B → agents" to wrap to a second line as an
            # orphan hint. `B` alone isn't bound — Ctrl+B is the real
            # panel-toggle binding, and the panel remembers its last
            # focal tab so it lands on agents/events automatically.
            t.append("  · ", style="dim")
            t.append("Ctrl+B → agents", style="dim")
        else:
            t.append("✗ ", style="bold red")
            t.append(
                f"{self._skill_name}#{self._short_id}",
                style="dim",
            )
            t.append("  · ", style="dim")
            failed_msg = f"failed: {self._reason}" if self._reason else "failed"
            t.append(failed_msg, style="dim")
            t.append("  · ", style="dim")
            t.append("Ctrl+B → events", style="dim")
        return t

    def _refresh(self) -> None:
        if self._static is None:
            return
        if self._finished:
            self._static.update(self._build_finished())
        else:
            self._static.update(self._build_running())
