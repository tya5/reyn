"""Textual widget collection for ReynTUIApp.

CSS placement rule (see also ``../theme.tcss`` header):
- ``theme.tcss`` (App CSS_PATH) — layout / dock / size for screen-level
  widgets (ReynHeader, ConversationView, InputBar, InterventionWidget).
- ``DEFAULT_CSS`` on each widget class — widget-internal rules that
  travel with the widget (focus indicators, scrollbar styling, child
  composition). Used by widgets composed inside other widgets where
  theme.tcss can't reach naturally.

Colour values: import from ``reyn.tui._palette`` rather than
hard-coding hex literals. ``_palette.py`` is the single source of
truth; re-themeing means editing that one file.
"""
from __future__ import annotations

from .conversation import ConversationView
from .header import ReynHeader
from .input_bar import InputBar
from .intervention import InterventionWidget
from .rewind_menu import RewindMenuWidget
from .right_panel import PANEL_TYPES, RightPanel
from .streaming_row import StreamingRow

__all__ = [
    "ReynHeader",
    "ConversationView",
    "InputBar",
    "InterventionWidget",
    "RewindMenuWidget",
    "StreamingRow",
    "RightPanel",
    "PANEL_TYPES",
]
