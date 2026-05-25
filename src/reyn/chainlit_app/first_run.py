"""First-run asset copy for `reyn chainlit`.

When the operator launches ``reyn chainlit`` in a directory that has
never hosted a Chainlit app, Chainlit auto-creates a generic
``chainlit.md`` welcome page and a default ``.chainlit/config.toml``.
This module ships reyn-branded / reyn-tweaked versions in ``assets/``
and copies them into ``cwd`` first — same idempotent pattern as
Chainlit's own ``init_config()`` (= copy only if the destination
doesn't exist).

Operator customization is preserved: once they edit either file,
subsequent launches see it exists and skip the copy.
"""
from __future__ import annotations

import shutil
from pathlib import Path


def assets_dir() -> Path:
    """Return the directory holding shipped first-run assets."""
    return Path(__file__).resolve().parent / "assets"


def _copy_if_absent(src: Path, dst: Path) -> Path | None:
    """Internal: copy ``src`` to ``dst`` only when ``dst`` doesn't exist."""
    if not src.is_file():
        return None
    if dst.exists():
        return None
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst


def ensure_chainlit_md(target_dir: Path) -> Path | None:
    """Copy ``chainlit.md`` from assets to ``target_dir`` if absent."""
    return _copy_if_absent(
        assets_dir() / "chainlit.md",
        target_dir / "chainlit.md",
    )


def ensure_chainlit_config(target_dir: Path) -> Path | None:
    """Copy ``.chainlit/config.toml`` from assets if absent.

    Ships a partial config that flips ``[UI].confirm_new_chat = false``
    so agent switches via the chat-profile picker don't pop the
    "Start a new chat?" confirm dialog. Other ``[UI]`` / ``[features]``
    fields fall back to chainlit's pydantic class defaults because
    ``chainlit/config.py::load_settings`` merges partial TOML with the
    UISettings / FeaturesSettings constructors' defaults.

    Operator customization is preserved across launches — once the
    file exists at ``target_dir/.chainlit/config.toml`` this function
    is a no-op. Returns the destination path on copy, ``None`` on skip.
    """
    return _copy_if_absent(
        assets_dir() / ".chainlit" / "config.toml",
        target_dir / ".chainlit" / "config.toml",
    )


def ensure_public_css(target_dir: Path) -> Path | None:
    """Copy ``public/reyn.css`` from assets if absent.

    Chainlit's ``[UI].custom_css`` flag in the shipped config.toml
    points at ``/public/reyn.css`` (= APP_ROOT-relative path), so the
    file MUST sit at ``target_dir/public/reyn.css`` for the rule to
    apply. The default ships hides the upstream "New Chat" button
    because in a reyn context it triggers a bare session reset (=
    confusing as a UX affordance — see header comment in
    ``assets/public/reyn.css``). Operator can edit the file or drop
    the ``custom_css`` line to put the button back.
    """
    return _copy_if_absent(
        assets_dir() / "public" / "reyn.css",
        target_dir / "public" / "reyn.css",
    )


def ensure_all_assets(target_dir: Path) -> list[Path]:
    """Run every ``ensure_*`` helper, returning the list of paths that
    were actually written (= empty list when all destinations already
    existed and the call was a pure no-op)."""
    written: list[Path] = []
    for helper in (ensure_chainlit_md, ensure_chainlit_config, ensure_public_css):
        result = helper(target_dir)
        if result is not None:
            written.append(result)
    return written


__all__ = [
    "assets_dir",
    "ensure_all_assets",
    "ensure_chainlit_config",
    "ensure_chainlit_md",
    "ensure_public_css",
]
