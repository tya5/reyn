"""Tier 2: #5364 §1.6 "C" — a session's own ``history-content`` directory
is bounded by ``MediaStoreConfig.history_content_max_bytes``: once a
write pushes it over cap, the OLDEST file(s) (see
``media_store._eviction_order``) are deleted until it's back under.

Scope, per that field's own docstring: this bounds ONE session's own
content, not cross-session growth (#5366's separate subject) — the test
below drives a single ``MediaStore`` through several writes and only
asserts on ITS OWN directory.
"""
from __future__ import annotations

import os

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def test_a_write_that_pushes_the_session_over_cap_evicts_the_oldest_file(
    tmp_path,
) -> None:
    """Tier 2: three ~40-byte writes under a ~50-byte cap must leave AT
    MOST one file behind, and it must be the LAST one written — the
    first two are evicted, oldest first, as later writes push the
    running total back over cap."""
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=50),
        project_root=tmp_path,
        agent_name="alice",
        session_id="main",
    )

    blocks = []
    for i in range(3):
        blocks.append(
            # seq=i: save_tool_result's filename is (timestamp, chain,
            # tool, seq) — two calls in the SAME wall-clock second with
            # the same default seq=1 would collide on ONE filename
            # (silently overwriting), which would make this test pass
            # for the wrong reason (only one file ever existing) rather
            # than genuinely exercising eviction across multiple files.
            store.save_tool_result(
                f"payload number {i} " * 2, mime_type="text/plain", seq=i,
            )
        )
        # #5364 §1.6: mtime is the eviction order's own sort key — force
        # each write's mtime to be strictly later than the previous
        # one's, since a fast test loop can otherwise land two writes in
        # the same filesystem-mtime tick and make "oldest" ambiguous.
        _bump_all_mtimes_forward(store.history_content_dir)

    # #5364 §1.6 "C": behavior, not shape — which SPECIFIC files survive,
    # not how many are left over (a raw file-count assertion pins the
    # eviction algorithm's exact shrink-to-fit tightness, not the
    # behavior under test: oldest-first eviction).
    oldest_path = tmp_path / blocks[0]["path"]
    middle_path = tmp_path / blocks[1]["path"]
    newest_path = tmp_path / blocks[2]["path"]
    assert not oldest_path.exists(), "the OLDEST write must be evicted first"
    assert not middle_path.exists(), (
        "the middle write must ALSO be evicted — it too predates the "
        "final write that pushed the session back over cap"
    )
    assert newest_path.exists(), (
        "the MOST RECENT write must survive — eviction is oldest-first"
    )


def test_a_session_under_cap_is_left_untouched(tmp_path) -> None:
    """Tier 2: a session whose content never exceeds the cap must never
    lose a file — eviction is conditional on being OVER cap, not an
    unconditional prune."""
    store = MediaStore(
        MediaStoreConfig(history_content_max_bytes=10_000_000),
        project_root=tmp_path,
        agent_name="alice",
        session_id="main",
    )
    first = store.save_tool_result("small", mime_type="text/plain", seq=1)
    second = store.save_tool_result("also small", mime_type="text/plain", seq=2)

    # #5364 §1.6 "C": both specific files present — not a count, which
    # a cap-eviction test must not pin (that would double as a hidden
    # assertion on the underlying write format's exact byte size).
    assert (tmp_path / first["path"]).exists()
    assert (tmp_path / second["path"]).exists()


def _bump_all_mtimes_forward(directory) -> None:
    """Force every file already in *directory* one second further into
    the past relative to whatever gets written next, so eviction order
    (sorted by mtime) is deterministic across a fast test loop instead
    of racing the filesystem's own mtime tick granularity."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))
