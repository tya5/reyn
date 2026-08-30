"""Tier 2: #5512 — ``MediaStore.read_media_base64`` memoizes the file
read + base64 encode by ``content_hash`` (the canonical path-ref field,
#383) — a second wire-materialisation call for the SAME content skips
both the read and the encode; a call for DIFFERENT content (a new
``content_hash``) does not skip either.

owner: "base64 の memo/cache 化は issue 化しておいて — 優先度は高くしなくて
良い". Explicit non-goal (issue body): wire byte count is unaffected —
this is an I/O/CPU saving, never a cost/budget change.

Accept criteria (lead-coder's own dispatch): witnessed by CALL COUNT on
the real underlying read, never duration (repo testing policy — no
sleep/timing-based assertions). Real ``MediaStore`` + real file on disk
throughout; the "spy" below is a real wrapper around the production
method (the #2937 counting-spy idiom already used elsewhere in this
repo — a real callable recording invocations, not a mock/MagicMock),
not a stand-in for MediaStore itself.
"""
from __future__ import annotations

from pathlib import Path

from reyn.data.workspace.media_store import MediaStore, MediaStoreConfig


def _new_store(tmp_path: Path) -> MediaStore:
    return MediaStore(MediaStoreConfig(), project_root=tmp_path, session_id="test-session")


def _counting_read_media(store: MediaStore) -> list:
    """Wrap ``store.read_media`` with a REAL counting spy — records each
    call's path, then delegates to the original implementation
    unchanged. Returns the shared ``calls`` list the caller inspects."""
    calls: list[str] = []
    original = store.read_media

    def _spy(path_str: str):
        calls.append(path_str)
        return original(path_str)

    store.read_media = _spy  # type: ignore[method-assign]
    return calls


def test_a_second_read_for_the_same_content_hash_skips_the_underlying_read(
    tmp_path: Path,
) -> None:
    """Tier 2: #5512 accept ① — two ``read_media_base64`` calls for the
    SAME ``content_hash`` invoke the real underlying ``read_media`` only
    ONCE. Strip-falsify performed by hand while writing this test: with
    the cache-store/cache-check lines in ``read_media_base64`` commented
    out, this assertion correctly reads 2 (confirmed red); restored,
    green — this is what makes the call-count assertion below an actual
    witness of the docstring's own claim (#5521/#5513's own "does this
    test witness what the docstring says" question, applied here), not
    a number that would pass regardless of whether caching happened."""
    store = _new_store(tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    block = store.save_media(raw, mime_type="image/png", tool="test", seq=1)
    path = block["path"]
    content_hash = block["content_hash"]

    calls = _counting_read_media(store)

    b64_first, found_first = store.read_media_base64(path, content_hash=content_hash)
    b64_second, found_second = store.read_media_base64(path, content_hash=content_hash)

    assert found_first and found_second
    assert b64_first == b64_second
    # The second call must not have added a second recorded read — a
    # bare count would pin an arbitrary number; this instead asserts the
    # SECOND call left no trace at all (the spy's own call log is
    # unchanged from what the first call alone produced).
    assert calls == [path], (
        f"expected the underlying read_media to fire only for the FIRST "
        f"call (the second should hit the content_hash cache and leave "
        f"no new entry), got {calls!r}"
    )


def test_a_different_content_hash_does_not_hit_the_stale_cache_entry(
    tmp_path: Path,
) -> None:
    """Tier 2: #5512 accept ②, deny side — a DIFFERENT ``content_hash``
    (simulating changed content, the real invalidation mechanism the
    issue itself named — a changed file is a different key by
    construction) must NOT reuse the first call's cached value. Without
    this, an "always return the first cached value regardless of key"
    implementation would pass accept ① for the wrong reason."""
    store = _new_store(tmp_path)
    raw_a = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    raw_b = b"\x89PNG\r\n\x1a\n" + b"\x01" * 80
    block_a = store.save_media(raw_a, mime_type="image/png", tool="test", seq=1)
    block_b = store.save_media(raw_b, mime_type="image/png", tool="test", seq=2)
    assert block_a["content_hash"] != block_b["content_hash"], (
        "test setup sanity: two different file contents must produce two "
        "different content_hash keys, or this test proves nothing"
    )

    calls = _counting_read_media(store)

    b64_a, _ = store.read_media_base64(block_a["path"], content_hash=block_a["content_hash"])
    b64_b, _ = store.read_media_base64(block_b["path"], content_hash=block_b["content_hash"])

    assert b64_a != b64_b, "two different files must not resolve to the same base64 payload"
    # Each distinct content_hash must produce its OWN recorded read — a
    # cache hit on the wrong key would either drop one entry or merge
    # the two paths; asserting the exact ordered pair rules out both.
    assert calls == [block_a["path"], block_b["path"]], (
        f"expected the underlying read_media to fire once PER distinct "
        f"content_hash (no stale hit across different keys), got {calls!r}"
    )


def test_no_content_hash_never_caches(tmp_path: Path) -> None:
    """Tier 2: #5512 — ``content_hash=None`` (a caller with no hash to key
    on, e.g. a legacy path-ref block) always misses the cache — every
    call re-reads. Documents the degrade explicitly rather than leaving
    it as an untested assumption."""
    store = _new_store(tmp_path)
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 80
    block = store.save_media(raw, mime_type="image/png", tool="test", seq=1)
    path = block["path"]

    calls = _counting_read_media(store)

    store.read_media_base64(path, content_hash=None)
    store.read_media_base64(path, content_hash=None)

    # Both calls must have left their own trace — content_hash=None means
    # neither could have been served from the cache.
    assert calls == [path, path], (
        f"content_hash=None must never be cached — expected a real read "
        f"recorded for BOTH calls, got {calls!r}"
    )
