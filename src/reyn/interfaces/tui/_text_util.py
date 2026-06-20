"""Shared text helpers for the TUI."""
from __future__ import annotations

from rich.cells import cell_len


def _esc(s: str) -> str:
    """Escape Rich markup brackets in plain strings.

    Anywhere we render user-controlled / file-system-derived text into a
    Rich-markup string we need to escape ``[`` and ``]`` so the user
    can't accidentally (or maliciously) inject markup.
    """
    return s.replace("[", "\\[").replace("]", "\\]")


def truncate_to_cells(s: str, max_cells: int) -> str:
    """Truncate ``s`` to at most ``max_cells`` display columns, '…' if cut.

    CJK / full-width characters consume 2 cells each, so a naive ``s[:N]``
    codepoint slice overshoots a column budget by up to 2× and wraps narrow
    panel rows (mis-aligning cursor/row maps). ``rich.cells.cell_len`` is
    East-Asian-Width aware and matches what Textual renders. Returns the
    original string unchanged when it already fits.
    """
    if cell_len(s) <= max_cells:
        return s
    out: list[str] = []
    used = 0
    for ch in s:
        w = cell_len(ch)
        if used + w > max_cells:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


__all__ = ["_esc", "truncate_to_cells"]
