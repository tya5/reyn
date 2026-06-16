"""Textual TUI for `reyn chat` — default interactive surface.

Sub-packages:
  widgets/  — Header, ConversationView, InputBar, Intervention, StreamingRow
  theme.tcss — bundled CSS (coral accent)

The app entry point is `ReynTUIApp` in `app.py`.
"""
from __future__ import annotations

__all__ = ["ReynTUIApp"]
