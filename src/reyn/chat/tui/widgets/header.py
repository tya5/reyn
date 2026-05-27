"""ReynHeader — top-of-screen status bar.

Displays: Reyn · <agent_name> · <model> · <tokens today> · <cost today>
on the left/centre, and a live clock (YYYY-MM-DD HH:MM:SS) on the right.

Updated via `app.post_message(ReynHeader.StatusUpdate(...))` or by calling
`refresh_status()` directly from async code. The clock self-ticks once
per second from on_mount.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label

from ._renderable_cache import RenderableCacheMixin

# Trailing date suffix on a model id: ``-YYYYMMDD`` (8 digits) or the
# ``-YYYY-MM-DD`` form. Both rotate per release and add 9-11 cells to
# the header without changing within a session. Stripping them recovers
# ~25 cells of header width on a narrow terminal — the cost / token
# counters and the clock canary fit again without truncation.
_MODEL_DATE_SUFFIX = re.compile(r"-(?:\d{8}|\d{4}-\d{1,2}-\d{1,2})$")
_MODEL_LATEST_SUFFIX = re.compile(r"-latest$")


def _cap_proximity_color(used: float | int, cap: float | int | None) -> str | None:
    """Return amber / red style when ``used`` is close to ``cap``, else None.

    B-F1 (wave-8): the header's token + cost segments use this to surface
    cap-proximity at a glance. Returns:

      - None  when no cap is set (= style=None → default ``#aaaaaa``)
      - ``"#ffaa44"`` (amber) when used / cap >= 0.75
      - ``"#ff4444"`` (red)   when used / cap >= 0.90

    Thresholds mirror ``cost_tab._budget_bar`` so the visual gradient is
    consistent across the header status line and the right-panel cost
    tab's progress bar. The hard ``budget_warn`` lifecycle marker still
    fires at 80 % per ``BudgetTracker.warn_ratio``; this softer header
    gradient gives the user lead-time.
    """
    if cap is None:
        return None
    try:
        cap_f = float(cap)
        used_f = float(used)
    except (TypeError, ValueError):
        return None
    if cap_f <= 0:
        return None
    ratio = used_f / cap_f
    if ratio >= 0.90:
        return "#ff4444"
    if ratio >= 0.75:
        return "#ffaa44"
    return None


def _shorten_model_id(model: str) -> str:
    """Return ``model`` with trailing date / ``-latest`` suffix stripped.

    Conservative: keeps the provider prefix (``claude-``, ``gpt-``, …) and
    everything up to the version segment so the user can still tell what
    family is active. Only strips the universally redundant tail.

    Examples::

        claude-opus-4-5-20251101    → claude-opus-4-5
        claude-3-7-sonnet-20250219  → claude-3-7-sonnet
        gpt-4o-2024-08-06           → gpt-4o
        gemini-1.5-flash-latest     → gemini-1.5-flash
        claude-sonnet-4-6           → claude-sonnet-4-6 (untouched)
    """
    if not model:
        return model
    stripped = _MODEL_DATE_SUFFIX.sub("", model)
    stripped = _MODEL_LATEST_SUFFIX.sub("", stripped)
    return stripped or model  # paranoia: never return empty


class ReynHeader(RenderableCacheMixin, Widget):
    """Single-line status bar docked at the top of the screen."""

    DEFAULT_CSS = """
    ReynHeader {
        dock: top;
        height: 1;
        background: #1a1a1a;
        layout: horizontal;
    }
    ReynHeader #title {
        color: $primary;
        text-style: bold;
        padding: 0 1;
        width: auto;
    }
    ReynHeader #status {
        color: #aaaaaa;
        text-align: right;
        padding: 0 1;
        width: 1fr;
    }
    """

    @dataclass
    class StatusUpdate(Message):
        """Posted to update the status fields without locking."""
        agent_name: str = ""
        model: str = ""
        tokens_today: int = 0
        tokens_cap: int | None = None
        cost_usd: float = 0.0
        cost_cap: float | None = None

    def __init__(
        self,
        *,
        agent_name: str = "",
        model: str = "",
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self._agent_name = agent_name
        self._model = model
        self._tokens_today = 0
        self._tokens_cap: int | None = None
        self._cost_usd = 0.0
        self._cost_cap: float | None = None
        # Issue #277 — count of stalled / cross-channel pending ops
        # surfaced as ``[N pending]`` badge on the status line when > 0.
        # Refreshed by ``set_stalled_count`` (called from the App layer
        # on the ``_refresh_live`` tick or right after an explicit
        # mutation). Omitted from the status entirely when 0 so the
        # cold-default UX is unchanged.
        self._stalled_count: int = 0
        # ``/find`` cycle-state badge — {"query", "position", "total"} dict
        # when a /find query is active (= router has seeded cycle state),
        # None otherwise. Surfaced as ``[find: 'q' 2/3]`` next to the
        # ``[N pending]`` badge so the user has a persistent "you're in
        # find mode" cue. The sticky status itself auto-hides after
        # ~2.5 s, so without the header badge the user couldn't tell
        # at a glance whether Ctrl+G is currently armed.
        self._find_state: dict | None = None
        # Voice mode badge state. One of:
        #   - None          — not in voice mode (= no badge)
        #   - "recording"   — mic is live; user is dictating
        #   - "transcribing"— mic stopped, whisper processing audio
        # Surfaced as ``🔴 voice`` / ``⏳ voice`` adjacent to the find
        # badge so the user has a persistent "voice mode active"
        # indicator that survives the conv-pane status-line scroll.
        # Without this, after switching focus away + back the user
        # couldn't tell at a glance whether they were still recording.
        self._voice_state: str | None = None
        # Cell-offset ranges for clickable status badges. Populated
        # in ``_format_status`` on every repaint so the mouse-click
        # dispatch ``on_click`` can map x-coord → badge → app action.
        # Keys: ``"find"`` / ``"pending"`` / ``"voice"``. Values:
        # ``(start_cell, end_cell)`` in the rendered status text.
        # Empty when no badge is currently rendered.
        self._badge_offsets: dict[str, tuple[int, int]] = {}
        # ``RenderableCacheMixin`` provides the cache slot +
        # ``rendered_text`` accessor. Every ``Label.update`` is paired
        # with ``self._set_rendered_cache(text)`` via the
        # ``_repaint_status`` funnel so the cache stays in lockstep
        # with the visible content.

    def compose(self) -> ComposeResult:
        yield Label("Reyn", id="title")
        initial = self._format_status()
        self._set_rendered_cache(initial)
        yield Label(initial, id="status")

    def on_mount(self) -> None:
        # Re-render once per second so the embedded clock stays current.
        # 1 Hz is plenty — seconds are included so a frozen UI is
        # immediately visible (the clock is the canary).
        self.set_interval(1.0, self._tick_clock)

    @staticmethod
    def _now_text() -> str:
        # ``HH:MM:SS`` only (8 cells) — the date portion (10 cells +
        # separator = 11 saved) pushed the header past the 80-col
        # terminal boundary and clipped the clock half on cold-default
        # widths. The clock's primary role here is the "is the UI
        # frozen?" canary, which is the SECOND field — seconds are what
        # change visibly. The date is already surfaced once in the conv
        # pane via ``_date_separator`` at session start, so dropping it
        # from the per-second header avoids the redundancy without
        # losing day-level context.
        return time.strftime("%H:%M:%S")

    def _tick_clock(self) -> None:
        self._repaint_status()

    def _repaint_status(self) -> None:
        """Render + cache the status Text, push to the Label widget.

        Single funnel for "status changed → repaint" so the mixin's
        cache stays in sync with what the user sees. Three sites
        previously did ``Label.update(_format_status())`` directly;
        they all delegate here now.
        """
        new_text = self._format_status()
        self._set_rendered_cache(new_text)
        try:
            self.query_one("#status", Label).update(new_text)
        except Exception:
            pass

    # Minimum cells to reserve for the agent name before progressive
    # field drop kicks in. 6 cells can show ~3 chars + ellipsis — enough
    # to be a meaningful identity signal.
    _MIN_AGENT_CELLS = 6

    def _compute_field_widths(
        self, *, include_model: bool = True, include_cost: bool = True, include_tokens: bool = True
    ) -> tuple[int, int]:
        """Return ``(other_cells, part_count)`` for the given field inclusion flags.

        Used by both ``_choose_included_fields`` and
        ``_maybe_truncate_agent_name`` so the budget arithmetic is in one
        place. ``agent_name`` and ``clock`` are always included (they are
        never dropped). Badges (pending / find / voice) are always included
        — they are active-state cues with higher priority than fixed fields.
        """
        other_cells = 0
        part_count = 0  # start at 0; agent_name slot counted by callers

        if include_model and self._model:
            other_cells += cell_len(_shorten_model_id(self._model))
            part_count += 1

        if include_tokens:
            tok_str = f"{self._tokens_today:,}"
            if self._tokens_cap is not None:
                tok_str += f" / {self._tokens_cap:,}"
            tok_str += " tok"
            other_cells += cell_len(tok_str)
            part_count += 1

        if include_cost:
            cost_str = f"${self._cost_usd:.4f}"
            if self._cost_cap is not None:
                cost_str += f" / ${self._cost_cap:.2f}"
            other_cells += cell_len(cost_str)
            part_count += 1

        if self._stalled_count > 0:
            other_cells += cell_len(f"[{self._stalled_count} pending]")
            part_count += 1

        if self._find_state is not None:
            fs = self._find_state
            find_badge = (
                f"[find: '{fs.get('query', '')}' "
                f"{fs.get('position', 0)}/{fs.get('total', 0)}]"
            )
            other_cells += cell_len(find_badge)
            part_count += 1

        if self._voice_state == "recording":
            other_cells += cell_len("🔴 voice · Enter→send Esc→cancel")
            part_count += 1
        elif self._voice_state == "transcribing":
            other_cells += cell_len("⏳ voice")
            part_count += 1

        # Clock is always present.
        other_cells += cell_len(self._now_text())
        part_count += 1

        return other_cells, part_count

    def _choose_included_fields(self, available: int) -> tuple[bool, bool, bool]:
        """Return ``(include_model, include_cost, include_tokens)`` flags.

        Drops optional fields in priority order (model first, then cost,
        then tokens) until the agent name budget reaches
        ``_MIN_AGENT_CELLS``. Clock and badges are never dropped — they
        are higher-priority active-state / canary signals.

        When ``available <= 0`` (= pre-mount or extreme width) all fields
        are returned as-is; the budget guard in ``_maybe_truncate_agent_name``
        handles the extreme case with the 3-cell minimum.
        """
        if available <= 0:
            return True, True, True

        name_cells = cell_len(self._agent_name) if self._agent_name else 0
        sep_w = cell_len("  │  ")

        include_model = True
        include_cost = True
        include_tokens = True

        for drop in ("model", "cost", "tokens"):
            other_cells, part_count = self._compute_field_widths(
                include_model=include_model,
                include_cost=include_cost,
                include_tokens=include_tokens,
            )
            # +1 for agent_name slot; separator count = total_parts - 1
            total_parts = 1 + part_count  # agent + everything else
            separator_cells = max(0, total_parts - 1) * sep_w
            budget = available - other_cells - separator_cells
            if budget >= self._MIN_AGENT_CELLS:
                break  # enough room — stop dropping
            if drop == "model":
                include_model = False
            elif drop == "cost":
                include_cost = False
            elif drop == "tokens":
                include_tokens = False

        return include_model, include_cost, include_tokens

    def _maybe_truncate_agent_name(
        self,
        *,
        include_model: bool = True,
        include_cost: bool = True,
        include_tokens: bool = True,
    ) -> str:
        """Truncate ``_agent_name`` so the assembled status fits the cell.

        Computes the cell-width of every other status field (model,
        tokens, cost, optional pending badge, clock) plus the ``  │  ``
        separators (5 cells each), subtracts from the widget's status
        cell width (= total widget width minus the title cell), and
        clips the agent name to whatever room remains. Appends ``…``
        when truncated. When ``self.size.width`` is 0 (= pre-mount) the
        name is returned verbatim — the next ``_tick_clock`` refresh
        will recompute once layout is known. A minimum of 3 cells is
        always reserved for the agent name so the field doesn't
        disappear entirely on extreme widths.

        The ``include_*`` flags mirror the output of ``_choose_included_fields``
        so that truncation budget matches the actual set of rendered parts.
        """
        name = self._agent_name
        if not name:
            return name
        try:
            total_width = self.size.width
        except Exception:
            total_width = 0
        if total_width <= 0:
            return name
        # Title cell ≈ 1 (left pad) + cell_len("Reyn") + 1 (right pad)
        # — matches the Label("Reyn", id="title") composed above. Hard-
        # coded here rather than re-querying the DOM because _format_status
        # is called from _tick_clock every second.
        title_cells = 2 + cell_len("Reyn")
        # Right-side status pad is 0 1 (= 1 cell on each side, see
        # DEFAULT_CSS ``#status padding: 0 1``).
        status_pad = 2
        available = total_width - title_cells - status_pad
        if available <= 0:
            return name
        other_cells, part_count = self._compute_field_widths(
            include_model=include_model,
            include_cost=include_cost,
            include_tokens=include_tokens,
        )
        # +1 for agent_name slot itself.
        total_parts = 1 + part_count
        separator_cells = max(0, total_parts - 1) * cell_len("  │  ")
        budget = available - other_cells - separator_cells
        if budget >= cell_len(name):
            return name
        if budget < 3:
            # Reserve at least 3 cells so the name isn't fully eaten.
            budget = 3
        # Walk the name char-by-char in cell space; stop when adding
        # the next char would exceed budget − 1 (= keep room for the
        # ellipsis cell).
        ellipsis = "…"
        max_body = budget - cell_len(ellipsis)
        if max_body <= 0:
            return ellipsis
        out: list[str] = []
        used = 0
        for ch in name:
            ch_cells = cell_len(ch)
            if used + ch_cells > max_body:
                break
            out.append(ch)
            used += ch_cells
        return "".join(out) + ellipsis

    def _format_status(self) -> Text:
        """Build the right-side status as a Rich Text with dim │ separators.

        Field layout (left → right):
          agent_name │ model │ tokens │ cost │ clock

        ``model`` is rendered DIM and date-suffix-stripped (e.g.
        ``claude-opus-4-5-20251101`` → ``claude-opus-4-5``). It rarely
        changes within a session, so de-emphasising it keeps the user's
        eye on the per-turn metrics that DO change — tokens, cost, and
        the clock canary at the right edge.

        Narrow-terminal layout: two-stage approach.

        Stage 1 — progressive field drop (``_choose_included_fields``):
        when the total of fixed fields would leave fewer than
        ``_MIN_AGENT_CELLS`` for the agent name, optional fields are
        dropped in priority order — model first (longest, least time-
        sensitive), then cost, then tokens. Clock and active-state
        badges (voice / find / pending) are never dropped.

        Stage 2 — agent name truncation (``_maybe_truncate_agent_name``):
        after optional fields are dropped, any remaining overflow is
        absorbed by truncating the agent name with ``…``. A 3-cell
        minimum is always reserved so the field doesn't disappear.
        """
        # Progressive field drop at narrow widths: compute which optional
        # fields (model / cost / tokens) survive given the available cell
        # budget. Clock and badges are never dropped. When the widget is
        # pre-mount (size.width == 0) _choose_included_fields returns all-
        # True and _maybe_truncate_agent_name falls back to verbatim.
        try:
            total_width = self.size.width
        except Exception:
            total_width = 0
        title_cells = 2 + cell_len("Reyn")
        status_pad = 2
        available = total_width - title_cells - status_pad
        include_model, include_cost, include_tokens = self._choose_included_fields(available)

        # (text, style) tuples — style=None falls back to the widget's
        # default text color (#aaaaaa, see DEFAULT_CSS above).
        parts: list[tuple[str, str | None]] = []
        if self._agent_name:
            parts.append((
                self._maybe_truncate_agent_name(
                    include_model=include_model,
                    include_cost=include_cost,
                    include_tokens=include_tokens,
                ),
                None,
            ))
        if include_model and self._model:
            parts.append((_shorten_model_id(self._model), "dim #888888"))

        if include_tokens:
            tok_str = f"{self._tokens_today:,}"
            if self._tokens_cap is not None:
                tok_str += f" / {self._tokens_cap:,}"
            tok_str += " tok"
            # B-F1 (wave-8): cap-proximity color escalation. When budget caps
            # are configured, the token / cost segments shift to amber at
            # ≥ 75 % utilisation and to red at ≥ 90 % so the user has a
            # live gradient cue without waiting for the hard
            # ``[↑ budget warn: …]`` lifecycle marker at 80 %. Thresholds
            # match the cost-tab's ``_budget_bar`` for cross-surface
            # consistency. When no cap is configured, the field stays at
            # the default ``#aaaaaa`` (= style=None falls through).
            parts.append((tok_str, _cap_proximity_color(self._tokens_today, self._tokens_cap)))

        if include_cost:
            # Use 4 decimals so the cheap-model spend stays visible. With 2dp
            # `gemini-flash-lite` rounds to `$0.00` even after dozens of calls;
            # users see the token counter tick up but think the cost is free.
            # The cap (when set) is at a larger scale, so 2dp there is fine.
            cost_str = f"${self._cost_usd:.4f}"
            if self._cost_cap is not None:
                cost_str += f" / ${self._cost_cap:.2f}"
            parts.append((cost_str, _cap_proximity_color(self._cost_usd, self._cost_cap)))
        # Issue #277 — pending-ops badge. Inserted before the clock so
        # the canary stays in its expected (= rightmost) position.
        # Amber-style colour to signal "user attention soft-required".
        # Omitted entirely when count is 0 — cold-default layout
        # unchanged.
        if self._stalled_count > 0:
            parts.append(
                (f"[{self._stalled_count} pending]", "#ffaa44"),
            )
        # ``/find`` active-state badge — placed alongside [N pending]
        # as a sibling "active state" indicator. Subtle blue
        # (= ``#88aacc``) so it doesn't compete with the amber pending
        # marker or red error styling; complementary palette signals
        # "informational state, no action required".
        if self._find_state is not None:
            fs = self._find_state
            badge = (
                f"[find: '{fs.get('query', '')}' "
                f"{fs.get('position', 0)}/{fs.get('total', 0)}]"
            )
            parts.append((badge, "#88aacc"))
        # Voice mode badge — mirrors the find-badge contract but uses
        # the recording / transcribing dichotomy. Red while the mic is
        # live (= user attention), amber while whisper processes the
        # audio (= "waiting on me, not on you"). Placed adjacent to the
        # find badge as a sibling "active state" cue; clock stays
        # rightmost.
        # T2-3 (Wave-12 B#8): while recording, append a dim inline hint
        # so users discover dictate-and-send / cancel without reading docs.
        # Kept short (11 cells) so it survives narrow-terminal truncation.
        if self._voice_state == "recording":
            parts.append(("🔴 voice · Enter→send Esc→cancel", "bold #ff6644"))
        elif self._voice_state == "transcribing":
            parts.append(("⏳ voice", "bold #ffaa44"))
        # Clock always present, last — the canary for "is the UI frozen?"
        parts.append((self._now_text(), None))

        # Build the assembled Text + track cell offsets for each
        # clickable badge so ``on_click`` can dispatch by mouse-x
        # position. Each badge gets a tuple of (start_cell, end_cell)
        # in the *rendered* text — separator widths are included as
        # they advance ``cur`` between parts.
        out = Text()
        sep = "  │  "
        sep_w = cell_len(sep)
        self._badge_offsets: dict[str, tuple[int, int]] = {}
        cur = 0
        for i, (text, style) in enumerate(parts):
            if i > 0:
                out.append(sep, style="dim #555555")
                cur += sep_w
            seg_w = cell_len(text)
            if text.startswith("[find:"):
                self._badge_offsets["find"] = (cur, cur + seg_w)
            elif text.startswith("[") and text.endswith("pending]"):
                self._badge_offsets["pending"] = (cur, cur + seg_w)
            elif text.startswith("🔴 voice") or text.startswith("⏳ voice"):
                self._badge_offsets["voice"] = (cur, cur + seg_w)
            if style is None:
                out.append(text)
            else:
                out.append(text, style=style)
            cur += seg_w
        return out

    def refresh_status(
        self,
        *,
        agent_name: str | None = None,
        model: str | None = None,
        tokens_today: int | None = None,
        tokens_cap: int | None = None,
        cost_usd: float | None = None,
        cost_cap: float | None = None,
        stalled_count: int | None = None,
    ) -> None:
        """Update status fields and re-render. Call from async context."""
        if agent_name is not None:
            self._agent_name = agent_name
        if model is not None:
            self._model = model
        if tokens_today is not None:
            self._tokens_today = tokens_today
        if tokens_cap is not None:
            self._tokens_cap = tokens_cap
        if cost_usd is not None:
            self._cost_usd = cost_usd
        if cost_cap is not None:
            self._cost_cap = cost_cap
        if stalled_count is not None:
            self._stalled_count = max(0, int(stalled_count))
        self._repaint_status()

    def set_find_state(
        self,
        query: str | None,
        position: int = 0,
        total: int = 0,
    ) -> None:
        """Update or clear the ``/find`` active-state badge.

        ``query=None`` clears the badge (= no /find currently active).
        Any non-empty query installs the badge as
        ``[find: 'q' position/total]``. The header re-renders only
        when the new state differs from the previous, so a stream
        of redundant updates from the router doesn't generate paint
        churn.
        """
        if query is None:
            new_state: dict | None = None
        else:
            new_state = {
                "query": query,
                "position": int(position),
                "total": int(total),
            }
        if new_state == self._find_state:
            return
        self._find_state = new_state
        self._repaint_status()

    def set_voice_state(self, state: str | None) -> None:
        """Update or clear the voice mode badge.

        Accepted states:
          - ``None``           → no badge (= voice mode inactive)
          - ``"recording"``    → 🔴 voice (red, mic is live)
          - ``"transcribing"`` → ⏳ voice (amber, whisper running)

        Anything else (= unknown string) clears the badge — defensive
        against caller typos. Equality-gated repaint so redundant
        calls don't churn the Static.
        """
        if state not in (None, "recording", "transcribing"):
            state = None
        if state == self._voice_state:
            return
        self._voice_state = state
        self._repaint_status()

    @property
    def voice_state(self) -> str | None:
        """Public read of the current voice-mode badge state.

        Returns ``None`` (= no badge), ``"recording"``, or
        ``"transcribing"``. Use ``set_voice_state()`` to mutate.
        Exposes the private ``_voice_state`` slot through the public
        surface so tests (and callers) don't need to reach into private
        state — per CLAUDE.md testing policy.
        """
        return self._voice_state

    @property
    def find_state(self) -> dict | None:
        """Public read of the current ``/find`` badge state.

        Returns the ``{"query", "position", "total"}`` dict when a
        /find query is active, or ``None`` when no find mode is live.
        Use ``set_find_state()`` to mutate.
        """
        return self._find_state

    def format_status(self) -> Text:
        """Public alias for the internal ``_format_status()`` builder.

        Returns the current status Rich Text (right side of the header).
        Tests use this to assert on rendered agent name and model without
        reaching into the private implementation.
        """
        return self._format_status()

    @property
    def badge_offsets(self) -> dict[str, tuple[int, int]]:
        """Read-only copy of the badge-name → (start, end) cell-range map.

        Populated by ``_format_status`` whenever a badge is rendered.
        Keys are ``"find"``, ``"pending"``, or ``"voice"``; values are
        ``(start_cell, end_cell)`` in text-relative (= status-text-local)
        coordinates. Tests use this to verify badge placement without
        reaching into the private dict.
        """
        return dict(self._badge_offsets)

    def badge_at_x(self, x: int) -> str | None:
        """Return the badge key under widget-local cell column ``x``, or None.

        Public for tests so the click-dispatch math can be exercised
        without faking a Click event. The status label right-aligns,
        so we convert the widget-local x to a position within the
        rendered text using ``self.size.width``. Returns None when
        the click lands outside any badge's cell range.

        Layout assumptions encoded here:
          - Title label ("Reyn", padding 0 1) occupies the leftmost
            ``len("Reyn") + 2 = 6`` cells.
          - Status label fills the remaining width with
            ``text-align: right; padding: 0 1`` — so the text right
            edge is at widget x = (widget_width - 2), and the text
            left edge is at (widget_width - 2 - text_cells).
        """
        try:
            total_width = int(self.size.width)
        except Exception:
            return None
        if total_width <= 0:
            return None
        rendered = self.rendered_text()
        text_cells = cell_len(rendered)
        if text_cells <= 0:
            return None
        # Right padding cell of status label.
        text_right_edge = total_width - 2
        text_left_edge = text_right_edge - text_cells
        pos_in_text = x - text_left_edge
        if pos_in_text < 0 or pos_in_text >= text_cells:
            return None
        for name, (start, end) in self._badge_offsets.items():
            if start <= pos_in_text < end:
                return name
        return None

    def on_click(self, event: events.Click) -> None:
        """Dispatch a click on a status badge to the corresponding app action.

        Per-badge mapping:
          - find    → Ctrl+G equivalent (= ``action_find_next``);
                      cycles to the next match
          - pending → opens the right panel + switches to the
                      Pending tab
          - voice   → toggles voice recording (= ``action_voice_toggle``,
                      same as Ctrl+R)

        Click outside any badge cell range → silent no-op (= the
        rest of the header has no click semantics; users clicking
        the agent name / model / cost expect nothing to happen).
        Stops propagation only when a badge was actually hit.
        """
        badge = self.badge_at_x(event.x)
        if badge is None:
            return
        event.stop()
        app = self.app
        if badge == "find":
            try:
                app.action_find_next()
            except Exception:
                pass
        elif badge == "pending":
            self._dispatch_pending_click(app)
        elif badge == "voice":
            # ``action_voice_toggle`` is async — schedule via
            # ``call_later`` so the click handler stays sync.
            try:
                app.call_later(app.action_voice_toggle)
            except Exception:
                pass

    def _dispatch_pending_click(self, app: "Widget") -> None:
        """Open the right panel + switch to the Pending tab.

        Inlines the "open + jump" sequence so this PR stays
        independent of the Ctrl+1..7 quick-jump PR (= #579, still
        in flight) — when that lands, the body here can collapse
        to ``app.action_panel_jump_pending()``.
        """
        from .right_panel import RightPanel
        try:
            panel_visible = getattr(app, "_panel_visible", False)
            if not panel_visible:
                app.action_toggle_panel()
            panel = app.query_one("#right_panel", RightPanel)
            panel.set_panel_type("pending")
        except Exception:
            pass

    def on_reyn_header_status_update(self, msg: StatusUpdate) -> None:
        """Handle StatusUpdate message."""
        self.refresh_status(
            agent_name=msg.agent_name or None,
            model=msg.model or None,
            tokens_today=msg.tokens_today or None,
            cost_usd=msg.cost_usd,
            cost_cap=msg.cost_cap,
            tokens_cap=msg.tokens_cap,
        )
