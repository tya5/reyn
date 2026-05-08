"""Shared utilities and constants for RightPanel tab renderers.

Re-exports from the TUI-wide ``_palette`` and ``_text_util`` modules so
intra-package imports stay short (``from .base import _CORAL, _esc``).
The single source of truth for colour values lives in
``reyn.chat.tui._palette``.
"""
from __future__ import annotations

import logging

from reyn.chat.tui._palette import _CORAL
from reyn.chat.tui._text_util import _esc

logger = logging.getLogger(__name__)

__all__ = ["_CORAL", "_esc", "logger"]
