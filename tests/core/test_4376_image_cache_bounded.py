"""Tier 2: #4376 — ReynPresenter._image_cache is bounded by total bytes, not
left to accumulate for a session's full lifetime.

lead-coder's discovery while investigating #4374: every unique image `src` a
chat session resolved stayed cached forever (`begin_image_resolution`'s 3
write sites had zero eviction paths) — up to 5MB/entry
(`image_fetch.DEFAULT_MAX_BYTES`), the same "unbounded accumulation, no
eviction path" shape as #3876 (413MB) / #3872 (10GB).

Model-membership binding (flowview's own v0.17.0 memory-control guide
pattern — shed a cache entry when its owning entry leaves the model) was
considered first but is not available here: reyn's chat FlowView model never
removes an individual entry mid-session (only a full `conversation.clear()`
on session switch — see presenter.py's own docstring at the fix site). The
fallback is a total-byte cap derived from `DEFAULT_MAX_BYTES` (never a fresh
magic number), enforced by `_store_image_resolution`, the single mutation
point every `begin_image_resolution` write path now goes through.

Per lead-coder's explicit six-question-⑤ requirement: these tests assert
what BOUNDS the cache (the total never exceeds the cap; an evicted entry is
observably gone), not "insert N, N remain" — the latter would still pass a
regression that changed the eviction policy's shape as long as the COUNT
happened to match, without actually proving anything is bounded.
"""
from __future__ import annotations

from reyn.core.present.image_fetch import DEFAULT_MAX_BYTES, ImageResolution
from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter


def _resolution(nbytes: int) -> ImageResolution:
    return ImageResolution(ok=True, body=b"x" * nbytes, content_type="image/png")


def test_total_cached_bytes_never_exceeds_the_cap() -> None:
    """Tier 2: #4376 — inserting enough entries to exceed the byte cap keeps
    the running total AT OR UNDER the cap, not merely "some bound decided by
    how many happened to fit" — this is the actual invariant the fix
    provides, checked directly against the cap it declares."""
    presenter = ReynPresenter()
    entry_size = DEFAULT_MAX_BYTES  # the largest single entry the fetch layer allows
    # Enough entries to comfortably exceed the cap several times over.
    n_entries = (presenter.image_cache_byte_cap // entry_size) + 5

    for i in range(n_entries):
        presenter._store_image_resolution(f"src-{i}", _resolution(entry_size))
        assert presenter.image_cache_size_bytes <= presenter.image_cache_byte_cap, (
            f"cache exceeded its own byte cap after inserting src-{i}: "
            f"{presenter.image_cache_size_bytes} > {presenter.image_cache_byte_cap}"
        )


def test_the_cap_is_derived_from_the_shared_per_entry_constant() -> None:
    """Tier 2: #4376 — lead-coder's explicit requirement: the total cap must
    be DERIVED from `DEFAULT_MAX_BYTES` (the per-entry fetch cap), not an
    independently-chosen number that could silently drift out of sync with
    it. Asserts the actual divisibility relationship, not just "it's some
    number bigger than one entry" — a derived-but-then-hardcoded constant
    would still pass a weaker check."""
    presenter = ReynPresenter()
    assert presenter.image_cache_byte_cap % DEFAULT_MAX_BYTES == 0
    assert presenter.image_cache_byte_cap >= DEFAULT_MAX_BYTES


def test_an_evicted_entry_is_observably_gone_and_a_later_request_would_refetch() -> None:
    """Tier 2: #4376 — the oldest entry, once evicted to make room, is no
    longer reported as cached (`has_cached_image` — the same membership
    check `begin_image_resolution` itself uses to decide whether to start a
    new fetch). This is the real, observable consequence a caller sees, not
    an assertion on `_image_cache`'s raw contents."""
    presenter = ReynPresenter()
    entry_size = DEFAULT_MAX_BYTES
    n_to_overflow = (presenter.image_cache_byte_cap // entry_size) + 3

    presenter._store_image_resolution("oldest", _resolution(entry_size))
    assert presenter.has_cached_image("oldest")

    for i in range(n_to_overflow):
        presenter._store_image_resolution(f"filler-{i}", _resolution(entry_size))

    assert not presenter.has_cached_image("oldest"), (
        "the oldest entry should have been evicted once enough newer "
        "entries pushed the total over the byte cap"
    )
    # The most recently stored entry must survive — the cap bounds the
    # TOTAL, it must not evict what was just asked for.
    assert presenter.has_cached_image(f"filler-{n_to_overflow - 1}")


def test_re_storing_an_existing_src_does_not_double_count_its_old_size() -> None:
    """Tier 2: #4376 — falsifies the double-counting shape a naive
    "always add, never subtract the old value" implementation would have:
    re-resolving the SAME src (e.g. a retry after a transient failure) must
    replace, not accumulate, its contribution to the tracked total."""
    presenter = ReynPresenter()
    small = DEFAULT_MAX_BYTES // 4

    presenter._store_image_resolution("retried", _resolution(small))
    after_first = presenter.image_cache_size_bytes
    presenter._store_image_resolution("retried", _resolution(small))
    after_second = presenter.image_cache_size_bytes

    assert after_second == after_first, (
        f"re-storing the same src changed the tracked total "
        f"({after_first} -> {after_second}) — the old entry's bytes were "
        f"not correctly subtracted before adding the new ones"
    )


def test_a_single_entry_under_the_cap_evicts_nothing() -> None:
    """Tier 2: #4376 accept-side — the eviction loop must not fire when
    nothing needs evicting (guards against an off-by-one that evicts on
    every insert regardless of total)."""
    presenter = ReynPresenter()
    presenter._store_image_resolution("only", _resolution(DEFAULT_MAX_BYTES))

    assert presenter.has_cached_image("only")
    assert presenter.image_cache_size_bytes == DEFAULT_MAX_BYTES
