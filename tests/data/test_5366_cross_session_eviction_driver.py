"""Tier 2: #5366 §3 — the project-wide GC's own DRIVER
(``MediaStore._evict_cross_session_over_cap`` / ``cross_session_eviction_
preview``), driven through a real ``save_tool_result`` write, not called
directly.

Real on-disk writes via real ``MediaStore`` instances (multiple agents),
same idiom as ``test_5366_cross_session_eviction_candidates.py`` (this
file's own sibling, which pins the candidate-LISTING half; this one pins
the eviction/refuse/preview half that consumes it).
"""
from __future__ import annotations

import os

import pytest

from reyn.config.infra import StorageConfig
from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    MediaStoreWriteUnavailable,
    history_content_root_for,
)


def _bump_mtime_forward(directory) -> None:
    """Same determinism helper the candidate-listing sibling test uses —
    forces every existing file further into the past so write-order
    ties never race real filesystem mtime-tick granularity."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


def _dir_total_bytes(directory) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def test_max_bytes_none_never_evicts(tmp_path):
    """Tier 2: non-vacuity control — StorageConfig's own default
    (max_bytes=None) means cross-session GC never even LOOKS, regardless
    of how much is on disk. Without this control, a bug that always
    skips eviction would pass every other test in this file for the
    wrong reason (nothing to evict) rather than the right one (told not
    to)."""
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=StorageConfig(max_bytes=None),
    )
    alice.save_tool_result("x" * 500, mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=StorageConfig(max_bytes=None),
    )
    # Would exceed any small cap, but max_bytes is None -> no check at all.
    bob.save_tool_result("y" * 500, mime_type="text/plain", seq=1)

    root = history_content_root_for(tmp_path)
    remaining = [p for p in root.rglob("*") if p.is_file()]
    # unpack-enforcement idiom: raises ValueError if not exactly 2 —
    # never a bare len() == N check.
    _alice_file, _bob_file = remaining


def test_a_later_write_evicts_an_older_other_agents_file_once_over_cap(tmp_path):
    """Tier 2: #5366 §3's own witness. This is a PRE-write check (same
    shape as the ``durability_failed`` gate right above its own call
    site) — it evicts to make room for whatever is ALREADY on disk
    before this write lands, it does not project this write's own
    upcoming size. So the write that FIRST pushes the project over cap
    is allowed through (the project was under cap at the moment its own
    pre-check ran); the NEXT write's pre-check is what observes the
    now-over-cap state and evicts agent A's OLDER file first
    (cross_session_eviction_candidates' own oldest-first order), even
    though the evicting write never touched A's directory."""
    storage = StorageConfig(max_bytes=600)
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_block = alice.save_tool_result("a" * 500, mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    # Pre-check sees 500 <= 600 (alice's file alone) -> allowed through.
    # After this write lands the project is ~1000 bytes, over cap, but
    # nothing re-checks until the NEXT write's own pre-check.
    bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)
    assert (tmp_path / alice_block["path"]).resolve().exists(), (
        "setup: alice's file must still be there right after bob's "
        "FIRST write — that write's own pre-check ran before it landed"
    )
    _bump_mtime_forward(history_content_root_for(tmp_path))

    # This second write's pre-check now sees the project over cap and
    # evicts the oldest non-pinned candidate (alice's file) first.
    bob.save_tool_result("c" * 10, mime_type="text/plain", seq=2)

    root = history_content_root_for(tmp_path)
    alice_path = (tmp_path / alice_block["path"]).resolve()
    assert not alice_path.exists(), (
        "alice's older file must have been evicted by the next write's "
        "own pre-check once the project was observably over cap"
    )
    assert _dir_total_bytes(root) <= storage.max_bytes


def test_pinned_agent_survives_cross_session_eviction(tmp_path):
    """Tier 2: acceptance — a pinned agent's file is never evicted by the
    driver, even when it is the oldest and the project is over cap."""
    storage = StorageConfig(max_bytes=600, pin=["alice"])
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_block = alice.save_tool_result("a" * 500, mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)

    alice_path = (tmp_path / alice_block["path"]).resolve()
    assert alice_path.exists(), (
        "a pinned agent's file must survive cross-session eviction even "
        "while the project stays over cap"
    )


def test_raises_write_unavailable_when_candidates_exhausted_still_over_cap(tmp_path):
    """Tier 2: #5366 §3's own core acceptance — "候補が尽きてなお超過なら
    MediaStoreWriteUnavailable". Every existing file is pinned (no real
    candidate to evict), so the NEW write's own pre-check must refuse
    rather than mint a ref that pushes the project further over."""
    storage = StorageConfig(max_bytes=100, pin=["alice"])
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice.save_tool_result("a" * 500, mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    with pytest.raises(MediaStoreWriteUnavailable):
        bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)


def test_preview_lists_what_would_go_without_deleting_it(tmp_path):
    """Tier 2: #5366 §3 item ④ — cross_session_eviction_preview() reports
    the same file eviction WOULD remove, but the file is still on disk
    afterward (a pure read, no side effect)."""
    storage = StorageConfig(max_bytes=600)
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_block = alice.save_tool_result("a" * 500, mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    # bob's own write would itself trigger real eviction via save_tool_result
    # (already covered above); here bob asks for a PREVIEW of ITS OWN
    # store's view of the project without writing anything new at all —
    # the project is not yet over cap from bob's perspective (only
    # alice's 500 bytes exist so far, under the 600 cap), so nothing is
    # previewed. Bump alice's directory over cap by itself first.
    alice.save_tool_result("a" * 500, mime_type="text/plain", seq=2)

    preview = bob.cross_session_eviction_preview()

    alice_path = (tmp_path / alice_block["path"]).resolve()
    assert alice_path in preview, (
        f"the oldest file must appear in the preview once the project is "
        f"over cap; got {preview!r}"
    )
    assert alice_path.exists(), (
        "a preview must never actually delete anything"
    )


def test_preview_is_empty_when_under_cap(tmp_path):
    """Tier 2: (accept-side) control — nothing is previewed when the
    project is already under max_bytes, not a stale/leftover list."""
    storage = StorageConfig(max_bytes=10_000)
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice.save_tool_result("small", mime_type="text/plain", seq=1)

    assert alice.cross_session_eviction_preview() == []
