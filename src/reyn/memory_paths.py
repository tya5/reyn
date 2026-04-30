"""Resolve global and per-project memory directories.

Global memory:  ~/.reyn/memory/      (cross-project, "the user")
Project memory: <state_dir>/memory/   (CWD-scoped, "this project")

Both directories are searched at recall time. Writes are routed by the
write_memory skill based on its scope decision.
"""
from __future__ import annotations
from pathlib import Path


def global_memory_dir() -> Path:
    """The user-level memory directory shared across projects."""
    return Path.home() / ".reyn" / "memory"


def project_memory_dir(state_dir: str | Path) -> Path:
    """The project-level memory directory under the given .reyn state dir."""
    return Path(state_dir) / "memory"


def ensure_memory_dirs(state_dir: str | Path) -> tuple[Path, Path]:
    """Make sure both directories exist. Return (global_dir, project_dir)."""
    g = global_memory_dir()
    p = project_memory_dir(state_dir)
    g.mkdir(parents=True, exist_ok=True)
    p.mkdir(parents=True, exist_ok=True)
    return g, p
