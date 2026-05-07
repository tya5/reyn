"""Centralised colour palette for the TUI.

Single source of truth for the coral primary + the dim greys used across
widgets. Re-themeing means editing this file, not 7 widget modules.

Naming convention:
- ``_CORAL`` — primary accent (matches ``Theme(primary=...)``)
- ``_BG_*``  — surface fills
- ``_BORDER_*`` — border / divider colours
- ``_TEXT_*`` — text colour ramps from dimmest to brightest
"""
from __future__ import annotations

# Primary accent — keep in sync with reyn.chat.tui.app.ReynTUIApp._REYN_THEME
_CORAL = "#C8553D"

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
_TEXT_BODY = "#aaaaaa"      # body / status
_TEXT_BRIGHT = "#dddddd"    # primary content

__all__ = [
    "_CORAL",
    "_BG_PANEL",
    "_BG_HEADER",
    "_BORDER_DIM",
    "_DIVIDER_DIM",
    "_TEXT_DIMMEST",
    "_TEXT_DIM",
    "_TEXT_NEUTRAL",
    "_TEXT_BODY",
    "_TEXT_BRIGHT",
]
