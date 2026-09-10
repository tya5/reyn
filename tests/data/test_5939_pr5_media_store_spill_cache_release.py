"""Tier 2: #5939/#5851 PR-5 — ``MediaStore.clear_spill_path_cache``, the
owning-instance public clear function ``Session._drop_instance_caches``
delegates to, and the race it must not reopen.

Real ``MediaStore`` + real ``DurabilityWorker`` throughout (never a Mock),
matching ``test_media_store.py``'s own established discipline.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def _store(tmp_path: Path) -> MediaStore:
    return MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="pr5-agent",
        session_id="pr5-session",
    )


def test_clear_spill_path_cache_prunes_a_file_this_process_deleted(tmp_path: Path):
    """Tier 2: the genuine, previously-unpruned gap this method closes —
    neither eviction call site ever calls ``.discard()`` after
    ``path.unlink()``, so a self-deleted file stays tracked forever until
    this method (or a fresh construction) runs."""
    async def _body() -> None:
        store = _store(tmp_path)
        block_a = store.save_tool_result("body a", mime_type="text/plain", seq=1)
        block_b = store.save_tool_result("body b", mime_type="text/plain", seq=2)
        await store.flush()
        assert store.is_history_content_spill(block_a["path"])
        assert store.is_history_content_spill(block_b["path"])

        # Simulate what eviction does: unlink the file, WITHOUT telling
        # the tracking sets (the gap this method closes).
        (tmp_path / block_a["path"]).unlink()

        before, after = store.clear_spill_path_cache()

        assert before == 2
        assert after == 1, "the deleted file's entry must be pruned"
        assert not store.is_history_content_spill(block_a["path"]), (
            "a deleted file must no longer read as tracked"
        )
        assert store.is_history_content_spill(block_b["path"]), (
            "a file that still exists must NOT be pruned alongside it"
        )

    asyncio.run(_body())


def test_clear_spill_path_cache_does_not_drop_a_write_still_queued(tmp_path: Path):
    """Tier 2: the SECOND race (found during PR-5 implementation, #5851
    issue thread): a fresh ``save_tool_result`` write is fire-and-forget
    on ``MediaStore``'s own worker when a loop is running (every real
    chat turn) — the file does not exist on disk yet the INSTANT after
    the call returns, before the worker's background task has had any
    chance to run (no ``await`` crossed in between: this is a
    STRUCTURAL guarantee, not a timing race, because nothing yields the
    loop between the enqueue and this read).

    A NAIVE ``clear_spill_path_cache`` (checking ``path.exists()`` with
    no flush first) would wrongly prune this real, legitimate,
    still-queued entry — this test pins that ``Session._drop_instance_
    caches`` (not exercised directly here, see the session-level test)
    must flush FIRST. This file proves the race is real at the
    MediaStore layer: calling this method directly, with NO flush, right
    after the write, DOES lose the entry — the witness for why the flush
    in ``_drop_instance_caches`` is load-bearing, not decorative."""
    async def _body() -> None:
        store = _store(tmp_path)
        block = store.save_tool_result("body", mime_type="text/plain", seq=1)
        # NO flush here, on purpose -- the write is still queued.
        before, after = store.clear_spill_path_cache()
        assert before == 1
        assert after == 0, (
            "sanity: the race is real -- an unflushed write IS wrongly "
            "pruned by a naive exists()-only check with nothing awaited "
            "in between (this is the exact defect Session._drop_instance_"
            "caches's own flush()-first ordering exists to prevent)"
        )
        assert not store.is_history_content_spill(block["path"]), (
            "documents the wrongful loss this test exists to pin -- "
            "Session._drop_instance_caches must never reach this state"
        )
        # Let the queued write actually land so the worker doesn't warn
        # about an unflushed job at teardown.
        await store.flush()

    asyncio.run(_body())
