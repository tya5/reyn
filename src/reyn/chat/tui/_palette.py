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

# Primary accent — keep in sync with reyn.chat.tui.app.ReynTUIApp._REYN_THEME
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
# (ErrorBox's own severity ramp is the _SEV_* family below — a separate concept.)
_STATUS_WARN = "#ffaa44"

# ErrorBox severity ramp (W13 3-tier border + header). HIGH/MED carry distinct
# rest + hover shades; LOW reuses the neutral text ramp (no dedicated token).
# This is the ErrorBox card's own severity system — distinct from _STATUS_ERROR
# (event-failure colour); the two reds name different concepts.
_SEV_HIGH = "#cc5555"            # high severity (rest)
_SEV_MED = "#cc9955"             # medium severity (rest)
_SEV_HIGH_HOVER = "#ff7777"      # high severity (hover — lighter sibling of _SEV_HIGH)
_SEV_MED_HOVER = "#ffbb77"       # medium severity (hover)

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
]
