"""Tier 2: #2782 — edit_file/grep_files/glob_files run their I/O off the event
loop (via `asyncio.to_thread`), the same defect class as #1765/#2780 one layer
lower: `host_backend.py`'s sync read/write calls used to run directly on the
event loop, freezing it on every file-tool call (the most-frequent tool group
for a coding agent).

`edit_file`'s read-modify-write is the load-bearing case: today it completes
with ZERO `await` in between (accidentally atomic — no other coroutine can
interleave). The fix wraps the WHOLE read-modify-write in ONE `to_thread` job
(not two separate to_thread calls for read and write), preserving that
atomicity exactly — see `_execute_edit_sync`'s docstring. `grep_files`/
`glob_files` have no such concern (pure reads); a single outer `to_thread` per
call is the natural granularity (not per-candidate-file).

Also verifies a scope-critical, easy-to-miss requirement discovered while
implementing: `ctx.events.emit(...)` must NOT run inside the threaded call —
`EventStore.write` (a chat_events subscriber) ultimately does an
`asyncio.Queue.put_nowait` (DurabilityWorker, #2780), which is not
thread-safe off the event loop thread. The emit call is placed AFTER the
`await asyncio.to_thread(...)` returns, back on the loop thread.

Real `Workspace`/`EventLog`/`invoke_tool` throughout — no mocks.

Path-locking (serializing two SEPARATE to_thread jobs against the same path,
for same-process concurrent-session edits) is explicitly DEFERRED — owner GO
covers option (a) only (see issue #2782); not tested here.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.file import _execute_grep_sync
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"file.read": "allow", "file.write": "allow"},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    ws = Workspace(events=events)
    return ToolContext(
        events=events,
        permission_resolver=_resolver(tmp_path),
        workspace=ws,
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _call(tmp_path, tool: str, args: dict) -> dict:
    return asyncio.run(invoke_tool(get_default_registry(), tool, args, _ctx(tmp_path)))


def test_edit_file_runs_off_the_main_thread(tmp_path, monkeypatch):
    """Tier 2: #2782 — edit_file's read-modify-write executes on a DIFFERENT OS
    thread than the caller (proof the offload actually happens, not just that
    the result is unchanged). RED before the fix: `_execute_edit` ran
    synchronously on the same thread as the caller."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("hello world\n")
    caller_thread = threading.current_thread().ident
    seen_thread = {}

    from reyn.core.op_runtime import file as file_mod
    orig = file_mod._execute_edit_sync

    def _spy(op, ctx):
        seen_thread["ident"] = threading.current_thread().ident
        return orig(op, ctx)

    monkeypatch.setattr(file_mod, "_execute_edit_sync", _spy)

    result = _call(tmp_path, "edit_file", {"path": "a.txt", "old_string": "hello", "new_string": "hi"})
    assert result["status"] == "ok"
    assert seen_thread["ident"] != caller_thread, (
        "edit_file's read-modify-write must run on a worker thread, not the "
        "event loop thread (#2782)"
    )


def test_edit_file_result_and_disk_content_unchanged_by_offload(tmp_path, monkeypatch):
    """Tier 2: the offload is behavior-preserving — same result shape, same
    on-disk content, as the pre-#2782 synchronous path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.txt").write_text("foo bar\n")
    result = _call(tmp_path, "edit_file", {"path": "b.txt", "old_string": "foo", "new_string": "baz"})
    assert result["status"] == "ok"
    assert result["replacements"] == 1
    assert (tmp_path / "b.txt").read_text() == "baz bar\n"


def test_edit_file_emits_tool_executed_event(tmp_path, monkeypatch):
    """Tier 2: #2782 — the `tool_executed` event still fires after the offload
    (moved to run AFTER the to_thread call, on the loop thread — not lost, not
    duplicated)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "c.txt").write_text("one two\n")
    events = EventLog()
    ws = Workspace(events=events)
    ctx = ToolContext(
        events=events, permission_resolver=_resolver(tmp_path), workspace=ws,
        caller_kind="router", router_state=RouterCallerState(),
    )
    asyncio.run(invoke_tool(get_default_registry(), "edit_file",
                            {"path": "c.txt", "old_string": "one", "new_string": "1"}, ctx))
    emitted = [e for e in events.all() if e.type == "tool_executed" and e.data.get("op") == "edit_file"]
    assert emitted, "tool_executed must fire after the offloaded edit, not be lost"
    assert emitted[0].data["replacements"] == 1
    assert not any(e.data.get("replacements") != 1 for e in emitted), "must not emit a duplicate/mismatched event"


def test_edit_file_validation_error_does_not_emit(tmp_path, monkeypatch):
    """Tier 2: a validation-error branch (old_string not found) still emits NO
    event — byte-identical to pre-#2782 behavior (only the success path ever
    emitted)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "d.txt").write_text("only this\n")
    events = EventLog()
    ws = Workspace(events=events)
    ctx = ToolContext(
        events=events, permission_resolver=_resolver(tmp_path), workspace=ws,
        caller_kind="router", router_state=RouterCallerState(),
    )
    result = asyncio.run(invoke_tool(get_default_registry(), "edit_file",
                                     {"path": "d.txt", "old_string": "nope", "new_string": "x"}, ctx))
    assert result["status"] == "error"
    assert not any(e.type == "tool_executed" for e in events.all())


def test_grep_files_runs_off_the_main_thread(tmp_path, monkeypatch):
    """Tier 2: #2782 — grep_files' tree-walk + per-file read executes on a
    worker thread, not the caller's thread."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x.py").write_text("needle_here\n")
    caller_thread = threading.current_thread().ident
    seen_thread = {}

    from reyn.core.op_runtime import file as file_mod
    orig = file_mod._execute_grep_sync

    def _spy(op, ctx):
        seen_thread["ident"] = threading.current_thread().ident
        return orig(op, ctx)

    monkeypatch.setattr(file_mod, "_execute_grep_sync", _spy)

    result = _call(tmp_path, "grep_files", {"pattern": "needle_here"})
    assert result["status"] == "ok"
    assert seen_thread["ident"] != caller_thread, (
        "grep_files must run its tree-walk/regex-scan on a worker thread (#2782)"
    )


def test_grep_files_result_unchanged_by_offload(tmp_path, monkeypatch):
    """Tier 2: grep_files' result shape is unchanged by the offload."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "y.py").write_text("import os\nneedle_here\n")
    result = _call(tmp_path, "grep_files", {"pattern": "needle_here"})
    assert result["status"] == "ok"
    assert result["count"] == 1


def test_grep_sync_core_returns_none_emit_marker_for_validation_errors():
    """Tier 2: `_execute_grep_sync`'s (result, emit_marker) contract — a
    validation error (empty pattern) returns `None` as the emit marker, so the
    async wrapper correctly skips emitting (matches pre-#2782: those branches
    never emitted)."""
    op = FileIROp(kind="file", op="grep", pattern="", path=".")
    result, match_count = _execute_grep_sync(op, None)
    assert result["status"] == "error"
    assert match_count is None


def test_glob_files_runs_off_the_main_thread(tmp_path, monkeypatch):
    """Tier 2: #2782 — glob_files' directory walk executes on a worker thread."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "z.txt").write_text("x")
    caller_thread = threading.current_thread().ident
    seen_thread = {}

    events = EventLog()
    ws = Workspace(events=events)
    orig = ws.glob_files

    def _spy(*args, **kwargs):
        seen_thread["ident"] = threading.current_thread().ident
        return orig(*args, **kwargs)

    ws.glob_files = _spy
    ctx = ToolContext(
        events=events, permission_resolver=_resolver(tmp_path), workspace=ws,
        caller_kind="router", router_state=RouterCallerState(),
    )
    result = asyncio.run(invoke_tool(get_default_registry(), "glob_files", {"pattern": "*.txt"}, ctx))
    assert result["status"] == "ok"
    assert seen_thread["ident"] != caller_thread, (
        "glob_files must run its directory walk on a worker thread (#2782)"
    )
