"""Tier 2: reyn.registry.cache invariants.

Verifies TTL hit / miss / expired / set+get roundtrip / corrupt-file graceful.
No mocks — uses real filesystem via tmp_path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

import pytest

import reyn.registry.cache as cache_mod


def _patch_cache_dir(tmp_path: Path):
    """Context manager: redirect _cache_dir() to tmp_path."""
    return mock.patch.object(cache_mod, "_cache_dir", return_value=tmp_path)


# ---------------------------------------------------------------------------
# set + get roundtrip
# ---------------------------------------------------------------------------


def test_set_then_get_returns_data(tmp_path):
    """Tier 2: set(key, data) followed by get(key) returns the same data."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("my-key", {"hello": "world"})
        result = cache_mod.get("my-key")
    assert result == {"hello": "world"}


def test_set_creates_parent_dirs(tmp_path):
    """Tier 2: set() creates ~/.reyn/registry-cache/ if it doesn't exist."""
    nested = tmp_path / "deep" / "nested"
    with mock.patch.object(cache_mod, "_cache_dir", return_value=nested):
        cache_mod.set("k", {"x": 1})
        result = cache_mod.get("k")
    assert result == {"x": 1}


# ---------------------------------------------------------------------------
# cache miss (key never set)
# ---------------------------------------------------------------------------


def test_get_missing_key_returns_none(tmp_path):
    """Tier 2: get() on a key that was never set returns None."""
    with _patch_cache_dir(tmp_path):
        result = cache_mod.get("nonexistent-key")
    assert result is None


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


def test_get_within_ttl_returns_data(tmp_path):
    """Tier 2: get() within TTL window returns cached data."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("fresh", {"val": 42})
        # Immediately after set — well within TTL.
        result = cache_mod.get("fresh")
    assert result == {"val": 42}


def test_get_after_ttl_returns_none(tmp_path):
    """Tier 2: get() after TTL expiry returns None (mtime pushed into the past)."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("stale", {"val": 99})

        # Find the file and set its mtime to 25 hours ago.
        key_path = cache_mod._key_to_path("stale")
        old_mtime = time.time() - (25 * 3600)
        import os
        os.utime(key_path, (old_mtime, old_mtime))

        result = cache_mod.get("stale")
    assert result is None


# ---------------------------------------------------------------------------
# Corrupt file
# ---------------------------------------------------------------------------


def test_get_corrupt_file_returns_none(tmp_path):
    """Tier 2: get() on a corrupt (non-JSON) cache file returns None gracefully."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("corrupt", {"ok": True})
        key_path = cache_mod._key_to_path("corrupt")
        key_path.write_text("not valid json }{", encoding="utf-8")

        result = cache_mod.get("corrupt")
    assert result is None


# ---------------------------------------------------------------------------
# Key encoding
# ---------------------------------------------------------------------------


def test_key_encoding_is_filesystem_safe(tmp_path):
    """Tier 2: keys with special chars (colons, slashes) produce safe filenames."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("search:github:20", {"servers": []})
        result = cache_mod.get("search:github:20")
    assert result == {"servers": []}

    # The filename must not contain raw colons or slashes.
    (only_file,) = tmp_path.iterdir()
    filename = only_file.name
    assert ":" not in filename
    assert "/" not in filename


# ---------------------------------------------------------------------------
# Overwrite
# ---------------------------------------------------------------------------


def test_set_overwrites_existing_entry(tmp_path):
    """Tier 2: second set() on the same key overwrites the first."""
    with _patch_cache_dir(tmp_path):
        cache_mod.set("key", {"version": 1})
        cache_mod.set("key", {"version": 2})
        result = cache_mod.get("key")
    assert result == {"version": 2}
