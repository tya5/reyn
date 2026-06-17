"""Tier 2: GREP_FILES and GLOB_FILES ToolDefinition end-to-end invariants.

Tests verify that:
  1. GREP_FILES handler returns expected matches from real workspace files.
  2. GLOB_FILES handler returns expected file paths from real workspace files.
  3. Both ToolDefinitions have correct gates, purity, and category.
  4. Both are reachable via get_default_registry() round-trip.
  5. Routing rules exist in _OPERATION_RULES for file__grep and file__glob.

No MagicMock / AsyncMock. All tests use real ToolDefinition instances,
real Workspace, real EventLog, and the fallback ToolContext path
(permission_resolver=None → minimal OpContext synthesis inside the handler).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.tools.file import (  # noqa: F401 (ToolRegistry imported for type check)
    GLOB_FILES,
    GREP_FILES,
)
from reyn.tools.types import ToolContext

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_workspace(tmp_path: Path) -> Workspace:
    """Build a real Workspace rooted at tmp_path."""
    events = EventLog()
    ws = Workspace(events=events)
    # Patch base_dir so file resolution uses tmp_path as project root.
    ws.base_dir = tmp_path
    return ws


def _make_ctx(tmp_path: Path, monkeypatch) -> ToolContext:
    """Build a minimal ToolContext backed by a real Workspace.

    permission_resolver=None triggers the fallback OpContext synthesis path
    inside _build_legacy_op_context so Workspace CWD resolution applies.
    """
    monkeypatch.chdir(tmp_path)
    ws = _make_workspace(tmp_path)
    events = EventLog()
    return ToolContext(
        caller_kind="router",
        events=events,
        permission_resolver=None,
        workspace=ws,
    )


def _run(coro):
    return asyncio.run(coro)


# ── 1. GREP_FILES handler end-to-end ─────────────────────────────────────────


def test_grep_files_returns_match_in_file(tmp_path, monkeypatch):
    """Tier 2: grep_files handler finds a pattern in a real file.

    Writes a file with a known string, runs grep_files, and checks
    that the match is returned with path, line_number, and content.
    No mocks.
    """
    target = tmp_path / "hello.txt"
    target.write_text("first line\nhello world\nthird line\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GREP_FILES.handler({"pattern": "hello", "path": "."}, ctx))

    assert result.get("status") == "ok"
    matches = result.get("matches", [])
    assert len(matches) >= 1
    paths_found = [m["path"] for m in matches]
    assert any("hello.txt" in p for p in paths_found), (
        f"Expected 'hello.txt' in match paths, got: {paths_found}"
    )
    # The matching line content should contain 'hello'
    matched_line = next(m for m in matches if "hello.txt" in m["path"])
    assert "hello" in matched_line["content"]
    assert matched_line["line_number"] == 2


def test_grep_files_no_match_returns_empty(tmp_path, monkeypatch):
    """Tier 2: grep_files returns status=ok with count=0 when pattern not found."""
    target = tmp_path / "nope.txt"
    target.write_text("apple banana cherry\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GREP_FILES.handler({"pattern": "XYZZY_NOT_FOUND", "path": "."}, ctx))

    assert result.get("status") == "ok"
    assert result.get("count", 0) == 0
    assert result.get("matches", []) == []


def test_grep_files_glob_filter_limits_files(tmp_path, monkeypatch):
    """Tier 2: grep_files respects glob filter — only .py files searched."""
    py_file = tmp_path / "code.py"
    py_file.write_text("def hello(): pass\n", encoding="utf-8")
    txt_file = tmp_path / "notes.txt"
    txt_file.write_text("hello world\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(
        GREP_FILES.handler({"pattern": "hello", "path": ".", "glob": "*.py"}, ctx)
    )

    assert result.get("status") == "ok"
    match_paths = [m["path"] for m in result.get("matches", [])]
    assert all("code.py" in p or ".py" in p for p in match_paths), (
        f"Glob filter should have excluded .txt files; got paths: {match_paths}"
    )


def test_grep_files_case_insensitive_default(tmp_path, monkeypatch):
    """Tier 2: grep_files is case-insensitive by default (case_sensitive absent)."""
    f = tmp_path / "mixed.txt"
    f.write_text("HELLO world\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    # Searching lowercase 'hello' should match uppercase 'HELLO' (default: case-insensitive)
    result = _run(GREP_FILES.handler({"pattern": "hello", "path": "."}, ctx))

    assert result.get("status") == "ok"
    assert result.get("count", 0) >= 1


def test_grep_files_case_sensitive_excludes_mismatch(tmp_path, monkeypatch):
    """Tier 2: grep_files with case_sensitive=true does not match differently-cased text."""
    f = tmp_path / "upper.txt"
    f.write_text("HELLO world\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(
        GREP_FILES.handler(
            {"pattern": "hello", "path": ".", "case_sensitive": True}, ctx
        )
    )

    assert result.get("status") == "ok"
    # Exact-case 'hello' should NOT match 'HELLO'
    assert result.get("count", 0) == 0


def test_grep_files_max_results_caps_matches(tmp_path, monkeypatch):
    """Tier 2: grep_files max_results caps the number of returned matches."""
    f = tmp_path / "many.txt"
    # Write 20 lines each matching 'hit'
    f.write_text("\n".join(f"hit line {i}" for i in range(20)) + "\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(
        GREP_FILES.handler({"pattern": "hit", "path": ".", "max_results": 5}, ctx)
    )

    assert result.get("status") == "ok"
    assert len(result.get("matches", [])) <= 5


# ── 2. GLOB_FILES handler end-to-end ─────────────────────────────────────────


def test_glob_files_returns_py_files(tmp_path, monkeypatch):
    """Tier 2: glob_files handler returns .py files matching **/*.py.

    Creates two .py files and one .txt file, runs glob_files, and checks
    that only .py paths appear in the result. No mocks.
    """
    (tmp_path / "a.py").write_text("# a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("# b\n", encoding="utf-8")
    (tmp_path / "c.txt").write_text("c\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "**/*.py"}, ctx))

    matches = result.get("matches", [])
    for m in matches:
        assert m.endswith(".py"), f"Expected only .py paths, got: {m}"


def test_glob_files_count_equals_matches_length(tmp_path, monkeypatch):
    """Tier 2: glob_files count field equals len(matches)."""
    (tmp_path / "x.md").write_text("# x\n", encoding="utf-8")
    (tmp_path / "y.md").write_text("# y\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "*.md"}, ctx))

    matches = result.get("matches", [])
    assert result.get("count") == len(matches)


def test_glob_files_no_match_returns_empty(tmp_path, monkeypatch):
    """Tier 2: glob_files returns empty matches list when no files match."""
    (tmp_path / "only.txt").write_text("text\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    result = _run(GLOB_FILES.handler({"pattern": "**/*.nonexistent_ext_xyz"}, ctx))

    assert result.get("matches", []) == []
    assert result.get("count", 0) == 0


def test_glob_files_with_path_prefix(tmp_path, monkeypatch):
    """Tier 2: glob_files respects path arg as subdirectory root."""
    subdir = tmp_path / "src"
    subdir.mkdir()
    (subdir / "module.py").write_text("# module\n", encoding="utf-8")
    (tmp_path / "root.py").write_text("# root\n", encoding="utf-8")

    ctx = _make_ctx(tmp_path, monkeypatch)
    # Only search inside src/
    result = _run(GLOB_FILES.handler({"pattern": "*.py", "path": "src"}, ctx))

    matches = result.get("matches", [])
    assert any("module.py" in m for m in matches), (
        f"Expected module.py in src/ matches, got: {matches}"
    )


# ── 3. Gate / purity / category invariants ────────────────────────────────────


def test_grep_files_gates():
    """Tier 2: GREP_FILES has gates.router='allow' and gates.phase='allow'."""
    assert GREP_FILES.gates.router == "allow"
    assert GREP_FILES.gates.phase == "allow"


def test_glob_files_gates():
    """Tier 2: GLOB_FILES has gates.router='allow' and gates.phase='allow'."""
    assert GLOB_FILES.gates.router == "allow"
    assert GLOB_FILES.gates.phase == "allow"


def test_grep_files_purity_read_only():
    """Tier 2: GREP_FILES purity is 'read_only' — no workspace side effect."""
    assert GREP_FILES.purity == "read_only"


def test_glob_files_purity_read_only():
    """Tier 2: GLOB_FILES purity is 'read_only' — no workspace side effect."""
    assert GLOB_FILES.purity == "read_only"


def test_grep_files_category_io():
    """Tier 2: GREP_FILES category is 'io'."""
    assert GREP_FILES.category == "io"


def test_glob_files_category_io():
    """Tier 2: GLOB_FILES category is 'io'."""
    assert GLOB_FILES.category == "io"


# ── 4. Registry round-trip ────────────────────────────────────────────────────


def test_grep_files_in_default_registry():
    """Tier 2: grep_files is findable in get_default_registry()."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    tool = registry.lookup("grep_files")
    assert tool is not None
    assert tool.name == "grep_files"


def test_glob_files_in_default_registry():
    """Tier 2: glob_files is findable in get_default_registry()."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    tool = registry.lookup("glob_files")
    assert tool is not None
    assert tool.name == "glob_files"


def test_grep_files_in_registry_for_router():
    """Tier 2: grep_files appears in registry.for_router() (gates.router=allow)."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    router_names = {t.name for t in registry.for_router()}
    assert "grep_files" in router_names


def test_glob_files_in_registry_for_router():
    """Tier 2: glob_files appears in registry.for_router() (gates.router=allow)."""
    from reyn.tools import get_default_registry

    registry = get_default_registry()
    router_names = {t.name for t in registry.for_router()}
    assert "glob_files" in router_names


# ── 5. _OPERATION_RULES routing rules ────────────────────────────────────────


def test_file_grep_routing_rule_exists():
    """Tier 2: _OPERATION_RULES contains file__grep → grep_files."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    assert "file__grep" in _OPERATION_RULES
    target_name, _ = _OPERATION_RULES["file__grep"]
    assert target_name == "grep_files"


def test_file_glob_routing_rule_exists():
    """Tier 2: _OPERATION_RULES contains file__glob → glob_files."""
    from reyn.tools.universal_dispatch import _OPERATION_RULES

    assert "file__glob" in _OPERATION_RULES
    target_name, _ = _OPERATION_RULES["file__glob"]
    assert target_name == "glob_files"
