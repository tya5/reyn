"""Tier 2: #4478 — ``.reyn/media/`` (images) counts against the SAME
project-wide ``StorageConfig.max_bytes`` cap ``.reyn/memory/history-
content/`` already does — architect ruling: "母集団を1つ広げるだけ。新
しい概念は1つも要りません。". No TTL / max-N / a media-only cap — one
operator number, the existing eviction order (oldest-first) and pin
mechanism, reused.

A real, pre-existing structural gap made pin unenforceable for media:
``.reyn/media/`` was a FLAT directory (``save_media`` wrote directly
under ``media_dir``, no per-agent nesting), so
``cross_session_eviction_candidates``'s own pin match
(``path.relative_to(root).parts[0]`` against a pinned agent name) read
a media file's own FILENAME where it expected an agent name — never a
match, so pin silently gave media no protection at all. Architect's
own follow-up ruling (same issue): ``save_media`` now nests under
``<agent>/<session_id>/`` — the SAME shape ``history_content_dir_for``
already established for tool-results, "0 new concepts" — when a real
``agent_name`` is available; a legacy/read-only construction (no
``agent_name``) keeps the pre-#4478 flat write. Existing flat files are
never migrated (disclosed as pin-unprotected candidates, never
silently claimed safe) — owner-scoped: reyn-self carries 0 such files.

Same idiom as ``tests/data/test_5366_cross_session_eviction_driver.py``
(this file's own established sibling for the tool-results side): real
on-disk writes via real ``MediaStore`` instances, no fakes.
"""
from __future__ import annotations

import os

import pytest

from reyn.config.infra import StorageConfig
from reyn.data.workspace.media_store import (
    MediaStore,
    MediaStoreConfig,
    MediaStoreWriteUnavailable,
    cross_session_eviction_candidates,
    media_content_dir_for,
)


def _bump_mtime_forward(directory) -> None:
    """Same determinism helper #5366's own driver test uses — forces
    every existing file further into the past so write-order ties never
    race real filesystem mtime-tick granularity."""
    for path in directory.rglob("*"):
        if path.is_file():
            st = path.stat()
            os.utime(path, (st.st_atime, st.st_mtime - 1))


# ── save_media's own nesting (the pin precondition) ──────────────────────────


def test_save_media_nests_under_agent_and_session_when_agent_name_is_set(tmp_path):
    """Tier 2: accept — a store constructed with a real ``agent_name``
    writes new media under ``<media_dir>/<agent>/<session_id>/…`` (the
    SAME shape history-content's own ``history_content_dir_for`` already
    uses), not flat directly under ``media_dir``."""
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
    )
    block = store.save_media(b"\x89PNG\r\n", mime_type="image/png")

    written = (tmp_path / block["path"]).resolve()
    expected_dir = media_content_dir_for(tmp_path, "alice", "main")
    assert written.parent == expected_dir, (
        f"expected the file under {expected_dir}, got {written.parent}"
    )
    assert written.exists()


def test_save_media_with_no_agent_name_stays_flat_legacy_contract(tmp_path):
    """Tier 2: legacy contract — a store with NO ``agent_name`` (4 of 5
    production construction sites are read-only and legitimately have
    none, per ``_history_content_dir``'s own docstring) keeps writing
    directly under ``media_dir``, unchanged from before #4478 — never
    raises, never nests."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path, session_id="main")
    block = store.save_media(b"\x89PNG\r\n", mime_type="image/png")

    written = (tmp_path / block["path"]).resolve()
    media_dir = (tmp_path / MediaStoreConfig().media_dir).resolve()
    assert written.parent == media_dir, (
        f"a store with no agent_name must stay flat under {media_dir}, "
        f"got {written.parent}"
    )


# ── pin now protects (only) a post-#4478 nested media file ──────────────────


def test_pinned_agents_nested_media_is_excluded_from_eviction_candidates(tmp_path):
    """Tier 2: accept — the real point of the nesting. A NEW (post-#4478)
    media file written by a PINNED agent is excluded from
    cross_session_eviction_candidates — witness 2 becomes real."""
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
    )
    block = store.save_media(b"\x89PNG\r\n", mime_type="image/png")
    written = (tmp_path / block["path"]).resolve()

    media_dir = (tmp_path / MediaStoreConfig().media_dir).resolve()
    candidates = cross_session_eviction_candidates(media_dir, pin=["alice"])
    assert written not in candidates, (
        "a pinned agent's own nested media file must be excluded from "
        "the eviction candidate list"
    )


def test_an_unpinned_agents_nested_media_is_a_candidate(tmp_path):
    """Tier 2: deny sibling — a NEW nested media file from an agent NOT
    in the pin list is a real candidate, same as history-content."""
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
    )
    block = store.save_media(b"\x89PNG\r\n", mime_type="image/png")
    written = (tmp_path / block["path"]).resolve()

    media_dir = (tmp_path / MediaStoreConfig().media_dir).resolve()
    candidates = cross_session_eviction_candidates(media_dir, pin=["alice"])
    assert written in candidates, (
        "an agent not in the pin list must remain a real candidate"
    )


def test_a_legacy_flat_media_file_is_a_pin_unprotected_candidate(tmp_path):
    """Tier 2: deny — a pre-#4478-shaped flat file (no agent_name at
    write time) is a candidate REGARDLESS of pin — this file names the
    disclosed gap directly: "pin protects media" is NOT claimed for a
    flat file, only for a post-#4478 nested one."""
    store = MediaStore(MediaStoreConfig(), project_root=tmp_path, session_id="main")
    block = store.save_media(b"\x89PNG\r\n", mime_type="image/png")
    written = (tmp_path / block["path"]).resolve()

    media_dir = (tmp_path / MediaStoreConfig().media_dir).resolve()
    # Even pinning a name that happens to equal this flat file's own
    # first path segment (its filename) cannot protect it — parts[0]
    # for a flat file IS the filename, never an agent name; pinning an
    # unrelated real agent leaves the flat file unaffected either way.
    candidates = cross_session_eviction_candidates(media_dir, pin=["alice"])
    assert written in candidates, (
        "a legacy flat media file must remain a candidate — pin has no "
        "attribution to match it against"
    )


def test_reviewer_strip_removing_nesting_makes_the_pin_test_fail(tmp_path):
    """Tier 2: reviewer strip (architect's own required witness) —
    reverting save_media to the pre-#4478 flat write makes the pin-
    protection accept test genuinely fail, proving that test actually
    depends on the nesting rather than passing vacuously."""
    store = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
    )
    # Simulate the pre-#4478 flat write directly (bypassing save_media's
    # own nesting) — the same construction save_media used to produce.
    store.media_dir.mkdir(parents=True, exist_ok=True)
    flat_path = store.media_dir / "20260101T000000-abc-tool-1.png"
    flat_path.write_bytes(b"\x89PNG\r\n")

    candidates = cross_session_eviction_candidates(store.media_dir, pin=["alice"])
    assert flat_path in candidates, (
        "sanity: a flat write (the pre-#4478 shape) must NOT be excluded "
        "by pin — proving the pin-protection accept test above is "
        "genuinely exercising the nesting, not passing vacuously"
    )


# ── the project-wide cap's own widened population ────────────────────────────


def test_media_bytes_count_toward_the_same_project_wide_cap(tmp_path):
    """Tier 2: accept — witness 1/5. A tool-result write's own pre-check
    now measures history-content BYTES PLUS media BYTES together against
    ONE ``max_bytes`` — an over-cap state driven by media ALONE still
    triggers eviction on the next tool-result write, and media's own
    older files are real candidates (not a second, separately-tracked
    number)."""
    storage = StorageConfig(max_bytes=600)
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_media_block = alice.save_media(b"a" * 500, mime_type="image/png")
    _bump_mtime_forward(tmp_path)

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    # bob's own tool-result write's pre-check must see alice's MEDIA
    # bytes (500) already over cap once bob adds a text write on top —
    # this only proves the point if the cap counts media at all.
    bob.save_tool_result("b" * 200, mime_type="text/plain", seq=1)
    _bump_mtime_forward(tmp_path)

    # This second write's own pre-check now sees the combined total
    # over cap and must evict alice's older MEDIA file to make room.
    bob.save_tool_result("c" * 10, mime_type="text/plain", seq=2)

    alice_media_path = (tmp_path / alice_media_block["path"]).resolve()
    assert not alice_media_path.exists(), (
        "alice's older media file must have been evicted once the "
        "COMBINED (history-content + media) total went over cap"
    )


def test_pinned_agents_media_survives_a_combined_cap_eviction(tmp_path):
    """Tier 2: accept sibling — witness 2, driven through the real
    combined-cap eviction path (not just the candidate-listing unit
    check above)."""
    storage = StorageConfig(max_bytes=600, pin=["alice"])
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_media_block = alice.save_media(b"a" * 500, mime_type="image/png")
    _bump_mtime_forward(tmp_path)

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)

    alice_media_path = (tmp_path / alice_media_block["path"]).resolve()
    assert alice_media_path.exists(), (
        "a pinned agent's media must survive combined-cap eviction"
    )


def test_max_bytes_none_never_evicts_media_either(tmp_path):
    """Tier 2: witness 3 — the None-default failsafe covers media too,
    not just history-content."""
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=StorageConfig(max_bytes=None),
    )
    block = alice.save_media(b"a" * 10_000, mime_type="image/png")
    _bump_mtime_forward(tmp_path)

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=StorageConfig(max_bytes=None),
    )
    bob.save_tool_result("b" * 10_000, mime_type="text/plain", seq=1)

    assert (tmp_path / block["path"]).resolve().exists(), (
        "max_bytes=None must never evict anything, media included"
    )


def test_raises_write_unavailable_when_media_and_history_content_both_pinned_over_cap(
    tmp_path,
):
    """Tier 2: witness 4/core — every candidate (both trees) pinned, so
    the combined population cannot shrink below cap; the next write must
    refuse rather than mint a ref that pushes the project further over."""
    storage = StorageConfig(max_bytes=100, pin=["alice"])
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice.save_media(b"a" * 500, mime_type="image/png")
    _bump_mtime_forward(tmp_path)

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    with pytest.raises(MediaStoreWriteUnavailable):
        bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)


def test_preview_lists_a_media_file_that_would_be_evicted(tmp_path):
    """Tier 2: cross_session_eviction_preview() reports media candidates
    too, without deleting anything — the SAME "what would go" surface
    #5366 item ④ already gives text results."""
    storage = StorageConfig(max_bytes=600)
    alice = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="alice", session_id="main",
        storage=storage,
    )
    alice_media_block = alice.save_media(b"a" * 500, mime_type="image/png")
    _bump_mtime_forward(tmp_path)

    bob = MediaStore(
        MediaStoreConfig(), project_root=tmp_path, agent_name="bob", session_id="main",
        storage=storage,
    )
    bob.save_tool_result("b" * 500, mime_type="text/plain", seq=1)

    preview = bob.cross_session_eviction_preview()
    alice_media_path = (tmp_path / alice_media_block["path"]).resolve()
    assert alice_media_path in preview
    assert alice_media_path.exists(), "a preview must never actually delete anything"
