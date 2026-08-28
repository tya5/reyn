"""Tier 2: #5366 §3 — the project-wide GC's own candidate set
(``cross_session_eviction_candidates``).

Real on-disk writes via real ``MediaStore`` instances (multiple agents,
multiple sessions) — never a hand-built directory tree — so attribution
is exercised through the SAME path shape #5383's key-space fix
established (``<agent>/<sid>/``), not a synthetic stand-in.
"""
from __future__ import annotations

import os

from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    cross_session_eviction_candidates,
    history_content_root_for,
)


def _bump_mtime_forward(directory) -> None:
    """Force every file already in *directory* one second further into
    the past — the same determinism helper #5364 §1.6 "C"'s own test
    uses, so ordering across a fast test loop is not racing filesystem
    mtime-tick granularity."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


def test_candidates_span_every_agent_oldest_first_with_no_pin(tmp_path):
    """Tier 2: with no pin, the candidate list is exactly
    _eviction_order's own output over the WHOLE root — every agent's
    every session, oldest write first, regardless of which agent wrote
    it."""
    alice = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main")
    bob = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main")

    alice_block = alice.save_tool_result("alice first", mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))
    bob_block = bob.save_tool_result("bob second", mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))
    alice_block2 = alice.save_tool_result("alice third", mime_type="text/plain", seq=2)

    root = history_content_root_for(tmp_path)
    candidates = cross_session_eviction_candidates(root)

    expected = [
        (tmp_path / alice_block["path"]).resolve(),
        (tmp_path / bob_block["path"]).resolve(),
        (tmp_path / alice_block2["path"]).resolve(),
    ]
    assert candidates == expected, (
        f"candidates must span both agents in write order — got "
        f"{[p.name for p in candidates]!r}"
    )


def test_pinned_agent_is_excluded_entirely(tmp_path):
    """Tier 2: acceptance — a pinned agent's own file NEVER appears in
    the candidate list, even though it would otherwise be the OLDEST
    (first-evicted) entry."""
    alice = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main")
    bob = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main")

    alice_block = alice.save_tool_result("alice oldest", mime_type="text/plain", seq=1)
    _bump_mtime_forward(history_content_root_for(tmp_path))
    bob_block = bob.save_tool_result("bob newest", mime_type="text/plain", seq=1)

    root = history_content_root_for(tmp_path)
    candidates = cross_session_eviction_candidates(root, pin=["alice"])

    assert (tmp_path / alice_block["path"]).resolve() not in candidates, (
        "pinned agent's file must never be a candidate, regardless of age"
    )
    assert candidates == [(tmp_path / bob_block["path"]).resolve()], (
        "the unpinned agent's file must still be a candidate"
    )


def test_pinning_every_agent_leaves_no_candidates(tmp_path):
    """Tier 2: control — pinning every agent that has written content
    leaves an EMPTY candidate list (not an error), the shape the
    exhaustion-drive follow-up (#5366's own next piece) needs to detect
    "nothing left to evict"."""
    alice = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main")
    alice.save_tool_result("alice content", mime_type="text/plain", seq=1)

    root = history_content_root_for(tmp_path)
    candidates = cross_session_eviction_candidates(root, pin=["alice"])

    assert candidates == []


def test_empty_pin_list_behaves_like_no_pin(tmp_path):
    """Tier 2: control — an explicit empty pin list is the SAME as
    omitting pin entirely (no accidental exclusion from an empty-but-
    not-None list)."""
    alice = MediaStore(MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main")
    block = alice.save_tool_result("alice content", mime_type="text/plain", seq=1)

    root = history_content_root_for(tmp_path)
    with_empty = cross_session_eviction_candidates(root, pin=[])
    with_none = cross_session_eviction_candidates(root, pin=None)

    expected = [(tmp_path / block["path"]).resolve()]
    assert with_empty == expected
    assert with_none == expected


def test_no_content_at_all_returns_empty_not_an_error(tmp_path):
    """Tier 2: (accept-side) a root that was never written to (or does
    not exist yet) yields an empty candidate list, not an exception —
    mirrors _eviction_order's own missing-directory tolerance."""
    root = history_content_root_for(tmp_path)
    assert not root.exists()
    assert cross_session_eviction_candidates(root, pin=["alice"]) == []
