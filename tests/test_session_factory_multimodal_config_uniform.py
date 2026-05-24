"""Tier 2: every ``ChatSession(...)`` call site passes ``multimodal_config``.

Background: ``MediaStore`` is built from ``multimodal_config`` inside
``ChatSession.__init__``.  When ``multimodal_config`` is missing, the
session ends up with ``media_store=None``, which silently disables the
preview-driven tool result path (= path_ref mode in ``web_fetch`` /
``file_read``).  The failure mode is invisible — the session still runs,
``web_fetch`` returns inline content instead of a path_ref + preview,
and ``read_tool_result`` errors with "MediaStore is not configured".

Asymmetry observed 2026-05-22 during #385 Step 2 measurement prep:

* ``src/reyn/cli/commands/chat.py``    — passes ``multimodal_config`` ✓
* ``src/reyn/cli/commands/dogfood.py`` — passes ``multimodal_config`` ✓
* ``src/reyn/cli/commands/mcp.py``     — MISSING (this PR fixes)
* ``src/reyn/web/deps.py``             — MISSING (this PR fixes)

The fix is straightforward, but the asymmetry will reappear whenever a
new ``ChatSession()`` call site is added (= different surface: A2A
server, HTTP gateway, scheduled job, etc.).  This test parses each
file's AST, finds every ``ChatSession(...)`` call, and asserts the
``multimodal_config`` keyword is present.  Drift is caught at PR-review
time instead of post-deploy during measurement.

Tier 2 because the cross-callsite invariant is an OS-level wiring rule
the chat session contract depends on; a regression directly breaks the
preview-driven path that #385 / PR #396 ship.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    """Return the Reyn repo root by walking up from this test file."""
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("unable to locate repo root from " + str(here))


def _chat_session_calls_in(path: Path) -> list[ast.Call]:
    """Return every ``ChatSession(...)`` call node in the parsed source.

    Matches both ``ChatSession(...)`` (= direct name) and ``X.ChatSession(...)``
    (= attribute access).  Only the rightmost name segment is compared so
    aliased / re-exported forms still trigger.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "ChatSession":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "ChatSession":
            calls.append(node)
    return calls


# The 4 call sites this invariant covers.  Adding a new one is a deliberate
# act — append it here in the same PR that introduces the call site.
_CALL_SITE_FILES = [
    "src/reyn/cli/commands/chat.py",
    "src/reyn/cli/commands/dogfood.py",
    "src/reyn/cli/commands/mcp.py",
    "src/reyn/web/deps.py",
    "src/reyn/chainlit_app/app.py",
]


@pytest.mark.parametrize("rel_path", _CALL_SITE_FILES)
def test_chat_session_call_site_passes_multimodal_config(rel_path: str) -> None:
    """Tier 2: each known ``ChatSession()`` factory call passes
    ``multimodal_config``.  A missing kwarg here disables preview-driven
    tool result path silently (= ``media_store=None`` fallback).
    """
    path = _repo_root() / rel_path
    assert path.is_file(), f"call-site file moved? {rel_path}"
    calls = _chat_session_calls_in(path)
    assert calls, f"no ChatSession(...) call found in {rel_path}"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
        assert "multimodal_config" in keywords, (
            f"{rel_path}:{call.lineno} ChatSession(...) is missing "
            "`multimodal_config=` kwarg. Add `multimodal_config=<source>` "
            "so MediaStore can build and preview-driven tool results work."
        )


def test_no_unknown_chat_session_call_sites() -> None:
    """Tier 2: every ``ChatSession(...)`` call inside ``src/`` is covered
    by this test's ``_CALL_SITE_FILES`` list.

    If you added a new call site (= different surface like a new HTTP
    gateway / scheduled job), append it to ``_CALL_SITE_FILES`` IN THE
    SAME PR so the multimodal_config invariant remains enforced.
    Forgetting to add it here means the new surface silently ships
    without preview-driven support.

    Test sites under ``tests/`` are intentionally excluded — they
    construct minimal ``ChatSession`` instances for unit testing where
    ``multimodal_config`` doesn't need to be wired.
    """
    root = _repo_root()
    src = root / "src"
    expected = {(root / p).resolve() for p in _CALL_SITE_FILES}
    actual = set()
    for py in src.rglob("*.py"):
        if not _chat_session_calls_in(py):
            continue
        # Filter out the ChatSession class definition itself (= reyn/chat/session.py).
        # The class file references ``ChatSession`` only through self / its own
        # definition; constructor call sites are the ones in factory functions.
        text = py.read_text(encoding="utf-8")
        if "class ChatSession" in text:
            continue
        actual.add(py.resolve())
    extras = actual - expected
    assert not extras, (
        "Unlisted ChatSession() call sites found — add them to "
        "_CALL_SITE_FILES in this test in the same PR. Found: "
        + ", ".join(sorted(str(p.relative_to(root)) for p in extras))
    )
