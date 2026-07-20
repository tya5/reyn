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

Also verifies a scope-critical, easy-to-miss requirement, caught in review
(architect co-vet on the original PR) as a TRANSITIVE emit this module's
tests initially missed: `Workspace.write_file_bytes` itself unconditionally
emitted `workspace_updated` — called from INSIDE `_execute_edit_sync`
(the threaded core), so the explicit `ctx.events.emit("tool_executed", ...)`
being correctly deferred wasn't sufficient; the transitive one inside
`write_file_bytes` still fired off-loop. The mechanism is subtler than "it
would crash": a worker-thread emit reaching `EventStore.write` calls
`asyncio.get_running_loop()`, which RAISES off-loop, falling to a
non-serialized sync-fallback write path that mutates `EventStore`'s
rotation state without the loop-thread-only serialization protecting it —
racing the DurabilityWorker's own writes to the same JSONL file (a
data-integrity hazard, not a crash, so a `bare EventLog()` with no
subscriber — as this module's earlier tests used — can never catch it: the
race lives inside `EventStore`, which only exists once actually subscribed).
Fixed via `write_file_bytes(..., emit=False)` from the threaded core, with
the async wrapper emitting `workspace_updated` itself after `to_thread`
returns, mirroring `tool_executed`.

Real `Workspace`/`EventLog`/`invoke_tool` throughout — no mocks. The
thread-identity wiring test below subscribes a REAL `EventStore` (not a bare
`EventLog`) so a transitive off-loop emit is actually observable.

Path-locking (serializing two SEPARATE to_thread jobs against the same path,
for same-process concurrent-session edits) is explicitly DEFERRED — owner GO
covers option (a) only (see issue #2782); not tested here.
"""
from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

from reyn.core.events.event_store import EventStore
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
    # #2998: the op_runtime glob handler now calls `glob_files_with_total`
    # (not `glob_files`) so it can also report the pre-cap match total —
    # same to_thread-wrapped call site, different Workspace method.
    orig = ws.glob_files_with_total

    def _spy(*args, **kwargs):
        seen_thread["ident"] = threading.current_thread().ident
        return orig(*args, **kwargs)

    ws.glob_files_with_total = _spy
    ctx = ToolContext(
        events=events, permission_resolver=_resolver(tmp_path), workspace=ws,
        caller_kind="router", router_state=RouterCallerState(),
    )
    result = asyncio.run(invoke_tool(get_default_registry(), "glob_files", {"pattern": "*.txt"}, ctx))
    assert result["status"] == "ok"
    assert seen_thread["ident"] != caller_thread, (
        "glob_files must run its directory walk on a worker thread (#2782)"
    )


def test_edit_file_never_emits_from_a_non_loop_thread(tmp_path, monkeypatch):
    """Tier 2: #2782 — the load-bearing wiring test (architect co-vet finding):
    edit_file must not emit ANY event — not just `tool_executed`, but the
    TRANSITIVE `workspace_updated` emitted by `Workspace.write_file_bytes` —
    from a non-loop thread. Subscribes a REAL `EventStore` (not a bare
    `EventLog`, which cannot observe this: the race lives inside
    `EventStore.write`'s off-loop fallback path, not in `EventLog.emit`
    itself). A spy wraps `EventStore.write` and records which thread called
    it; every call must match the calling (loop) thread's identity.

    Before the emit-split fix landed for `write_file_bytes`, this failed:
    `workspace_updated` fired from the `to_thread` worker thread."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "w.txt").write_text("hello world\n")

    events = EventLog()
    store = EventStore(tmp_path / "events")
    events.add_subscriber(store)
    ws = Workspace(events=events)
    ctx = ToolContext(
        events=events, permission_resolver=_resolver(tmp_path), workspace=ws,
        caller_kind="router", router_state=RouterCallerState(),
    )

    caller_thread = threading.current_thread().ident
    seen_threads: list[int | None] = []
    orig_write = store.write

    def _spy_write(event):
        seen_threads.append(threading.current_thread().ident)
        return orig_write(event)

    monkeypatch.setattr(store, "write", _spy_write)

    result = asyncio.run(invoke_tool(get_default_registry(), "edit_file",
                                     {"path": "w.txt", "old_string": "hello", "new_string": "hi"}, ctx))
    asyncio.run(store.flush())

    assert result["status"] == "ok"
    assert seen_threads, "EventStore.write must have been called (workspace_updated + tool_executed)"
    assert all(t == caller_thread for t in seen_threads), (
        "EventStore.write must NEVER be called from a non-loop thread — a worker-thread call "
        "falls to EventStore's non-serialized sync-fallback path, racing the DurabilityWorker's "
        "own writes to the same file (#2782 architect co-vet finding)"
    )
