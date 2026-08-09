"""Repo-root discovery for tests, by walking up from a file's own location
to the nearest ancestor containing ``pyproject.toml`` — never by counting
directory levels from a call site.

A depth-counted root (``Path(__file__).parent.parent``, or an explicit
``.parents[N]``) is correct only as long as the file's own position in the
tree never changes; moving the file to a different depth silently breaks
it, and the breakage is invisible at the diff level (a byte-identical
``git mv`` still reports a pure rename) — it only surfaces at test-run
time, as a wrong path. The marker walk here is depth-independent: it is
correct at any location, including this module's own, permanently.

``REPO_ROOT`` is resolved once, at import time. If no ancestor carries
``pyproject.toml`` (this module has moved outside the repo, or the repo
itself lost its marker), resolution raises loudly rather than silently
falling back to the current working directory.

No ``SRC`` / ``SCRIPTS`` convenience constants are exported here: the
111 call sites this module was built for don't agree on what "SRC"
should even point at (some want ``src/``, some want ``src/reyn/``, one
wants ``scripts/dogfood_trace.py``) — each keeps composing its own
suffix from ``REPO_ROOT`` rather than a shared constant nobody would
actually consume.
"""
from __future__ import annotations

from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Walk *start* and its ancestors for the nearest ``pyproject.toml``.
    Raises ``RuntimeError`` — never returns a guess — if none is found."""
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(
        f"no pyproject.toml found walking up from {start} — repo root "
        "could not be located; refusing to silently fall back to cwd"
    )


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
