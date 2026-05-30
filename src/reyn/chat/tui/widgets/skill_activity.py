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

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from reyn.chat.tui._palette import _CORAL, _TEXT_NEUTRAL, _TEXT_MUTED, _TEXT_BODY, _STATUS_ERROR

from ._renderable_cache import RenderableCacheMixin

_TICK_INTERVAL_S = 0.5  # elapsed-time refresh rate

# Max tool calls rendered inline in the drill-down expand view.
# Beyond this we collapse to ``+N more``. Skills with > ~8 tool
# calls would otherwise push the expanded row across many screen
# lines, defeating the "compact drill-down" idiom.
_TOOL_DRILL_MAX_RENDER = 6

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

# Reserve cells on the right edge so long content doesn't clip behind
# scrollbar / RichLog right-padding at 80-col widths. Same 6-cell
# budget as ToolCallRow._RIGHT_MARGIN_CELLS so both rows degrade
# consistently at narrow terminals.
_RIGHT_MARGIN_CELLS = 6

# Minimum cells kept for the elapsed segment (e.g. "  2.1s" = 7 cells).
# Degrade order: drop detail → shorten phase → truncate skill_name#id.
# The leading glyph + space (2 cells) + elapsed are ALWAYS preserved.
_ELAPSED_MIN_CELLS = 7  # "  0.0s" at most


class SkillActivityRow(RenderableCacheMixin, Widget):
    """One ambient skill-progress row that updates in-place.

    Transitions from a live running line to a compact completion line on
    finish(). The elapsed-time counter ticks every 0.5 s via set_interval
    and stops as soon as finish() is called.
    """

    DEFAULT_CSS = """
    SkillActivityRow {
        height: auto;
        padding: 0 0;
        overflow: hidden;
    }
    SkillActivityRow Static {
        height: auto;
        padding: 0 0;
        overflow: hidden;
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
        # Tool-call history — each entry is ``(tool_name, args_snippet)``
        # recorded when a tool_call_started event fires under this
        # skill's run_id (= parent-aware routing from
        # ConversationView.start_tool_call_row). Powers the 2nd level
        # of the drill-down expand view — phases × tools — so a user
        # exploring a finished skill can see both "which phases ran"
        # and "what did each phase actually call". Bounded only by
        # the natural per-skill tool-call count; we cap the rendered
        # list at _TOOL_DRILL_MAX_RENDER to keep the expand row from
        # exploding for tool-heavy skills.
        self._tool_calls: list[tuple[str, str]] = []
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
        # Width override for testing — when non-zero, ``_available_width``
        # returns this value instead of ``self.size.width``. Set by tests
        # that need to exercise narrow-terminal truncation paths without
        # mounting the widget in a full Textual app.
        self._width_override: int = 0
        # ``RenderableCacheMixin`` provides the cache + ``rendered_text``
        # accessor; we just call ``self._set_rendered_cache(text)`` on
        # every ``Static.update`` so the cache stays in sync with what
        # the user sees.

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

    def record_tool_call(self, tool_name: str, args_repr: str = "") -> None:
        """Append a tool call to the drill-down history.

        Called by ``ConversationView.start_tool_call_row`` when a
        ``tool_call_started`` event arrives with a ``parent_run_id``
        matching this row's run_id. The pair (tool name + args
        snippet) is stored verbatim; rendering trims at expand time
        so the recorded data stays full-fidelity for future replay /
        export uses. Re-runs of the same tool with different args
        record as separate entries — re-invocation IS meaningful
        execution detail.
        """
        self._tool_calls.append((tool_name, args_repr))
        # Refresh only when expanded — collapsed view doesn't show
        # tool calls, so the rebuild would be wasted paint cycles
        # during a tool-call-heavy phase.
        if self._expanded:
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

    def build_running(self) -> "Text":
        """Public alias for ``_build_running()``.

        Returns the ``rich.text.Text`` for the running (= not yet
        finished) state of the row. Callers and tests can use this to
        inspect the rendered output without reading the private method
        directly — per CLAUDE.md testing policy.
        """
        return self._build_running()

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

    def _truncate_to_cells(self, text: str, max_cells: int) -> str:
        """Truncate ``text`` to fit within ``max_cells`` display cells.

        Cell-aware so CJK / emoji count as 2 cells per glyph. Appends
        an ellipsis when truncation happened. Reserves the ellipsis cell
        so the result always fits within the budget. Mirrors the same
        helper in ToolCallRow for consistent truncation behaviour across
        both widgets.
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

    def _available_width(self) -> int:
        """Return the widget width, falling back to 80 when unmounted.

        Mirrors the ToolCallRow / AsyncStackPanel pattern: pre-mount
        ``self.size.width`` is 0 so we use 80 (typical terminal width)
        to keep truncation arithmetic sensible before the widget is
        laid out.

        When ``_width_override`` is set (non-zero), that value is used
        directly — this supports tests that exercise narrow-terminal
        truncation without mounting the widget in a full Textual app.
        """
        if self._width_override:
            return self._width_override
        try:
            w = int(getattr(self.size, "width", 0))
        except Exception:
            w = 0
        return w if w > 0 else 80

    def _build_running(self) -> Text:
        total_width = self._available_width()
        secs = time.monotonic() - self._start

        # ── Elapsed segment (always kept) ─────────────────────────────────
        if secs >= _ELAPSED_RED_S:
            elapsed_style = "bold " + _STATUS_ERROR
        elif secs >= _ELAPSED_AMBER_S:
            elapsed_style = "bold #ffaa44"  # palette-candidate: elapsed-warning amber (no foundation token yet)
        else:
            elapsed_style = "dim"
        elapsed_str = f"  {secs:.1f}s"
        elapsed_cells = cell_len(elapsed_str)

        # ── Glyph (always kept) ───────────────────────────────────────────
        # Animated braille spinner — replaces the static ▶ so "still
        # alive" is obvious even when the response was streamed and the
        # user is wondering "is this skill still doing things?".
        spinner = _SPINNER_FRAMES[self._spin_idx % len(_SPINNER_FRAMES)]
        glyph_str = f"{spinner} "
        glyph_cells = cell_len(glyph_str)

        # ── Label prefix (always kept if present) ─────────────────────────
        prefix_cells = cell_len(self._label_prefix)

        # ── Budget arithmetic (right-to-left) ─────────────────────────────
        # Always-kept = prefix + glyph + elapsed + right-margin.
        # Remaining budget is distributed across: separator + skill_name#id
        # + phase + detail.
        always_cells = prefix_cells + glyph_cells + elapsed_cells + _RIGHT_MARGIN_CELLS
        body_budget = max(0, total_width - always_cells)

        # skill_name#id core segment: "  ·" prefix is 3 cells.
        skill_id = f"{self._skill_name}#{self._short_id}"
        skill_id_cells = cell_len(skill_id)
        sep_cells = cell_len("  · ")  # separator before phase

        # Phase segment: "phase_name" + optional " vN"
        if self._phase:
            phase_str = self._phase
            if self._visit > 1:
                phase_str += f" v{self._visit}"
        else:
            phase_str = ""
        phase_cells = cell_len(phase_str)

        # Detail segment: "  ⤷ " prefix (5 cells) + detail text.
        detail_prefix_cells = cell_len("  ⤷ ")
        detail_cells = cell_len(self._detail) if self._detail else 0

        # Plan badge: "  [plan N/M]" — plan + 4 bracket/space cells.
        plan_badge_cells = (
            cell_len(f"  [{self._plan_step_label}]")
            if self._plan_step_label
            else 0
        )

        # Degrade order:
        # 1. Full layout — everything fits.
        # 2. Drop detail (most ephemeral).
        # 3. Drop plan badge too.
        # 4. Truncate phase to whatever remains after skill_name#id.
        # 5. Truncate skill_name#id with ellipsis.

        full_needed = (
            skill_id_cells + sep_cells + phase_cells
            + plan_badge_cells + detail_prefix_cells + detail_cells
        )
        no_detail_needed = skill_id_cells + sep_cells + phase_cells + plan_badge_cells

        show_detail = full_needed <= body_budget
        # Plan badge shown when dropping detail frees enough space.
        show_plan_badge = no_detail_needed <= body_budget
        # Budget remaining for phase after skill_name#id + separator:
        after_skill_budget = max(0, body_budget - skill_id_cells - sep_cells)
        if show_plan_badge:
            after_skill_budget = max(0, after_skill_budget - plan_badge_cells)
        # Truncate phase to remaining budget:
        if phase_str and cell_len(phase_str) > after_skill_budget:
            phase_display = self._truncate_to_cells(phase_str, after_skill_budget)
        else:
            phase_display = phase_str

        # Truncate skill_name#id itself only when even skill+sep alone
        # exceeds body_budget — last-resort fallback.
        skill_id_display = skill_id
        if skill_id_cells + sep_cells > body_budget:
            skill_budget = max(4, body_budget - sep_cells)
            skill_id_display = self._truncate_to_cells(skill_id, skill_budget)

        # ── Assemble the Text object ───────────────────────────────────────
        t = Text()
        if self._label_prefix:
            t.append(self._label_prefix, style="dim " + _TEXT_NEUTRAL)
        t.append(glyph_str, style=_CORAL)
        # skill_name#abcd — normal weight
        t.append(skill_id_display, style="bold")
        t.append("  · ", style="dim")
        # phase — italic coral
        if phase_display:
            t.append(phase_display, style=f"italic {_CORAL}")
        # Elapsed — colour-coded so a slow / stuck skill stands out:
        #   < 30 s → dim (= normal)
        #   30–60s → amber (= "taking a while")
        #   ≥ 60 s → red (= "this is unusual; might be blocked")
        t.append(elapsed_str, style=elapsed_style)
        # Persistent plan-step badge — appears between elapsed and detail
        # so the user always knows "this skill is plan step 2/5" even after
        # the in-phase detail has been overwritten by the next llm: / act:
        # signal or cleared by a phase advance.
        if show_plan_badge and self._plan_step_label:
            t.append("  [", style="dim")
            t.append(self._plan_step_label, style=f"dim {_CORAL}")
            t.append("]", style="dim")
        # In-phase detail ("llm: opus-4-5", "act: 3 ops", etc.). Dim so
        # it doesn't compete with the phase name, separated from elapsed
        # by ``  ⤷`` so the eye can grok "this is the inner-most thing
        # happening right now".
        if show_detail and self._detail:
            t.append("  ⤷ ", style="dim")
            t.append(self._detail, style="dim")
        return t

    def _build_finished(self) -> Text:
        total_width = self._available_width()

        # ── Always-kept segments ──────────────────────────────────────────
        prefix_cells = cell_len(self._label_prefix)
        skill_id = f"{self._skill_name}#{self._short_id}"
        skill_id_cells = cell_len(skill_id)

        t = Text()
        if self._label_prefix:
            t.append(self._label_prefix, style="dim " + _TEXT_NEUTRAL)

        if self._success:
            glyph_str = "✓ "
            glyph_cells = cell_len(glyph_str)
            elapsed_str = self._elapsed()
            elapsed_cells = cell_len(elapsed_str)
            # Ctrl+B hint: "  · Ctrl+B → agents" — ~20 cells; shown only
            # when budget allows (it's always been the last to be dropped
            # in the old layout too, just implicitly via wrap).
            ctrl_hint = "  · Ctrl+B → agents"
            ctrl_hint_cells = cell_len(ctrl_hint)
            sep_cells = cell_len("  · ")

            # Build the reason segment if present.
            reason_str = f" · {self._reason}" if self._reason else ""
            reason_cells = cell_len(reason_str)

            # Budget for the ctrl-hint: total - always_kept - skill_id
            # - sep - elapsed - reason - right_margin.
            always_cells = prefix_cells + glyph_cells + skill_id_cells + sep_cells + elapsed_cells + _RIGHT_MARGIN_CELLS
            remaining = total_width - always_cells - reason_cells

            show_ctrl_hint = remaining >= ctrl_hint_cells
            # When reason + hint won't fit, drop hint first; reason is
            # more informative than the hint.
            if not show_ctrl_hint and reason_str:
                # Try showing reason without hint.
                pass  # reason_str stays; hint is already False

            # Truncate skill_name#id only as a last resort when the
            # essential segments alone exceed total_width.
            essential_cells = prefix_cells + glyph_cells + skill_id_cells + sep_cells + elapsed_cells
            if essential_cells > total_width - _RIGHT_MARGIN_CELLS:
                skill_budget = max(
                    4,
                    total_width - _RIGHT_MARGIN_CELLS - prefix_cells - glyph_cells - sep_cells - elapsed_cells,
                )
                skill_id = self._truncate_to_cells(skill_id, skill_budget)

            t.append(glyph_str, style="bold green")
            t.append(skill_id, style="dim")
            t.append("  · ", style="dim")
            t.append(elapsed_str, style="dim")
            if reason_str:
                t.append(reason_str, style="dim")
            # Use the same dot separator as the rest of the row rather
            # than a fixed 12-space gap — the gap pushed the line past
            # the conv pane width (~70 cols with the right panel open),
            # causing "Ctrl+B → agents" to wrap to a second line as an
            # orphan hint. `B` alone isn't bound — Ctrl+B is the real
            # panel-toggle binding, and the panel remembers its last
            # focal tab so it lands on agents/events automatically.
            if show_ctrl_hint:
                t.append(ctrl_hint, style="dim")
        elif self._aborted:
            # C-F5 (wave-8): user-initiated cancellation gets the ⊘
            # glyph in dim grey, distinguishing intent from a system
            # failure. Same shape design as ToolCallRow's aborted
            # state — color + glyph as a redundant cue.
            glyph_str = "⊘ "
            glyph_cells = cell_len(glyph_str)
            cancel_msg = (
                f"cancelled: {self._reason}" if self._reason else "cancelled"
            )
            sep_cells = cell_len("  · ")
            msg_cells = cell_len(cancel_msg)

            essential_cells = prefix_cells + glyph_cells + skill_id_cells + sep_cells
            remaining_for_msg = max(0, total_width - essential_cells - _RIGHT_MARGIN_CELLS)
            if msg_cells > remaining_for_msg:
                cancel_msg = self._truncate_to_cells(cancel_msg, remaining_for_msg)

            if essential_cells > total_width - _RIGHT_MARGIN_CELLS:
                skill_budget = max(4, total_width - _RIGHT_MARGIN_CELLS - prefix_cells - glyph_cells - sep_cells)
                skill_id = self._truncate_to_cells(skill_id, skill_budget)

            t.append(glyph_str, style="dim " + _TEXT_MUTED)
            t.append(skill_id, style="dim")
            t.append("  · ", style="dim")
            t.append(cancel_msg, style="dim")
        else:
            glyph_str = "✗ "
            glyph_cells = cell_len(glyph_str)
            failed_msg = f"failed: {self._reason}" if self._reason else "failed"
            ctrl_hint = "  · Ctrl+B → events"
            ctrl_hint_cells = cell_len(ctrl_hint)
            sep_cells = cell_len("  · ")
            msg_cells = cell_len(failed_msg)

            always_cells = prefix_cells + glyph_cells + skill_id_cells + sep_cells
            remaining_for_msg = max(0, total_width - always_cells - _RIGHT_MARGIN_CELLS)
            show_ctrl_hint = remaining_for_msg >= msg_cells + ctrl_hint_cells
            if msg_cells > remaining_for_msg - (ctrl_hint_cells if show_ctrl_hint else 0):
                msg_budget = max(
                    4,
                    remaining_for_msg - (ctrl_hint_cells if show_ctrl_hint else 0),
                )
                failed_msg = self._truncate_to_cells(failed_msg, msg_budget)

            if always_cells > total_width - _RIGHT_MARGIN_CELLS:
                skill_budget = max(4, total_width - _RIGHT_MARGIN_CELLS - prefix_cells - glyph_cells - sep_cells)
                skill_id = self._truncate_to_cells(skill_id, skill_budget)

            t.append(glyph_str, style="bold red")
            t.append(skill_id, style="dim")
            t.append("  · ", style="dim")
            t.append(failed_msg, style="dim")
            if show_ctrl_hint:
                t.append(ctrl_hint, style="dim")
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
            t.append(" " * len(self._label_prefix), style="dim " + _TEXT_NEUTRAL)
        t.append("  ↳ phases: ", style="dim")
        if not self._phase_history:
            t.append("(none yet)", style="dim " + _TEXT_NEUTRAL)
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
                t.append(label, style="dim " + _TEXT_BODY)
                t.append(f"({entered_at:.1f}s)", style="dim")
        return t

    def _build_tools_line(self) -> Text:
        """Render the tool-call drill-down line shown when expanded.

        Format:
          ``  ↳ tools (3): file:read, file:grep("foo"), bash:run("npm…")``

        Only rendered when ``_tool_calls`` is non-empty so skills
        that didn't call any tools don't get a dangling "tools: ()"
        line. The list is truncated at ``_TOOL_DRILL_MAX_RENDER``
        with a ``+N more`` suffix beyond that; the full tool list
        is still in ``_tool_calls`` for inspection / future export.
        """
        t = Text()
        if self._label_prefix:
            # Indent under the same prefix as the head/history lines
            # so a sub-skill's tool list nests visually with its parent.
            t.append(" " * len(self._label_prefix), style="dim " + _TEXT_NEUTRAL)
        t.append(f"  ↳ tools ({len(self._tool_calls)}): ", style="dim")
        for i, (tool_name, args) in enumerate(
            self._tool_calls[:_TOOL_DRILL_MAX_RENDER]
        ):
            if i > 0:
                t.append(", ", style="dim")
            t.append(tool_name, style="dim " + _TEXT_BODY)
            if args:
                # Compress args to a short hint so the tools-line
                # stays readable. Full args live on the
                # ToolCallRow (= ``_tool_call_rows[op_id]``).
                snippet = args if len(args) <= 14 else args[:13] + "…"
                t.append(f"({snippet})", style="dim " + _TEXT_MUTED)
        hidden = len(self._tool_calls) - _TOOL_DRILL_MAX_RENDER
        if hidden > 0:
            t.append(f", +{hidden} more", style="dim " + _TEXT_NEUTRAL)
        return t

    def _refresh(self) -> None:
        if self._static is None:
            return
        head = self._build_finished() if self._finished else self._build_running()
        if not self._expanded:
            self._static.update(head)
            self._set_rendered_cache(head)
            return
        # Expanded view: append the history line under the head with a
        # newline separator. Rich Text handles newlines inside update().
        body = Text()
        body.append_text(head)
        body.append("\n")
        body.append_text(self._build_history_line())
        # Tool-call drill-down (= 2nd level): only when at least one
        # tool ran under this skill. Without the gate, skills with
        # no tool calls would get a misleading empty "tools (0):" row.
        if self._tool_calls:
            body.append("\n")
            body.append_text(self._build_tools_line())
        self._static.update(body)
        self._set_rendered_cache(body)
