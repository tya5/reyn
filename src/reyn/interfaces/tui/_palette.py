"""Centralised colour palette for the TUI.

Single source of truth for the coral primary + the dim greys used across
widgets. Re-themeing means editing this file, not 7 widget modules.

Naming convention:
- ``_CORAL`` — interactive / "you are here" affordance accent
  (matches ``Theme(primary=...)``). Use for: action hints, cursor
  indicators (▶ on a focused row, ▌ on selected picker rows), panel
  tab labels, status glyphs.
- ``_AMBER`` — agent identity accent. Use for: agent header label, the
  streaming cursor (▍), and the intervention prefix (``<name> asks``).
  Distinct from coral so the eye can immediately tell "this names the
  agent" from "you can act here / your cursor is here". Coral and
  amber share a warm hue family so the two reads still feel cohesive.
- ``_BG_*``  — surface fills
- ``_BORDER_*`` — border / divider colours
- ``_TEXT_*`` — text colour ramps from dimmest to brightest
"""
from __future__ import annotations

# Primary accent — keep in sync with reyn.interfaces.tui.app.ReynTUIApp._REYN_THEME
_CORAL = "#C8553D"
# Agent-identity accent. Warm amber, distinct enough from _CORAL that the
# eye reads "agent name / agent stream" as a separate signal from "you
# can act / your cursor is here". Picked on the same hue arc as _CORAL
# (= no jarring colour shift), but with notably lower saturation + a
# yellow lean so the two reads don't merge in dim light.
_AMBER = "#d4945a"

# Surfaces
_BG_PANEL = "#111111"     # right panel + tabs background
_BG_HEADER = "#1a1a1a"    # ReynHeader / _PanelHeader strip background

# Borders / dividers
_BORDER_DIM = "#2a2a2a"   # default panel / section border
_DIVIDER_DIM = "#333333"  # _PanelHeader bottom + #content top divider

# Text ramps (very dim → very bright)
_TEXT_DIMMEST = "#444444"   # timestamps, dim hints
_TEXT_DIM = "#555555"       # secondary labels
_TEXT_NEUTRAL = "#666666"   # neutral
_TEXT_MID = "#777777"       # mid-dim labels (cost call-counts, reply previews) — between neutral and muted
_TEXT_MUTED = "#888888"     # mid-dim labels (web events etc.) — between mid and body
_TEXT_SELECTED = "#bbbbbb"  # selected / highlighted row text (slash picker) — between body and bright
_TEXT_BODY = "#aaaaaa"      # body / status
_TEXT_BRIGHT = "#dddddd"    # primary content

# ── Semantic status / event accents ────────────────────────────────────────
# Centralised from ~475 hardcoded hex across widgets (events_tab carried the
# densest taxonomy). These PRESERVE intentional distinctions verified against
# usage — they are NOT duplicates to collapse:
#   - ``_STATUS_ERROR`` (recoverable: tool/mcp/validation/retry failures) vs
#     ``_STATUS_CRITICAL`` (hard stop: permission_denied / phase_failed /
#     budget·loop exceeded / interrupted) are distinct severity tiers.
#   - the plan-event oranges are deliberately a separate hue family from the
#     skill-event blue so a plan's forensic trail reads as its own category.
# Re-theme = edit here, not the widgets.
_STATUS_SUCCESS = "#44cc88"      # phase/step completed, ✓ ok, granted
_STATUS_SUCCESS_DIM = "#88ddaa"  # control_decided / extension granted (lighter sibling)
_STATUS_ERROR = "#ff6644"        # recoverable failure
_STATUS_CRITICAL = "#ff4444"     # hard stop / critical (distinct tier from _STATUS_ERROR)
_EVENT_SKILL = "#88aaff"         # skill_run / workflow / artifact / index activity (blue)
_EVENT_TOOL = "#cc88ff"          # tool / mcp invocation (purple)
_EVENT_LLM = "#ffcc66"           # llm call / budget warn (warm yellow)
_EVENT_INTERVENTION = "#ffcc88"  # user intervention / safety checkpoint / routing (amber)
_EVENT_PLAN = "#ff9944"          # plan emitted / aggregated / timeout (orange — distinct from skill blue)
_EVENT_PLAN_STEP = "#ffaa66"     # plan step lifecycle
_EVENT_PLAN_MEMO = "#cc8855"     # plan step memoization
_GREEN_DIMMEST = "#335544"       # context_built (very dim green)

# Warning accent — "taking a while / pending / stalled / cap-proximity". Recurring
# across header (cap≥0.75, pending, transcribing), streaming_row / tool_call_row /
# async_stack_panel / skill_activity (elapsed-time stall). Distinct from the event
# ambers (_EVENT_LLM / _EVENT_INTERVENTION / _EVENT_PLAN*) which name event categories.
# (The inline error severity ramp is the _SEV_* family below — a separate concept.)
_STATUS_WARN = "#ffaa44"

# Inline error severity ramp (3-tier text colour for write_error). HIGH/MED carry
# distinct shades; LOW reuses the neutral text ramp (no dedicated token).
# Drives write_error's inline coloured header in the conv RichLog.
# Distinct from _STATUS_ERROR (event-failure colour); the two reds name different concepts.
_SEV_HIGH = "#cc5555"            # high severity
_SEV_MED = "#cc9955"             # medium severity
_SEV_HIGH_HOVER = "#ff7777"      # high severity (hover — kept for palette completeness)
_SEV_MED_HOVER = "#ffbb77"       # medium severity (hover — kept for palette completeness)

# ── Phase-2c semantic accents (promoted from hardcoded hex) ─────────────────
# Muted red — a desaturated red for "soft negative" states where the full
# _STATUS_ERROR (#ff6644) red would be too loud: cancelled / partial-reply
# aborted (app.py, conversation), the 8-colour-terminal error glyph fallback
# (sticky_status), and the remote-limited mode marker (pending_tab). Distinct
# shade from _STATUS_ERROR (active failure) and _SEV_HIGH (inline error severity).
# Lightened from #aa6666 (= 4.32:1, below WCAG AA) to clear 4.5:1 against the
# panel bg (#111111 → 5.11:1) — this carries cancel/abort text, so it must be
# legible. Kept in its own pink-red lane, distinct from the brand _CORAL.
_RED_MUTED = "#b87272"
# "ready / idle-but-available" agent/process state (olive). Orthogonal to
# _STATUS_SUCCESS (running/done green) — the agents + pending panels show
# ready ◐ in olive vs running in green so the two states stay scannable.
_STATUS_READY = "#aaaa55"
# Dark success green — low-emphasis success tone for cost figures (cost_tab).
# Darker sibling of _STATUS_SUCCESS (#44cc88) / _STATUS_SUCCESS_DIM (#88ddaa,
# lighter); reads as "nominal" without competing with the brighter greens.
# Lightened from #2d7a4f (= 3.60:1, below WCAG AA) to 4.5:1+ against the panel
# bg (#111111 → 5.34:1) — it labels cost figures (informational numbers), so
# it must be legible. Stays the dimmest of the three success greens.
_STATUS_SUCCESS_DARK = "#3a9968"
# Actionable inline recovery hint (write_error inline hint line, warm olive-gold).
# Distinct from _TEXT_DIM (the dim metadata pointer) so the extracted recovery
# action reads as "do this", not just dim metadata. Lightened from #8a7a4a
# (= 4.46:1, just below WCAG AA) to 4.5:1+ (#111111 → 5.51:1) while keeping
# its own olive-gold lane — distinct from _SEV_MED amber, _AMBER agent
# accent, and _STATUS_READY olive.
_HINT_ACTION = "#9a8a52"

# Intervention widget warm surface (promoted from cross-file duplicates that
# lived in BOTH intervention.py DEFAULT_CSS and theme.tcss — a drift hazard
# now that theme.tcss can reference _palette via the $reyn-* CSS vars). These
# name the intervention widget's own warm theme; distinct from the global
# _BG_*/_TEXT_* ramp. The remaining intervention browns (#2a1a10 / #443322 /
# #aa8866 / #664433 / #ddaa88) are intervention.py-only (single-use) → left
# literal there (tokenising single-use colours adds indirection without a
# single-source win).
_IV_SURFACE = "#1e1510"          # intervention widget background
_IV_TEXT = "#eeddcc"             # intervention default text
_IV_HOVER = "#e0664e"            # intervention button hover / focus

__all__ = [
    "_CORAL",
    "_AMBER",
    "_BG_PANEL",
    "_BG_HEADER",
    "_BORDER_DIM",
    "_DIVIDER_DIM",
    "_TEXT_DIMMEST",
    "_TEXT_DIM",
    "_TEXT_NEUTRAL",
    "_TEXT_MID",
    "_TEXT_MUTED",
    "_TEXT_SELECTED",
    "_TEXT_BODY",
    "_TEXT_BRIGHT",
    "_STATUS_SUCCESS",
    "_STATUS_SUCCESS_DIM",
    "_STATUS_ERROR",
    "_STATUS_CRITICAL",
    "_STATUS_WARN",
    "_EVENT_SKILL",
    "_EVENT_TOOL",
    "_EVENT_LLM",
    "_EVENT_INTERVENTION",
    "_EVENT_PLAN",
    "_EVENT_PLAN_STEP",
    "_EVENT_PLAN_MEMO",
    "_GREEN_DIMMEST",
    "_SEV_HIGH",
    "_SEV_MED",
    "_SEV_HIGH_HOVER",
    "_SEV_MED_HOVER",
    "_RED_MUTED",
    "_STATUS_READY",
    "_STATUS_SUCCESS_DARK",
    "_HINT_ACTION",
    "_IV_SURFACE",
    "_IV_TEXT",
    "_IV_HOVER",
    "css_variables",
]


def css_variables() -> dict[str, str]:
    """Palette as Textual CSS variables (``$reyn-<name>``).

    ``theme.tcss`` (and any ``.tcss``) cannot ``import`` the Python tokens
    above, so historically it mirrored their hex values as literals kept in
    manual sync. The App's ``get_css_variables`` override injects this map so
    ``.tcss`` can reference the palette directly — e.g. ``color: $reyn-text-body``
    — making ``_palette.py`` the single source for CSS-side colours too (no
    more hand-synced hex). Add a row here when a ``.tcss`` rule needs a token.
    """
    return {
        "reyn-bg-panel": _BG_PANEL,
        "reyn-bg-header": _BG_HEADER,
        "reyn-border-dim": _BORDER_DIM,
        "reyn-divider-dim": _DIVIDER_DIM,
        "reyn-text-dimmest": _TEXT_DIMMEST,
        "reyn-text-dim": _TEXT_DIM,
        "reyn-text-neutral": _TEXT_NEUTRAL,
        "reyn-text-mid": _TEXT_MID,
        "reyn-text-muted": _TEXT_MUTED,
        "reyn-text-selected": _TEXT_SELECTED,
        "reyn-text-body": _TEXT_BODY,
        "reyn-text-bright": _TEXT_BRIGHT,
        "reyn-status-success": _STATUS_SUCCESS,
        "reyn-status-error": _STATUS_ERROR,
        "reyn-status-critical": _STATUS_CRITICAL,
        "reyn-status-warn": _STATUS_WARN,
        "reyn-event-intervention": _EVENT_INTERVENTION,
        "reyn-iv-surface": _IV_SURFACE,
        "reyn-iv-text": _IV_TEXT,
        "reyn-iv-hover": _IV_HOVER,
    }
