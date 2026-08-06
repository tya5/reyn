"""Resolve the project memory directory.

Memory lives in two layers (PR15+):
- `.reyn/memory/`                       — shared, visible to every agent
- `.reyn/agents/<name>/memory/`         — agent-scoped, only that agent

Global / cross-project memory was removed; all memory is scoped to one
project.
"""
from __future__ import annotations

from pathlib import Path


def memory_dir(agent: str | None = None, root: Path | None = None) -> Path:
    """Memory directory for the given layer.

    `agent=None` (default) → shared layer.
    `agent="<name>"`       → that agent's scoped layer.

    ``root`` (#3716): the caller's already-resolved project root
    (``.reyn``-inclusive — same convention as
    ``reyn.runtime.services.recovery.default_snapshot_path``'s ``root``
    param, #3705). ``None`` (every caller that has no root to supply) falls
    back to ``Path.cwd() / ".reyn"`` — the exact previous behavior.
    """
    base = root if root is not None else Path.cwd() / ".reyn"
    if agent is None:
        return base / "memory"
    return base / "agents" / agent / "memory"
