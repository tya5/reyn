"""Shared utilities and constants for RightPanel tab renderers."""
from __future__ import annotations

import logging

_CORAL = "#C8553D"  # primary theme colour — matches Theme(primary=...)

logger = logging.getLogger(__name__)


def _esc(s: str) -> str:
    """Escape Rich markup brackets in plain strings."""
    return s.replace("[", "\\[").replace("]", "\\]")


__all__ = ["_CORAL", "_esc", "logger"]
