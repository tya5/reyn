"""Tier 2: #1466 — file__write success includes size feedback.

write-success result gains ``bytes_written`` (always) and
``previous_size_bytes`` (overwrite only). New-file writes carry only
``bytes_written``; no content preview (token-bloat avoidance).

Zero extra I/O: ``previous_size_bytes`` reuses the #1452 encoding
pre-read (``read_file_bytes`` already called before the write).

Real Workspace + real op_runtime / registry — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.data.workspace.workspace import Workspace
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    return ToolContext(
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={"file.read": "allow", "file.write": "allow"},
            project_root=tmp_path,
            interactive=False,
        ),
        workspace=Workspace(events=events, base_dir=tmp_path),
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _write(tmp_path: Path, args: dict) -> dict:
    return asyncio.run(invoke_tool(
        get_default_registry(), "write_file", args, _ctx(tmp_path),
    ))


# ── 1. New file — bytes_written only ────────────────────────────────────────


def test_new_file_has_bytes_written() -> None:
    """Tier 2: #1466 — writing a new file returns bytes_written (UTF-8 byte count
    of the written content). No previous_size_bytes on a new file."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        tmp = Path(td)
        content = "hello world\n"
        result = _write(tmp, {"path": "new.txt", "content": content})
        assert result["status"] == "ok"
        assert result["bytes_written"] == len(content.encode("utf-8"))
        assert "previous_size_bytes" not in result


def test_new_file_bytes_written_multibyte() -> None:
    """Tier 2: #1466 — bytes_written reflects UTF-8 byte count, not char count.
    Non-ASCII content has bytes_written > len(content)."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path
        tmp = Path(td)
        content = "こんにちは\n"  # 5 chars, 16 bytes UTF-8
        result = _write(tmp, {"path": "jp.txt", "content": content})
        assert result["status"] == "ok"
        assert result["bytes_written"] == len(content.encode("utf-8"))
        assert result["bytes_written"] > len(content)
        assert "previous_size_bytes" not in result


# ── 2. Overwrite — both bytes_written and previous_size_bytes ────────────────


def test_overwrite_has_both_size_fields(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #1466 — overwriting an existing file returns both bytes_written
    (new size) and previous_size_bytes (old size). Regression pin: both fields
    must be present and be exact byte counts."""
    monkeypatch.chdir(tmp_path)
    old_content = "old content here\n"
    (tmp_path / "f.txt").write_bytes(old_content.encode("utf-8"))
    new_content = "new content\n"
    result = _write(tmp_path, {"path": "f.txt", "content": new_content})
    assert result["status"] == "ok"
    assert result["bytes_written"] == len(new_content.encode("utf-8"))
    assert result["previous_size_bytes"] == len(old_content.encode("utf-8"))


def test_overwrite_no_content_preview(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #1466 — write result must NOT include a content preview (token
    bloat avoidance; size numbers alone are the signal)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_bytes(b"old")
    result = _write(tmp_path, {"path": "f.txt", "content": "new\n"})
    assert result["status"] == "ok"
    assert "content" not in result
    assert "preview" not in result


# ── 3. Other shape regression pins ──────────────────────────────────────────


def test_write_result_core_fields_present(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #1466 — the core result shape (kind/op/path/status) is unchanged
    by the size-feedback addition."""
    monkeypatch.chdir(tmp_path)
    result = _write(tmp_path, {"path": "x.txt", "content": "abc"})
    assert result["kind"] == "file"
    assert result["op"] == "write"
    assert result["path"] == "x.txt"
    assert result["status"] == "ok"


def test_overwrite_size_differential(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: #1466 — falsification pair: after a large→small overwrite,
    previous_size_bytes > bytes_written (the shrink is visible in the result)."""
    monkeypatch.chdir(tmp_path)
    large = "x" * 1000
    small = "y" * 10
    (tmp_path / "f.txt").write_bytes(large.encode("utf-8"))
    result = _write(tmp_path, {"path": "f.txt", "content": small})
    assert result["status"] == "ok"
    assert result["previous_size_bytes"] == 1000
    assert result["bytes_written"] == 10
    assert result["previous_size_bytes"] > result["bytes_written"]
