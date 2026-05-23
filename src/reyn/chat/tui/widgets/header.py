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

    def _maybe_truncate_agent_name(self) -> str:
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
        # Cell-width of everything else this _format_status emits.
        other_cells = 0
        if self._model:
            other_cells += cell_len(_shorten_model_id(self._model))
        tok_str = f"{self._tokens_today:,}"
        if self._tokens_cap is not None:
            tok_str += f" / {self._tokens_cap:,}"
        tok_str += " tok"
        other_cells += cell_len(tok_str)
        cost_str = f"${self._cost_usd:.4f}"
        if self._cost_cap is not None:
            cost_str += f" / ${self._cost_cap:.2f}"
        other_cells += cell_len(cost_str)
        if self._stalled_count > 0:
            other_cells += cell_len(f"[{self._stalled_count} pending]")
        other_cells += cell_len(self._now_text())
        # Separator count: number of joins between parts. With
        # agent_name included, parts = 1 (agent) + (1 if model) + 2
        # (tokens, cost) + (1 if stalled) + 1 (clock).
        part_count = 1 + (1 if self._model else 0) + 2 + (
            1 if self._stalled_count > 0 else 0
        ) + 1
        separator_cells = max(0, part_count - 1) * cell_len("  │  ")
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

        Narrow-terminal truncation: when the assembled status's total
        cell-width would exceed the widget's available width (= title
        cell already accounted for), ``_agent_name`` is the field
        truncated first. Rationale: it is (a) the field most likely to
        be long (full agent names can run 20+ cells), (b) the least
        time-sensitive (= changes rarely, doesn't carry per-turn
        information), and (c) the leftmost — keeping the clock canary,
        cost, and pending badge at the right edge intact for
        glance-reading.
        """
        # (text, style) tuples — style=None falls back to the widget's
        # default text color (#aaaaaa, see DEFAULT_CSS above).
        parts: list[tuple[str, str | None]] = []
        if self._agent_name:
            parts.append((self._maybe_truncate_agent_name(), None))
        if self._model:
            parts.append((_shorten_model_id(self._model), "dim #888888"))
        tok_str = f"{self._tokens_today:,}"
        if self._tokens_cap is not None:
            tok_str += f" / {self._tokens_cap:,}"
        tok_str += " tok"
        # Use 4 decimals so the cheap-model spend stays visible. With 2dp
        # `gemini-flash-lite` rounds to `$0.00` even after dozens of calls;
        # users see the token counter tick up but think the cost is free.
        # The cap (when set) is at a larger scale, so 2dp there is fine.
        cost_str = f"${self._cost_usd:.4f}"
        if self._cost_cap is not None:
            cost_str += f" / ${self._cost_cap:.2f}"
        # B-F1 (wave-8): cap-proximity color escalation. When budget caps
        # are configured, the token / cost segments shift to amber at
        # ≥ 75 % utilisation and to red at ≥ 90 % so the user has a
        # live gradient cue without waiting for the hard
        # ``[↑ budget warn: …]`` lifecycle marker at 80 %. Thresholds
        # match the cost-tab's ``_budget_bar`` for cross-surface
        # consistency. When no cap is configured, the field stays at
        # the default ``#aaaaaa`` (= style=None falls through).
        parts.append((tok_str, _cap_proximity_color(self._tokens_today, self._tokens_cap)))
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
        if self._voice_state == "recording":
            parts.append(("🔴 voice", "bold #ff6644"))
        elif self._voice_state == "transcribing":
            parts.append(("⏳ voice", "bold #ffaa44"))
        # Clock always present, last — the canary for "is the UI frozen?"
        parts.append((self._now_text(), None))

        out = Text()
        for i, (text, style) in enumerate(parts):
            if i > 0:
                out.append("  │  ", style="dim #555555")
            if style is None:
                out.append(text)
            else:
                out.append(text, style=style)
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
