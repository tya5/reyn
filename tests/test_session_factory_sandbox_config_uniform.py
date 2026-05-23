"""Tier 2: every ``ChatSession(...)`` call site passes ``sandbox_config``.

Background: ``ChatSession`` forwards ``sandbox_config`` into the
``Agent`` it builds; the Agent in turn threads it into ``RouterCallerState``
where ``sandboxed_exec`` looks it up to pick the right backend
(``noop`` / ``seatbelt`` / ``landlock``). When the kwarg is missing,
the session ends up with ``sandbox_config=None`` and the chat-router
path falls back to ``get_default_backend(None)`` — which auto-selects
Seatbelt on macOS regardless of the operator's ``reyn.yaml`` setting.

Asymmetry observed 2026-05-23 during B52 W3-S5 retest:

* ``src/reyn/web/server.py``         — passes ``sandbox_config`` ✓ (cron path)
* ``src/reyn/cli/commands/chat.py``  — passes ``sandbox_config`` ✓ (= via _build_chat_session_kwargs)
* ``src/reyn/cli/commands/dogfood.py`` — passes ``sandbox_config`` ✓
* ``src/reyn/cli/commands/mcp.py``     — passes ``sandbox_config`` ✓
* ``src/reyn/web/deps.py``           — was MISSING; fixed in the same PR as this test.

The same asymmetry will reappear whenever a new ``ChatSession()`` call
site is added (= another HTTP gateway, a scheduled job, a daemon).
This test enumerates each known call site and asserts the
``sandbox_config`` kwarg is present, so drift is caught at PR-review
time rather than next dogfood batch.

Tier 2 because the cross-callsite invariant is an OS wiring rule the
sandbox-enforcement contract depends on: a regression silently
disables the operator's ``sandbox.backend: noop`` (or any explicit
selection) on the A2A surface.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "pyproject.toml").is_file():
            return ancestor
    raise RuntimeError("unable to locate repo root from " + str(here))


def _chat_session_calls_in(path: Path) -> list[ast.Call]:
    """Every ``ChatSession(...)`` call in the parsed source."""
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


# Mirrors test_session_factory_multimodal_config_uniform.py — keep in sync.
_CALL_SITE_FILES = [
    "src/reyn/cli/commands/chat.py",
    "src/reyn/cli/commands/dogfood.py",
    "src/reyn/cli/commands/mcp.py",
    "src/reyn/web/deps.py",
]


@pytest.mark.parametrize("rel_path", _CALL_SITE_FILES)
def test_chat_session_call_site_passes_sandbox_config(rel_path: str) -> None:
    """Tier 2: each known ``ChatSession()`` factory call passes
    ``sandbox_config``. A missing kwarg here silently disables the
    operator's declared sandbox backend on that surface (= reverts to
    auto-detect, picking Seatbelt on macOS regardless of reyn.yaml).
    """
    path = _repo_root() / rel_path
    assert path.is_file(), f"call-site file moved? {rel_path}"
    calls = _chat_session_calls_in(path)
    assert calls, f"no ChatSession(...) call found in {rel_path}"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
        assert "sandbox_config" in keywords, (
            f"{rel_path}:{call.lineno} ChatSession(...) is missing "
            "`sandbox_config=` kwarg. Add `sandbox_config=<source>` so "
            "the operator's reyn.yaml `sandbox.backend` declaration "
            "reaches the sandboxed_exec handler via the chat router."
        )


def test_no_unknown_chat_session_call_sites_for_sandbox_config() -> None:
    """Tier 2: every ``ChatSession(...)`` call site inside ``src/`` is
    covered by ``_CALL_SITE_FILES``.

    Mirrors the same enforcement used for ``multimodal_config``: a new
    surface (= different HTTP gateway, scheduled runner) must be added
    here in the same PR so the sandbox_config wiring stays uniform.
    """
    root = _repo_root()
    src = root / "src"
    expected = {(root / p).resolve() for p in _CALL_SITE_FILES}
    actual = set()
    for py in src.rglob("*.py"):
        if not _chat_session_calls_in(py):
            continue
        # Skip the ChatSession class definition itself.
        text = py.read_text(encoding="utf-8")
        if "class ChatSession" in text:
            continue
        actual.add(py.resolve())
    extras = actual - expected
    assert not extras, (
        "Unlisted ChatSession() call sites — add them to "
        "_CALL_SITE_FILES in this test in the same PR. Found: "
        + ", ".join(sorted(str(p.relative_to(root)) for p in extras))
    )
