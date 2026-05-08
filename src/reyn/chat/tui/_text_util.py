"""Shared text helpers for the TUI."""
from __future__ import annotations


def _esc(s: str) -> str:
    """Escape Rich markup brackets in plain strings.

    Anywhere we render user-controlled / file-system-derived text into a
    Rich-markup string we need to escape ``[`` and ``]`` so the user
    can't accidentally (or maliciously) inject markup.
    """
    return s.replace("[", "\\[").replace("]", "\\]")


__all__ = ["_esc"]
