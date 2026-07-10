"""Tier 2: #2782 (path-locking step) — a per-path `asyncio.Lock`
(`core.op_runtime.path_locks`) serializes concurrent same-path file writes.

Background: #2794 offloaded `edit_file`'s read-modify-write into ONE
`asyncio.to_thread` job — atomic WITHIN a single op, since no `await` appears
between the read and the write (see `_execute_edit_sync`'s docstring in
`file.py`). But it removed the implicit cross-op serialization that
single-threaded, single-event-loop execution used to provide: TWO concurrent
`edit_file` ops on the SAME path now run their read-modify-write in DIFFERENT
worker threads — both read the pre-edit content, both compute independently,
both write — and whichever writes last wins outright, silently discarding the
other op's edit (a classic read-modify-write lost-update race). Concrete
reachable paths: pipeline `parallel`/`for_each` fan-out, concurrent
sub-agents, concurrent A2A sessions sharing a `base_dir`.

`test_file_ops_offload_2782.py` (the #2794 PR) explicitly deferred this case:
"Path-locking ... is explicitly DEFERRED ... not tested here." This module
is that follow-up.

Real `Workspace`/`EventLog`/`OpContext`/`file.handle`/`asyncio.gather`
throughout — no mocks, no `unittest.mock`. `file.handle` is the actual
production op-dispatch entry point (the same function every tool-catalog
frontend — router `edit_file`/`write_file`, phase `file` op — ultimately
calls; see `test_op_runtime_file_mkdir_move_stat.py` for the same direct-call
convention). The only "fake" is a `threading.Barrier` wrapped around
`Workspace.read_file_bytes` in the primary guard test, used purely to force a
DETERMINISTIC race window (both threads' reads land before either write)
instead of relying on OS thread-scheduling luck — this is standard practice
for reproducing a race condition on demand, not a mock of a collaborator: the
real `read_file_bytes` still runs, wrapped, not replaced.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.file import handle
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"file.read": "allow", "file.write": "allow"},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=_resolver(tmp_path), base_dir=tmp_path)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=_resolver(tmp_path),
        actor="test_skill",
    )


def test_concurrent_edit_file_same_path_both_edits_survive(tmp_path):
    """Tier 2: #2782 — the load-bearing guard. Two concurrent `edit` ops on
    the SAME path, making DIFFERENT edits, launched via `asyncio.gather`
    through the real op-dispatch path (`file.handle` -> `_execute_edit` ->
    `asyncio.to_thread(_execute_edit_sync, ...)`). Both edits must survive in
    the final on-disk content — no lost update.

    A `threading.Barrier(2, timeout=...)` wraps `Workspace.read_file_bytes`
    so BOTH worker threads' reads are forced to land before either write
    proceeds — this is the exact interleaving the lost-update race requires.
    With the per-path lock in place, the second op's `to_thread` job cannot
    even START until the first op's lock is released (after ITS write has
    landed), so the barrier's second party never arrives in time; it times
    out (`BrokenBarrierError`, caught) and each op proceeds having read
    strictly AFTER the other's write — genuinely serialized, not merely
    lucky timing.
    """
    (tmp_path / "shared.txt").write_text("AAA line\nBBB line\n")
    ctx = _ctx(tmp_path)

    barrier = threading.Barrier(2)
    orig_read = ctx.workspace.read_file_bytes

    def _synced_read(path_str):
        data = orig_read(path_str)
        try:
            barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return data

    ctx.workspace.read_file_bytes = _synced_read

    op_a = FileIROp(kind="file", op="edit", path="shared.txt", old_string="AAA", new_string="aaa")
    op_b = FileIROp(kind="file", op="edit", path="shared.txt", old_string="BBB", new_string="bbb")

    async def _run():
        return await asyncio.gather(handle(op_a, ctx), handle(op_b, ctx))

    result_a, result_b = asyncio.run(_run())

    assert result_a["status"] == "ok", result_a
    assert result_b["status"] == "ok", result_b
    final = (tmp_path / "shared.txt").read_text()
    assert "aaa" in final, f"lost update: edit A's change is missing from final content {final!r}"
    assert "bbb" in final, f"lost update: edit B's change is missing from final content {final!r}"
    assert "AAA" not in final and "BBB" not in final, (
        f"an edit target string survived unreplaced — a partial/corrupted merge: {final!r}"
    )


def test_concurrent_edit_and_write_same_path_no_lost_update(tmp_path):
    """Tier 2: #2782 fix-class completeness — `write` is a DIFFERENT op kind
    than `edit` but mutates the same path, so it must acquire the SAME
    per-path lock. A concurrent `edit` (read-modify-write, offloaded to a
    worker thread) and `write` (blind overwrite, on the event-loop thread) on
    the SAME path must not interleave: whichever runs second must see a
    coherent full result, never a torn merge of the two."""
    (tmp_path / "shared2.txt").write_text("original\n")
    ctx = _ctx(tmp_path)

    op_edit = FileIROp(kind="file", op="edit", path="shared2.txt", old_string="original", new_string="edited")
    op_write = FileIROp(kind="file", op="write", path="shared2.txt", content="replaced\n")

    async def _run():
        return await asyncio.gather(handle(op_edit, ctx), handle(op_write, ctx))

    result_edit, result_write = asyncio.run(_run())

    # Whichever op the runtime happens to serialize first, EACH op must see a
    # coherent, fully-applied outcome: the write is unconditional
    # (`status == "ok"`) and the edit either succeeds against "original" (it
    # ran first) or fails cleanly with `old_string not found` (it ran second,
    # against "replaced\n" — never a torn/partial read).
    assert result_write["status"] == "ok", result_write
    assert result_edit["status"] in ("ok", "error"), result_edit
    final = (tmp_path / "shared2.txt").read_text()
    assert final in ("edited\n", "replaced\n"), (
        f"final content must be a CLEAN result of one full op, not a torn merge: {final!r}"
    )


def test_move_reversed_src_dest_does_not_deadlock(tmp_path):
    """Tier 2: #2782 — `move` acquires locks on BOTH its source and dest path
    via `path_locks.locked_paths`, which sorts paths (a FIXED global order)
    before acquiring them. Two concurrent moves over the SAME two paths with
    REVERSED src/dest (`a.txt`->`b.txt` concurrently with `b.txt`->`a.txt`)
    are the classic two-lock deadlock shape: naive src-then-dest acquisition
    would have op1 hold `a.txt` while waiting on `b.txt`, and op2 hold `b.txt`
    while waiting on `a.txt` — neither can ever proceed. Sorting first means
    BOTH ops always take `a.txt` before `b.txt` regardless of which is
    "source" — no deadlock, whichever move lands first (the runtime makes no
    ordering promise between two concurrent ops on overlapping paths, only
    mutual exclusion). Bounded via `asyncio.wait_for` so a regression back to
    unordered acquisition fails as a timeout, not a silent hang."""
    (tmp_path / "a.txt").write_text("A\n")
    (tmp_path / "b.txt").write_text("B\n")
    ctx = _ctx(tmp_path)

    op_1 = FileIROp(kind="file", op="move", path="a.txt", dest_path="b.txt")
    op_2 = FileIROp(kind="file", op="move", path="b.txt", dest_path="a.txt")

    async def _run():
        return await asyncio.wait_for(asyncio.gather(handle(op_1, ctx), handle(op_2, ctx)), timeout=5.0)

    result_1, result_2 = asyncio.run(_run())
    # The point is completion without deadlock — whichever move the runtime
    # happens to serialize first relocates its source out from under the
    # other, so the second observes `not_found`; either outcome is a clean
    # completion, not a hang.
    assert result_1["status"] in ("ok", "not_found"), result_1
    assert result_2["status"] in ("ok", "not_found"), result_2
