"""SkillActivityRow — ambient in-place skill progress widget.

Design:
  During a skill run, one SkillActivityRow replaces the noisy per-phase trace
  lines. It updates in-place as phases advance, showing elapsed time via a
  lightweight 0.5 s set_interval tick. On finish(), the running line transitions
  to a compact completed (✓) or failed (✗) summary and the timer stops.

  Rendering during run::

      ▶ skill_name#abcd  · current_phase  2.1s

  Rendering after finish(success=True, reason="3 phases")::

      ✓ skill_name#abcd  · 4.2s · 3 phases            Ctrl+B → agents

  Rendering after finish(success=False, reason="timeout")::

      ✗ skill_name#abcd  · failed: timeout            Ctrl+B → events

Caller contract:
  - Instantiate with run_id + skill_name.
  - Call set_phase() as each OS transition fires.
  - Call finish() once on skill completion or abort.
  - No messages are posted; caller owns lifetime and mounting.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual import events
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
        # Phase history — each entry is ``(phase, visit, elapsed_at_entry)``
        # recorded on every NEW phase transition via ``set_phase``. Powers
        # the inline expand drill-down (= click the row to see "what phases
        # has this skill been through and how long did each take?"). Stays
        # empty until ``set_phase`` is first called, then grows by one per
        # transition. Re-visits to the same phase (= ``v2`` / ``v3`` in
        # the collapsed view) are recorded as separate entries with the
        # incremented visit number so the user can see "we looped back to
        # research three times". Bound is the natural skill phase count —
        # typical Reyn skills have < 10 transitions; no explicit cap.
        self._phase_history: list[tuple[str, int, float]] = []
        # Optional in-phase detail (= what's happening WITHIN the phase right
        # now — "calling llm", "running act op", etc.). Without this the row
        # showed only the phase name during a 10–30 s LLM call inside that
        # phase, with no signal whether the skill was making progress or
        # stuck. The detail is replaced each event and cleared on phase
        # change (= the new phase's detail context starts fresh).
        self._detail: str = ""
        # Persistent plan-step badge (e.g. "plan 2/5") for sub-skills
        # spawned by a planner. The forwarder emits the badge once per
        # run_id via ``set_detail`` with a "plan N/M" payload; on first
        # arrival we extract it into ``_plan_step_label`` so it survives
        # subsequent ``set_detail`` (llm:/act:) and ``set_phase`` calls.
        # Without this, the plan context was visible for the first
        # in-phase signal only and then silently disappeared.
        self._plan_step_label: str = ""

        # Finish state
        self._finished = False
        self._success = True
        # C-F5 (wave-8): distinguishes user-initiated cancellation from
        # system failure. When ``True``, ``_build_finished`` renders the
        # ⊘ glyph in dim grey instead of the ✗ glyph in red. Set by
        # ``finish(..., aborted=True)`` from ``action_cancel_inflight``.
        self._aborted = False
        self._reason = ""

        # Expand state — when True, ``_refresh`` renders multi-line with
        # the full phase history; when False, the single-line collapsed
        # form. Toggled by mouse click on the row (see ``on_click``) or
        # the public ``toggle_expand`` method. Preserved across
        # ``finish()`` so the user can still drill down on a completed
        # skill to see its phase trajectory.
        self._expanded: bool = False

        # Timer
        self._start = time.monotonic()
        self._running = True
        # Spinner tick — increments on every _tick(), modulo wrapped to
        # cycle through `_SPINNER_FRAMES` for the running indicator.
        self._spin_idx: int = 0

        # DOM ref
        self._static: Static | None = None
        # Cached copy of the most-recently-rendered Text (= what's
        # currently displayed). Updated on every ``_refresh``. Read
        # by ``rendered_text`` for inspection — used by Tier 2 tests
        # to assert on the visible content without reaching into
        # Textual's private Static internals (= ``static._renderable``
        # is API-unstable across versions).
        self._rendered_cache: Text | None = None

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

        Each NEW (phase, visit) pair is appended to ``_phase_history``
        for the drill-down expand view. Duplicates of the immediately-
        previous (phase, visit) are suppressed so a stream of repeat
        ``set_phase("execute", 1)`` calls (= forwarder noise) doesn't
        bloat the history. Genuine re-visits (= same phase, higher
        visit count) are recorded because they're meaningful: "we
        looped back to research a 2nd time".
        """
        self._phase = phase
        self._visit = visit
        self._detail = ""
        if not self._phase_history or self._phase_history[-1][:2] != (phase, visit):
            elapsed = time.monotonic() - self._start
            self._phase_history.append((phase, visit, elapsed))
        self._refresh()

    def set_detail(self, detail: str) -> None:
        """Update the in-phase detail text shown after the elapsed counter.

        Detail is ephemeral: each call replaces the previous text, and
        ``set_phase`` clears it on phase advance. Empty string hides
        the detail segment. Typical sources: forwarder ``on_llm_called``
        (= ``"llm: <model>"``) or ``on_act_executed``
        (= ``"act: <N> ops"``).

        ``"plan N/M"`` is a special case: the forwarder emits it once
        per sub-skill mount to communicate plan-step attribution. We
        route it into the persistent ``_plan_step_label`` slot instead
        of the ephemeral ``_detail`` so it survives the next in-phase
        signal and the next ``set_phase`` call.
        """
        if detail.startswith("plan ") and "/" in detail:
            self._plan_step_label = detail
            self._refresh()
            return
        self._detail = detail
        self._refresh()

    def toggle_expand(self) -> None:
        """Flip the collapsed / expanded render shape.

        Expanded form appends a second line listing the phase history
        (= each prior phase + its entry-elapsed) under the normal
        running / finished summary. Useful for skills with a non-
        trivial phase trajectory the user wants to drill into without
        switching to the right panel.
        """
        self._expanded = not self._expanded
        self._refresh()

    @property
    def is_expanded(self) -> bool:
        """True when the row is currently rendering the drill-down view."""
        return self._expanded

    def on_click(self, event: events.Click) -> None:
        """Mouse-click anywhere on the row toggles the drill-down view.

        Stop-propagation so the click doesn't bubble up to the conv
        pane's scroll handling and inadvertently move the viewport.
        """
        event.stop()
        self.toggle_expand()

    def finish(
        self,
        success: bool = True,
        reason: str = "",
        *,
        aborted: bool = False,
    ) -> None:
        """Transition to the completed line and stop the timer.

        ``aborted=True`` (C-F5, wave-8) marks the finish as user-initiated
        cancellation rather than a system failure — renders as ``⊘`` in
        dim grey instead of ``✗`` in red. Use ``aborted=True`` from
        ``action_cancel_inflight``; leave it False for failures coming
        from workflow_aborted / exception paths so the user can tell
        "I cancelled" from "the system failed".
        """
        if self._finished:
            return
        self._finished = True
        self._running = False
        self._success = success
        self._aborted = aborted
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
        # Persistent plan-step badge — appears between elapsed and detail
        # so the user always knows "this skill is plan step 2/5" even after
        # the in-phase detail has been overwritten by the next llm: / act:
        # signal or cleared by a phase advance.
        if self._plan_step_label:
            t.append("  [", style="dim")
            t.append(self._plan_step_label, style=f"dim {_CORAL}")
            t.append("]", style="dim")
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
        elif self._aborted:
            # C-F5 (wave-8): user-initiated cancellation gets the ⊘
            # glyph in dim grey, distinguishing intent from a system
            # failure. Same shape design as ToolCallRow's aborted
            # state — color + glyph as a redundant cue.
            t.append("⊘ ", style="dim #888888")
            t.append(
                f"{self._skill_name}#{self._short_id}",
                style="dim",
            )
            t.append("  · ", style="dim")
            cancel_msg = (
                f"cancelled: {self._reason}" if self._reason else "cancelled"
            )
            t.append(cancel_msg, style="dim")
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

    def _build_history_line(self) -> Text:
        """Render the phase-history drill-down line shown when expanded.

        Format:
          ``  ↳ phases: plan(0.5s) → research(1.4s) → reviewing*(now)``

        The trailing entry is the current phase (if running) marked with
        ``*`` and ``(now)``; everything before it shows the elapsed-at-
        entry timestamp so the user can see "this skill spent 1.4 s in
        research before moving on". Re-visits to the same phase appear
        as separate entries with their visit number (= ``research v2``)
        so loop-backs are visible.
        """
        t = Text()
        if self._label_prefix:
            # Indent the history line under the same prefix so a sub-
            # skill's drill-down nests visually with its parent.
            t.append(" " * len(self._label_prefix), style="dim #666666")
        t.append("  ↳ phases: ", style="dim")
        if not self._phase_history:
            t.append("(none yet)", style="dim #666666")
            return t
        # The history is "(phase, visit, elapsed_at_entry)" for each
        # entry. Render each as "name(elapsed_at_entry)", with the
        # current (= still-running) phase getting a different suffix.
        for i, (phase, visit, entered_at) in enumerate(self._phase_history):
            if i > 0:
                t.append(" → ", style="dim")
            label = phase if visit == 1 else f"{phase} v{visit}"
            is_current = (
                not self._finished
                and (phase, visit) == (self._phase, self._visit)
            )
            if is_current:
                t.append(label, style=f"italic {_CORAL}")
                t.append("(now)", style="dim")
            else:
                t.append(label, style="dim #aaaaaa")
                t.append(f"({entered_at:.1f}s)", style="dim")
        return t

    def rendered_text(self) -> str:
        """Plain-text rendering of the last frame sent to ``Static.update``.

        Stable testing surface — Textual's ``Static`` does not expose
        a public getter for its current renderable across versions, so
        we cache locally in ``_refresh`` and read here. Returns "" when
        nothing has been rendered yet (= pre-mount).
        """
        cache = self._rendered_cache
        if cache is None:
            return ""
        return str(cache.plain)

    def _refresh(self) -> None:
        if self._static is None:
            return
        head = self._build_finished() if self._finished else self._build_running()
        if not self._expanded:
            self._static.update(head)
            self._rendered_cache = head
            return
        # Expanded view: append the history line under the head with a
        # newline separator. Rich Text handles newlines inside update().
        body = Text()
        body.append_text(head)
        body.append("\n")
        body.append_text(self._build_history_line())
        self._static.update(body)
        self._rendered_cache = body
