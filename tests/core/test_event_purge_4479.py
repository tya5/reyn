"""Tier 1/2: #4479 — automatic purge of `.reyn/events` files.

Covers `core/events/event_purge.py`'s pure selection/deletion functions
(Tier 1: no external state, deterministic on their inputs) and
`EventStore.submit_auto_purge`'s real, observable, on-disk effect (Tier 2:
a real EventStore + a real DurabilityWorker, actual files on `tmp_path` —
no mocks).

Both axes (age, disk-relative-size) are independent — EITHER firing is
enough (owner ruling: "日数orサイズ"), never `and`.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from reyn.core.events.event_purge import (
    apply_auto_purge,
    collect_dated_files,
    filename_start_date,
    purge_files,
    select_by_age,
    select_by_disk_budget,
    select_purge_targets,
)
from reyn.core.events.event_store import EventStore


def _write(root: Path, name: str, *, size_bytes: int = 0) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size_bytes if size_bytes else b'{"type":"t"}\n')
    return p


# ── filename_start_date / collect_dated_files ────────────────────────────


def test_filename_start_date_parses_the_leading_date():
    """Tier 1: the established `YYYY-MM-DDTHHMMSS[...].jsonl` filename shape."""
    assert filename_start_date("2026-01-15T120000.jsonl") == date(2026, 1, 15)


def test_filename_start_date_returns_none_for_an_unparseable_name():
    """Tier 1: a manually dropped-in file with no date prefix is never
    assumed recent (undated files sort first — see collect_dated_files)."""
    assert filename_start_date("notes.jsonl") is None


def test_collect_dated_files_sorts_undated_first_then_oldest_first(tmp_path):
    """Tier 1: undated files sort FIRST (never assumed recent — a
    conservative choice for a destructive-by-default purge), then real
    dates ascend oldest-first."""
    _write(tmp_path, "2026-06-01T000000.jsonl")
    _write(tmp_path, "2026-01-01T000000.jsonl")
    _write(tmp_path, "undated.jsonl")

    files = collect_dated_files(tmp_path)
    names = [p.name for p, _d in files]
    assert names == ["undated.jsonl", "2026-01-01T000000.jsonl", "2026-06-01T000000.jsonl"]


# ── select_by_age ─────────────────────────────────────────────────────────


def test_select_by_age_selects_strictly_older_files_only():
    """Tier 1: the cutoff is exclusive — a file dated exactly `before` is
    NOT selected (matches the pre-#4479 `reyn events purge --before`
    semantics: 'strictly before')."""
    files = [
        (Path("a"), date(2026, 1, 1)),
        (Path("b"), date(2026, 3, 1)),
        (Path("c"), date(2026, 6, 1)),
    ]
    selected = select_by_age(files, before=date(2026, 3, 1))
    assert selected == [Path("a")]


def test_select_by_age_never_selects_an_undated_file():
    """Tier 1: an undated file has no date to compare — the age axis must
    not guess; only the size axis can reach it."""
    files = [(Path("undated"), None), (Path("old"), date(2020, 1, 1))]
    selected = select_by_age(files, before=date(2026, 1, 1))
    assert selected == [Path("old")]


# ── select_by_disk_budget ────────────────────────────────────────────────


def test_select_by_disk_budget_purges_oldest_first_until_under_budget(
    tmp_path, monkeypatch,
):
    """Tier 1: once total size exceeds the free-space-relative budget,
    oldest-first files are selected until back under it."""
    old = _write(tmp_path, "2026-01-01T000000.jsonl", size_bytes=100)
    mid = _write(tmp_path, "2026-02-01T000000.jsonl", size_bytes=100)
    new = _write(tmp_path, "2026-03-01T000000.jsonl", size_bytes=100)
    files = collect_dated_files(tmp_path)

    from collections import namedtuple
    Usage = namedtuple("Usage", ["total", "used", "free"])
    # Total on-disk size = 300 bytes. A 1000-byte free budget at 20% =
    # 200 bytes — over budget by 100, so exactly the OLDEST file must go.
    monkeypatch.setattr(
        "reyn.core.events.event_purge.shutil.disk_usage",
        lambda _root: Usage(total=10_000, used=9_000, free=1_000),
    )
    selected = select_by_disk_budget(tmp_path, files, max_disk_usage_percent=20.0)
    assert selected == [old]
    assert mid not in selected
    assert new not in selected


def test_select_by_disk_budget_is_a_noop_under_budget(tmp_path, monkeypatch):
    """Tier 1: nothing selected when already under budget — no false-positive
    purge on a quiet events directory."""
    _write(tmp_path, "2026-01-01T000000.jsonl", size_bytes=10)
    files = collect_dated_files(tmp_path)
    from collections import namedtuple
    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(
        "reyn.core.events.event_purge.shutil.disk_usage",
        lambda _root: Usage(total=10_000, used=0, free=10_000),
    )
    assert select_by_disk_budget(tmp_path, files, max_disk_usage_percent=50.0) == []


def test_select_by_disk_budget_disabled_at_zero_percent(tmp_path):
    """Tier 1: `max_disk_usage_percent <= 0` disables the axis outright —
    no disk_usage() call, no selection, regardless of actual size."""
    _write(tmp_path, "2026-01-01T000000.jsonl", size_bytes=10_000_000)
    files = collect_dated_files(tmp_path)
    assert select_by_disk_budget(tmp_path, files, max_disk_usage_percent=0) == []


# ── select_purge_targets — the OR of both axes ───────────────────────────


def test_select_purge_targets_is_the_union_of_both_axes(tmp_path, monkeypatch):
    """Tier 1: EITHER axis firing selects a file — the owner's explicit
    'day OR size' ruling, not `and` (which would mean disabling one axis
    silently disables the other)."""
    old_small = _write(tmp_path, "2020-01-01T000000.jsonl", size_bytes=1)  # age only
    new_big = _write(  # size only
        tmp_path, f"{date.today().isoformat()}T000000.jsonl", size_bytes=1000,
    )
    kept = _write(tmp_path, f"{date.today().isoformat()}T010000.jsonl", size_bytes=1)

    from collections import namedtuple
    Usage = namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(
        "reyn.core.events.event_purge.shutil.disk_usage",
        lambda _root: Usage(total=10_000, used=0, free=1_000),
    )
    targets = select_purge_targets(
        tmp_path, max_age_days=30, max_disk_usage_percent=10.0,
    )
    assert old_small in targets, "age axis should have selected the old file"
    assert new_big in targets, "size axis should have selected the oversized file"
    assert kept not in targets


def test_select_purge_targets_both_axes_disabled_selects_nothing(tmp_path):
    """Tier 1: 0 on both axes means keep everything — the documented
    all-disabled shape."""
    _write(tmp_path, "2020-01-01T000000.jsonl")
    assert select_purge_targets(tmp_path, max_age_days=0, max_disk_usage_percent=0) == []


def test_select_purge_targets_missing_root_selects_nothing(tmp_path):
    """Tier 1: a not-yet-created events dir (a fresh project) is a no-op,
    not an error."""
    missing = tmp_path / "does-not-exist"
    assert select_purge_targets(missing, max_age_days=30) == []


# ── purge_files ───────────────────────────────────────────────────────────


def test_purge_files_deletes_and_counts(tmp_path):
    """Tier 1: real on-disk deletion, real count."""
    a = _write(tmp_path, "a.jsonl")
    b = _write(tmp_path, "b.jsonl")
    deleted = purge_files([a, b])
    assert deleted == 2
    assert not a.exists()
    assert not b.exists()


def test_purge_files_is_best_effort_on_a_missing_file(tmp_path):
    """Tier 1: an already-gone file (e.g. deleted by a concurrent purge) is
    skipped, not fatal — matches the CLI purge command's own established
    per-file try/except shape."""
    ghost = tmp_path / "ghost.jsonl"  # never created
    real = _write(tmp_path, "real.jsonl")
    deleted = purge_files([ghost, real])
    assert deleted == 1
    assert not real.exists()


# ── apply_auto_purge — the automatic-trigger entry point ────────────────


def test_apply_auto_purge_deletes_age_targets_end_to_end(tmp_path):
    """Tier 1: select + delete composed — the function EventStore's
    fire-and-forget job calls."""
    old = _write(tmp_path, "2020-01-01T000000.jsonl")
    new = _write(tmp_path, f"{date.today().isoformat()}T000000.jsonl")
    deleted = apply_auto_purge(tmp_path, max_age_days=30, max_disk_usage_percent=0)
    assert deleted == 1
    assert not old.exists()
    assert new.exists()


# ── EventStore.submit_auto_purge — real, observable, on-disk effect ─────


@pytest.mark.asyncio
async def test_event_store_first_write_purges_old_files(tmp_path):
    """Tier 2: #4479 — the FIRST write of a fresh EventStore instance is
    the guaranteed "session start" trigger (architect: rotation defaults
    OFF, so a true rotation may never fire; the first-open path always
    does, regardless of rotation config). A stale file dated far in the
    past is gone after the store's own `flush()` drains the fire-and-forget
    purge job."""
    events_dir = tmp_path / "events"
    stale = _write(events_dir, "2020-01-01T000000.jsonl")

    store = EventStore(events_dir, cleanup_period_days=30, max_disk_usage_percent=0)
    from reyn.schemas.models import Event
    store.write(Event(type="test_event", data={}))
    await store.flush()

    assert not stale.exists(), (
        "the stale file should have been purged by the store's own first-write trigger"
    )


@pytest.mark.asyncio
async def test_event_store_auto_purge_disabled_by_default_leaves_old_files(tmp_path):
    """Tier 2: regression guard — an `EventStore` constructed WITHOUT the
    new kwargs (every pre-#4479 call site, and any future one that doesn't
    opt in) purges nothing — 0 is the constructor default for both axes."""
    events_dir = tmp_path / "events"
    stale = _write(events_dir, "2020-01-01T000000.jsonl")

    store = EventStore(events_dir)  # no cleanup_period_days / max_disk_usage_percent
    from reyn.schemas.models import Event
    store.write(Event(type="test_event", data={}))
    await store.flush()

    assert stale.exists(), "default-constructed EventStore must not auto-purge"


def test_event_store_submit_auto_purge_is_a_noop_with_no_running_loop(tmp_path):
    """Tier 2: a sync-mode caller (no event loop — e.g. a CLI entry point)
    gets no automatic purge; `submit_auto_purge`'s own no-loop guard mirrors
    `write()`'s established sync-fallback precedent, but here the fallback
    is simply "do nothing" (the CLI's own `reyn events purge` stays the
    explicit path for that caller). Deliberately NOT `@pytest.mark.asyncio`
    — the real no-loop condition is a plain synchronous call, not something
    to simulate from inside a running loop."""
    events_dir = tmp_path / "events"
    stale = _write(events_dir, "2020-01-01T000000.jsonl")
    store = EventStore(events_dir, cleanup_period_days=30, max_disk_usage_percent=0)

    store.submit_auto_purge()  # no running loop in this plain sync test

    assert stale.exists(), "no running loop on the calling thread — must not purge"
