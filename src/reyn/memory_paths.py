"""Resolve the project memory directory.

Memory lives in two layers (PR15+):
- `.reyn/memory/`                       — shared, visible to every agent
- `.reyn/agents/<name>/memory/`         — agent-scoped, only that agent

Global / cross-project memory was removed; all memory is scoped to one
project.
"""
from __future__ import annotations
from pathlib import Path


def memory_dir(agent: str | None = None) -> Path:
    """Memory directory for the given layer.

    `agent=None` (default) → shared layer.
    `agent="<name>"`       → that agent's scoped layer.
    """
    if agent is None:
        return Path(".reyn") / "memory"
    return Path(".reyn") / "agents" / agent / "memory"
