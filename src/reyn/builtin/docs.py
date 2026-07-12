"""Production-reachable read path for the ``docs/reference/`` mirror.

Proposal 0060 Addendum D, D5b. ``resolve_reyn_root()``
(``reyn.runtime.reyn_src.resolve_reyn_root``) raises ``RuntimeError`` in a
wheel install — there is no co-located ``pyproject.toml`` to anchor the walk,
so every ``docs/`` path built on top of it is unreachable in production. This
module is the production-safe alternative: it reads from the build-time
mirror at ``reyn.builtin.reference`` (``[tool.setuptools.package-data]``
``"builtin/**/*"``, F3a) via :mod:`importlib.resources`, which works
identically whether the package is an editable/dev checkout or an installed
wheel — no repo-root walk required.

**Dev-checkout fallback**: a plain (non-editable-build-hook) dev checkout may
not have run the build hook that populates ``src/reyn/builtin/reference/``
(``scripts/mirror_reference_docs.py``, wired into ``setup.py``'s custom
``build_py``) — the mirror directory is git-ignored, generated only at build
time. When the mirror is absent, this function falls back to
``resolve_reyn_root()`` + ``docs/reference/`` directly, which works in any
dev checkout (this is the ONLY caller allowed to treat that RuntimeError as
"docs unavailable" rather than letting it propagate — every other caller
should prefer this function over calling ``resolve_reyn_root()`` for docs
access).
"""
from __future__ import annotations

import importlib.resources as _resources


class DocNotFoundError(FileNotFoundError):
    """The requested reference doc is not reachable via the builtin mirror
    NOR via a dev-checkout ``docs/reference/`` fallback."""


def read_builtin_doc(rel_path: str) -> str:
    """Read a reference doc by its ``docs/reference/``-relative path.

    Tries the wheel-packaged builtin mirror first (production-reachable, no
    repo root needed); falls back to the live ``docs/reference/`` tree via
    ``resolve_reyn_root()`` for a dev checkout that hasn't run the build hook.
    Raises :class:`DocNotFoundError` if neither resolves.
    """
    try:
        mirror_root = _resources.files("reyn.builtin") / "reference"
        candidate = mirror_root / rel_path
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, NotADirectoryError):
        pass

    try:
        from reyn.runtime.reyn_src import resolve_reyn_root

        root = resolve_reyn_root()
    except RuntimeError as exc:
        raise DocNotFoundError(
            f"reference doc {rel_path!r} not reachable: no builtin mirror "
            "and no dev-checkout repo root"
        ) from exc

    fallback = root / "docs" / "reference" / rel_path
    if not fallback.is_file():
        raise DocNotFoundError(f"reference doc {rel_path!r} not found at {fallback}")
    return fallback.read_text(encoding="utf-8")
