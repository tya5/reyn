"""Tier 2: mcp_cache_file module — FP-0037 S1 persistent cache utilities.

Pins the public contract for:
  - cache_file_path(state_dir) — path derivation
  - write_cache(path, servers) — atomic write + version envelope
  - read_cache(path) — safe read: absent/corrupt/version-mismatch → None
  - file_mtime(path) — stat or None

No mocks.  All tests use tmp_path to avoid polluting the working tree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reyn.chat.services.mcp_cache_file import (
    cache_file_path,
    file_mtime,
    read_cache,
    write_cache,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SAMPLE_SERVERS: dict[str, list[dict]] = {
    "github": [
        {"name": "get_repo", "description": "Get a repository", "inputSchema": {}},
        {"name": "list_prs", "description": "List pull requests", "inputSchema": {}},
    ],
    "filesystem": [
        {"name": "read_file", "description": "Read a file", "inputSchema": {}},
    ],
}


# ---------------------------------------------------------------------------
# cache_file_path
# ---------------------------------------------------------------------------


def test_cache_file_path_returns_expected_name(tmp_path: Path) -> None:
    """Tier 2: cache_file_path returns <state_dir>/mcp_tools_cache.json."""
    result = cache_file_path(tmp_path / "state")
    assert result == tmp_path / "state" / "mcp_tools_cache.json"


# ---------------------------------------------------------------------------
# write_cache / read_cache round-trip
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    """Tier 2: write_cache then read_cache returns the same servers dict."""
    path = cache_file_path(tmp_path / "state")
    write_cache(path, _SAMPLE_SERVERS)
    result = read_cache(path)
    assert result is not None
    assert result == _SAMPLE_SERVERS


def test_write_cache_creates_version_and_probed_at(tmp_path: Path) -> None:
    """Tier 2: written file contains version=1 and a probed_at ISO timestamp."""
    path = cache_file_path(tmp_path / "state")
    write_cache(path, {"s": []})
    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert "probed_at" in raw
    # probed_at must be a non-empty string (ISO timestamp).
    assert isinstance(raw["probed_at"], str) and raw["probed_at"]


# ---------------------------------------------------------------------------
# read_cache — absent file
# ---------------------------------------------------------------------------


def test_read_absent_returns_none(tmp_path: Path) -> None:
    """Tier 2: read_cache on a missing file returns None without raising."""
    path = tmp_path / "no_such_dir" / "mcp_tools_cache.json"
    result = read_cache(path)
    assert result is None


# ---------------------------------------------------------------------------
# read_cache — corrupt file
# ---------------------------------------------------------------------------


def test_read_corrupt_returns_none_and_logs(tmp_path: Path, caplog) -> None:
    """Tier 2: read_cache on invalid JSON returns None and logs a warning."""
    path = cache_file_path(tmp_path / "state")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("NOT VALID JSON }{", encoding="utf-8")
    import logging
    with caplog.at_level(logging.WARNING, logger="reyn.chat.services.mcp_cache_file"):
        result = read_cache(path)
    assert result is None
    assert any("mcp_cache_file" in r.name for r in caplog.records), (
        "expected a warning from the mcp_cache_file logger"
    )


# ---------------------------------------------------------------------------
# read_cache — version mismatch
# ---------------------------------------------------------------------------


def test_read_version_mismatch_returns_none(tmp_path: Path) -> None:
    """Tier 2: read_cache silently returns None when version != 1."""
    path = cache_file_path(tmp_path / "state")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 99, "probed_at": "2026-01-01T00:00:00+00:00", "servers": {}}
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = read_cache(path)
    assert result is None


# ---------------------------------------------------------------------------
# write_cache — parent dir creation
# ---------------------------------------------------------------------------


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    """Tier 2: write_cache creates missing parent directories."""
    deep_state = tmp_path / "new_parent" / "nested" / "state"
    path = cache_file_path(deep_state)
    assert not deep_state.exists()
    write_cache(path, {"s": []})
    assert path.exists(), "write_cache must create missing parent dirs"


# ---------------------------------------------------------------------------
# write_cache — atomicity (no partial file on failure)
# ---------------------------------------------------------------------------


def test_atomic_write_on_failure_no_partial_file(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: when os.replace raises after the .tmp write, the .json file
    is not created (= atomic guarantee: no partial writes reach the target)."""
    path = cache_file_path(tmp_path / "state")
    path.parent.mkdir(parents=True, exist_ok=True)

    original_replace = os.replace

    def _fail_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _fail_replace)

    with pytest.raises(OSError):
        write_cache(path, _SAMPLE_SERVERS)

    monkeypatch.setattr(os, "replace", original_replace)

    assert not path.exists(), (
        "target file must not exist after a failed os.replace (= no partial write)"
    )
    # .tmp file may or may not exist depending on implementation; we only
    # care that the target is absent.


# ---------------------------------------------------------------------------
# file_mtime
# ---------------------------------------------------------------------------


def test_file_mtime_returns_none_when_absent(tmp_path: Path) -> None:
    """Tier 2: file_mtime returns None for a nonexistent path."""
    result = file_mtime(tmp_path / "nonexistent.json")
    assert result is None


def test_file_mtime_returns_float_for_existing_file(tmp_path: Path) -> None:
    """Tier 2: file_mtime returns a positive float for an existing file."""
    path = tmp_path / "test.json"
    path.write_text("{}", encoding="utf-8")
    mtime = file_mtime(path)
    assert mtime is not None
    assert isinstance(mtime, float)
    assert mtime > 0
