"""Tier 2 tests for SourceManifest singleton + sources.yaml SSoT (ADR-0033 Phase 1).

All tests use real SourceManifest instances with pytest tmp_path for workspace
isolation. No unittest.mock / MagicMock / patch. Async tests use pytest-asyncio
(asyncio_mode = "strict" per pyproject.toml).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
import yaml

from reyn.index.source_manifest import (
    _MANIFESTS,
    SourceEntry,
    SourceLockedError,
    SourceManifest,
    get_source_manifest,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _entry(name: str = "my_code", **kw) -> SourceEntry:
    defaults = dict(
        description="My Python source",
        path="src/**/*.py",
        backend="sqlite",
        chunk_count=42,
        embedding_model="openai/text-embedding-3-small",
    )
    defaults.update(kw)
    return SourceEntry(name=name, **defaults)


def _manifest(tmp_path: Path) -> SourceManifest:
    """Return a fresh SourceManifest for an isolated workspace."""
    return SourceManifest(tmp_path)


# ── format_for_prompt: empty state ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_for_prompt_empty_returns_hint(tmp_path: Path):
    """Tier 2: empty manifest format_for_prompt returns onboarding hint."""
    m = _manifest(tmp_path)
    text = await m.format_for_prompt()
    assert "No indexed sources yet" in text
    assert "index_docs" in text
    assert "0 available" in text


# ── upsert + get_all roundtrip ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_and_get_all_roundtrip(tmp_path: Path):
    """Tier 2: upserted entry is returned by get_all with correct field values."""
    m = _manifest(tmp_path)
    e = _entry("alpha", description="Alpha source", chunk_count=10)
    await m.upsert(e)

    all_entries = await m.get_all()
    assert "alpha" in all_entries
    got = all_entries["alpha"]
    assert got.name == "alpha"
    assert got.description == "Alpha source"
    assert got.chunk_count == 10
    assert got.backend == "sqlite"


# ── upsert overwrites existing entry ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_overwrites_existing_entry(tmp_path: Path):
    """Tier 2: second upsert with same name overwrites the previous entry."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("beta", chunk_count=1))
    await m.upsert(_entry("beta", chunk_count=999, description="Updated"))

    all_entries = await m.get_all()
    assert all_entries["beta"].chunk_count == 999
    assert all_entries["beta"].description == "Updated"
    assert len(all_entries) == 1


# ── remove ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_returns_true_on_existing(tmp_path: Path):
    """Tier 2: remove returns True when the named entry exists."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("gamma"))
    result = await m.remove("gamma")
    assert result is True
    assert await m.get("gamma") is None


@pytest.mark.asyncio
async def test_remove_returns_false_on_missing(tmp_path: Path):
    """Tier 2: remove returns False when the named entry does not exist."""
    m = _manifest(tmp_path)
    result = await m.remove("nonexistent")
    assert result is False


# ── format_for_prompt: non-empty ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_format_for_prompt_with_entries(tmp_path: Path):
    """Tier 2: format_for_prompt with N entries renders correct markdown."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("code", description="Python source", chunk_count=100))
    await m.upsert(_entry("docs", description="Docs", chunk_count=50))

    text = await m.format_for_prompt()
    assert "2 available" in text
    assert "**code**" in text
    assert "Python source" in text
    assert "100 chunks" in text
    assert "**docs**" in text
    assert "50 chunks" in text
    assert "`recall`" in text
    # No onboarding hint when sources exist
    assert "No indexed sources yet" not in text


# ── atomic write: crash between tmp write and rename ─────────────────────────


@pytest.mark.asyncio
async def test_atomic_write_survives_orphaned_tmp(tmp_path: Path):
    """Tier 2: manifest survives an orphaned .yaml.tmp from a prior crash.

    Simulates the state where a previous process wrote <path>.yaml.tmp
    but crashed before renaming it.  A subsequent upsert should succeed
    and the final sources.yaml must reflect the new state.
    """
    m = _manifest(tmp_path)
    sources_path = tmp_path / ".reyn" / "index" / "sources.yaml"
    tmp_write_path = sources_path.with_suffix(".yaml.tmp")

    # Plant a stale .tmp (simulated crash artifact)
    sources_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_write_path.write_text("stale: {}", encoding="utf-8")

    # Normal upsert should complete and overwrite the stale tmp
    await m.upsert(_entry("delta", chunk_count=7))

    assert sources_path.exists()
    # Fresh manifest reads from file and finds the entry
    m2 = SourceManifest(tmp_path)
    entries = await m2.get_all()
    assert "delta" in entries
    assert entries["delta"].chunk_count == 7


# ── mem cache consistency ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mem_cache_consistent_across_get_upsert_remove(tmp_path: Path):
    """Tier 2: mem cache stays consistent across interleaved get / upsert / remove."""
    m = _manifest(tmp_path)

    await m.upsert(_entry("x", chunk_count=1))
    assert (await m.get("x")) is not None

    await m.upsert(_entry("x", chunk_count=2))
    assert (await m.get("x")).chunk_count == 2  # type: ignore[union-attr]

    await m.remove("x")
    assert (await m.get("x")) is None

    # get_all should reflect removal
    all_entries = await m.get_all()
    assert "x" not in all_entries


# ── singleton registry ────────────────────────────────────────────────────────


def test_get_source_manifest_returns_same_instance(tmp_path: Path):
    """Tier 2: get_source_manifest returns the same instance for the same workspace_root."""
    # Isolate from any other test by using a fresh subdirectory
    ws = tmp_path / "ws"
    ws.mkdir()

    # Clear module registry to avoid cross-test contamination
    _MANIFESTS.clear()
    m1 = get_source_manifest(ws)
    m2 = get_source_manifest(ws)
    assert m1 is m2

    # Different workspace → different instance
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    m3 = get_source_manifest(ws2)
    assert m3 is not m1

    # Cleanup
    _MANIFESTS.clear()


# ── acquire_source_lock: concurrent lock refused ──────────────────────────────


@pytest.mark.asyncio
async def test_acquire_source_lock_refuses_when_alive_pid_holds(tmp_path: Path):
    """Tier 2: second acquire raises SourceLockedError when held by alive PID."""
    m = _manifest(tmp_path)
    lock_path = tmp_path / ".reyn" / "index" / "src" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Write a lock held by our own (live) PID
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": 0.0}), encoding="utf-8"
    )

    with pytest.raises(SourceLockedError, match="src"):
        async with m.acquire_source_lock("src"):
            pass  # Should never reach here


# ── acquire_source_lock: stale lock is reaped ────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_source_lock_reaps_stale_lock(tmp_path: Path):
    """Tier 2: stale lock (dead PID) is overwritten and lock is acquired."""
    m = _manifest(tmp_path)
    lock_path = tmp_path / ".reyn" / "index" / "ghost" / ".lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Use a PID that is virtually guaranteed to be non-existent
    stale_pid = 99999999
    lock_path.write_text(
        json.dumps({"pid": stale_pid, "ts": 0.0}), encoding="utf-8"
    )

    entered = False
    async with m.acquire_source_lock("ghost"):
        entered = True
        # Lock file should now be held by our PID
        data = json.loads(lock_path.read_text())
        assert data["pid"] == os.getpid()

    assert entered
    # Lock file removed after context exit
    assert not lock_path.exists()


# ── acquire_source_lock: released after context exit ─────────────────────────


@pytest.mark.asyncio
async def test_acquire_source_lock_released_after_exit(tmp_path: Path):
    """Tier 2: subsequent acquire succeeds after the context manager exits."""
    m = _manifest(tmp_path)

    async with m.acquire_source_lock("reindex"):
        pass  # First acquire and release

    # Second acquire should succeed (lock file gone after first release)
    acquired_again = False
    async with m.acquire_source_lock("reindex"):
        acquired_again = True

    assert acquired_again


# ── cross-process cache invalidation (mtime poll) ────────────────────────────


@pytest.mark.asyncio
async def test_get_all_picks_up_external_writes(tmp_path: Path):
    """Tier 2: get_all detects sources.yaml changes written by another process."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("a", description="A", chunk_count=1))
    assert "a" in await m.get_all()

    # Simulate an external write (another process rewrote sources.yaml directly)
    yaml_path = tmp_path / ".reyn" / "index" / "sources.yaml"
    payload = yaml.safe_load(yaml_path.read_text()) or {}
    payload["b"] = {
        "description": "B",
        "path": "...",
        "backend": "sqlite",
        "chunk_count": 2,
        "embedding_model": "x",
    }
    # Ensure mtime advances (filesystem granularity is typically 1–10 ms)
    time.sleep(0.05)
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    # Same in-process manifest should detect the new mtime and reload
    all_after = await m.get_all()
    assert "b" in all_after, f"External write not picked up; got {list(all_after.keys())}"
    assert "a" in all_after


@pytest.mark.asyncio
async def test_format_for_prompt_picks_up_external_writes(tmp_path: Path):
    """Tier 2: format_for_prompt reflects external sources.yaml changes."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("first", description="First", chunk_count=5))

    # Direct external write adds "second"
    yaml_path = tmp_path / ".reyn" / "index" / "sources.yaml"
    payload = yaml.safe_load(yaml_path.read_text()) or {}
    payload["second"] = {
        "description": "Second source",
        "path": "docs/**",
        "backend": "sqlite",
        "chunk_count": 20,
        "embedding_model": "x",
    }
    time.sleep(0.05)
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")

    text = await m.format_for_prompt()
    assert "**second**" in text, f"External write not reflected in format_for_prompt; got:\n{text}"
    assert "Second source" in text
    assert "2 available" in text


@pytest.mark.asyncio
async def test_internal_writes_dont_trigger_spurious_reload(tmp_path: Path):
    """Tier 2: upsert/remove update _loaded_mtime so next get_all skips reload.

    We verify the mtime bookkeeping is correct by confirming that after our
    own upsert the manifest's recorded mtime matches the file's actual mtime.
    This ensures we won't hit an unnecessary disk read on the immediately
    following get_all call.
    """
    m = _manifest(tmp_path)
    await m.upsert(_entry("x", chunk_count=1))

    yaml_path = tmp_path / ".reyn" / "index" / "sources.yaml"
    file_mtime = yaml_path.stat().st_mtime

    # _loaded_mtime must match the file we just wrote
    assert m._loaded_mtime == file_mtime, (
        f"_loaded_mtime ({m._loaded_mtime}) != file mtime ({file_mtime}) "
        "after upsert — own write would trigger spurious reload"
    )

    # get_all must NOT see a stale cache (i.e. no unnecessary reload)
    result = await m.get_all()
    assert "x" in result


@pytest.mark.asyncio
async def test_get_all_handles_file_deleted_externally(tmp_path: Path):
    """Tier 2: get_all returns empty dict when sources.yaml is removed externally."""
    m = _manifest(tmp_path)
    await m.upsert(_entry("z", chunk_count=3))
    assert "z" in await m.get_all()

    # Another process deletes the file
    yaml_path = tmp_path / ".reyn" / "index" / "sources.yaml"
    yaml_path.unlink()

    result = await m.get_all()
    # Cache should be refreshed to empty (file gone)
    assert result == {}
