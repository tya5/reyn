"""Resolve the project memory directory.

Memory lives at `.reyn/memory/` in the project workspace (CWD-relative).
Global / cross-project memory was removed; all memory is scoped to a single
project.
"""
from __future__ import annotations
from pathlib import Path


def memory_dir() -> Path:
    """The project-level memory directory under `.reyn/`."""
    return Path(".reyn") / "memory"
