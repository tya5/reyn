"""Tier 1: the 4 packages #5051 moved to core dependencies import cleanly.

reyn's own packaging contract, not an OS invariant: ``fastapi`` / ``starlette``
/ ``uvicorn`` / ``websockets`` moved from the (now-empty back-compat)
``[web]`` extra into ``[project].dependencies`` (#5051, `pyproject.toml`).
17 test files across ``tests/web``/``tests/interfaces`` guard their own
FastAPI-backed fixtures with ``pytest.importorskip("fastapi", ...)`` — a
DELIBERATE decision, kept unchanged by #5051 (a #5058 follow-up owns
whether that stays ``importorskip`` or becomes a hard import), so a broken
install (one of these 4 genuinely missing, which after #5051 means a stale
environment rather than a normal configuration) silently skips all 17
rather than failing loud (CLAUDE.md's own six-questions Q4: "passes green
having never run"). This ONE test is the loud voice: if any of the 4 is
missing, THIS test fails hard (no importorskip), while the other 16 stay
harmlessly skipped.

Deliberately NOT derived from ``pyproject.toml``'s dependency list: a
package name does not always equal its import name (``uvicorn[standard]``
has no ``[standard]`` in the import statement; ``pillow`` imports as
``PIL``), so a naive derivation would need a second name-mapping table —
itself a second source of truth to keep in sync. The 4 packages below are
an explicit, curated enumeration; the reason for exactly these 4 (and no
others) is that they are what #5051 moved to core, not "every core
dependency" — a curated list needs a stated reason, and this docstring is
it (so the next reader does not have to re-derive it)."""
from __future__ import annotations


def test_web_core_deps_import_cleanly():
    """Tier 1: fastapi/starlette/uvicorn/websockets (the 4 packages #5051
    moved off the [web] extra into core) all import without raising, in
    THIS interpreter -- a broken/stale install (one genuinely missing or
    incompatible) fails this test loud, rather than silently skipping (the
    fate of the 17 importorskip-guarded files this test exists to give a
    voice to)."""
    import fastapi  # noqa: F401
    import starlette  # noqa: F401
    import uvicorn  # noqa: F401
    import websockets  # noqa: F401
