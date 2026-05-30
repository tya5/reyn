"""Shared utilities and constants for RightPanel tab renderers.

Re-exports from the TUI-wide ``_palette`` and ``_text_util`` modules so
intra-package imports stay short (``from .base import _CORAL, _esc``).
The single source of truth for colour values lives in
``reyn.chat.tui._palette``.
"""
from __future__ import annotations

import logging

from reyn.chat.tui._palette import (
    _BORDER_DIM,
    _CORAL,
    _EVENT_INTERVENTION,
    _EVENT_LLM,
    _EVENT_PLAN,
    _EVENT_PLAN_MEMO,
    _EVENT_PLAN_STEP,
    _EVENT_SKILL,
    _EVENT_TOOL,
    _GREEN_DIMMEST,
    _STATUS_CRITICAL,
    _STATUS_ERROR,
    _STATUS_SUCCESS,
    _STATUS_SUCCESS_DIM,
    _TEXT_BODY,
    _TEXT_BRIGHT,
    _TEXT_DIM,
    _TEXT_MUTED,
    _TEXT_NEUTRAL,
)
from reyn.chat.tui._text_util import _esc

logger = logging.getLogger(__name__)

__all__ = [
    "_BORDER_DIM",
    "_CORAL",
    "_EVENT_INTERVENTION",
    "_EVENT_LLM",
    "_EVENT_PLAN",
    "_EVENT_PLAN_MEMO",
    "_EVENT_PLAN_STEP",
    "_EVENT_SKILL",
    "_EVENT_TOOL",
    "_GREEN_DIMMEST",
    "_STATUS_CRITICAL",
    "_STATUS_ERROR",
    "_STATUS_SUCCESS",
    "_STATUS_SUCCESS_DIM",
    "_TEXT_BODY",
    "_TEXT_BRIGHT",
    "_TEXT_DIM",
    "_TEXT_MUTED",
    "_TEXT_NEUTRAL",
    "_esc",
    "logger",
]
