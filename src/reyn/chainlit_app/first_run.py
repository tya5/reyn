"""First-run asset copy for `reyn chainlit`.

When the operator launches ``reyn chainlit`` in a directory that has
never hosted a Chainlit app, Chainlit auto-creates a generic
``chainlit.md`` welcome page. This module ships a reyn-branded version
in ``assets/chainlit.md`` and copies it into ``cwd`` first — same
idempotent pattern as Chainlit's own ``init_config()`` (= copy only if
the destination doesn't exist).

Operator customization is preserved: once they edit ``chainlit.md``,
subsequent launches see it exists and skip the copy.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def assets_dir() -> Path:
    """Return the directory holding shipped first-run assets."""
    return Path(__file__).resolve().parent / "assets"


def ensure_chainlit_md(target_dir: Path) -> Path | None:
    """Copy ``chainlit.md`` from assets to ``target_dir`` if absent.

    Returns the destination path when a copy actually happened, ``None``
    when the file already existed (= operator customization preserved).
    """
    src = assets_dir() / "chainlit.md"
    if not src.is_file():
        return None
    dst = target_dir / "chainlit.md"
    if dst.exists():
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


__all__ = ["assets_dir", "ensure_chainlit_md"]
