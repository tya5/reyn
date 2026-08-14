"""Tier 2: #4676 — the 4 history_tail_reader read functions
(`read_last_line`/`read_history_tail`/`read_history_after`/
`read_history_before`) had the SAME TOCTOU shape #4671 fixed in
`history_file_stats`: `path.is_file()` checked BEFORE `stat()`/`open()`,
so a file vanishing in that gap raised `FileNotFoundError` in exactly
the case each function's own docstring promises a "missing" result for
(`None`, `[]`, `([], False)`, `[]` respectively).

Measured before fixing (#4676's own first step, per this repo's
"measure before build" discipline): unreachable WITHIN one process
(every call site here is synchronous with no thread-offload, and the
only in-session unlinker, `/clear-history`, is equally synchronous —
Python's single-threaded cooperative asyncio model makes two such
blocks structurally unable to interleave); reachable ACROSS processes
(`path_locks.py`'s own docstring: reyn's path-lock is deliberately
in-process only per ADR-0018, "cross-process mutual exclusion is
deferred to the A2A-server model"; no PID/socket single-instance guard
exists at session startup) — two `reyn` processes attached to the same
agent is unguarded, so one process's `/clear-history` racing another
process's own history read is real, if narrow.

The FIX's own reason does not rest on that reachability finding, only on
each function's own documented contract (mirroring #4671's own framing):
catch `FileNotFoundError` only — any OTHER `OSError` (a permission error
being the real-world example) must propagate, never be silently
misreported as an empty/missing file (D-1: measure, don't fake).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.history_tail_reader import (
    read_history_after,
    read_history_before,
    read_history_tail,
    read_last_line,
)


def _write_history(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _open_raises_after_stat_succeeded(hist: Path, monkeypatch) -> None:
    """Simulate the race: `stat()` (or the generator's own `stat()`)
    succeeds — the file existed a moment ago — but `open()` then raises
    `FileNotFoundError`, exactly as it would if the file vanished in the
    gap. Only the TARGET path is intercepted; every other `Path.open`
    call (including this test's own setup writes) is unaffected."""
    real_open = Path.open

    def _raiser(self, *args, **kwargs):
        if self == hist:
            raise FileNotFoundError(f"simulated race: {self} vanished before open()")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raiser)


def _stat_raises_permission_error(hist: Path, monkeypatch) -> None:
    real_stat = Path.stat

    def _raiser(self, *args, **kwargs):
        if self == hist:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _raiser)


# ── read_last_line ───────────────────────────────────────────────────────


def test_read_last_line_missing_file_returns_none(tmp_path: Path):
    """Tier 2: no history.jsonl yet — None, not an error (accept-side)."""
    assert read_last_line(tmp_path / "does-not-exist" / "history.jsonl") is None


def test_read_last_line_vanishing_between_stat_and_open_still_returns_none(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: #4676 — a real, existing file whose `open()` raises
    `FileNotFoundError` (vanished after `stat()` succeeded) still
    returns None, proving the fix wraps stat+open in ONE try, not just
    a preceding entry check."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    _open_raises_after_stat_succeeded(hist, monkeypatch)

    assert read_last_line(hist) is None


def test_read_last_line_permission_error_is_not_swallowed(tmp_path: Path, monkeypatch):
    """Tier 2: #4676 — only FileNotFoundError (the TOCTOU race) is
    treated as "vanished". Any other OSError (permission, the
    real-world example) must propagate, not be silently reported as a
    missing file (D-1: measure, don't fake)."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    _stat_raises_permission_error(hist, monkeypatch)

    with pytest.raises(PermissionError):
        read_last_line(hist)


# ── read_history_tail ────────────────────────────────────────────────────


def test_read_history_tail_missing_file_returns_empty(tmp_path: Path):
    """Tier 2: no history.jsonl yet — [], not an error (accept-side)."""
    assert read_history_tail(tmp_path / "does-not-exist" / "history.jsonl") == []


def test_read_history_tail_vanishing_mid_generator_still_returns_empty(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: #4676 — `_iter_raw_lines_reverse` (the shared
    backward-reader generator) calls `path.stat()` at the START of its
    FIRST iteration — raising there, from inside the caller's `for`
    loop, must be caught by the SAME `try` the caller wraps the loop
    in."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])

    real_stat = Path.stat

    def _raiser(self, *args, **kwargs):
        if self == hist:
            raise FileNotFoundError(f"simulated race: {self} vanished before stat()")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _raiser)

    assert read_history_tail(hist) == []


def test_read_history_tail_permission_error_is_not_swallowed(tmp_path: Path, monkeypatch):
    """Tier 2: #4676 — a permission error must propagate, not be
    silently reported as an empty file."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    _stat_raises_permission_error(hist, monkeypatch)

    with pytest.raises(PermissionError):
        read_history_tail(hist)


# ── read_history_after ───────────────────────────────────────────────────


def test_read_history_after_missing_file_returns_empty_not_truncated(tmp_path: Path):
    """Tier 2: no history.jsonl yet — ([], False), not an error
    (accept-side)."""
    lines, truncated = read_history_after(
        tmp_path / "does-not-exist" / "history.jsonl", after_seq=0,
    )
    assert (lines, truncated) == ([], False)


def test_read_history_after_vanishing_between_stat_and_open_still_returns_empty(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: #4676 — a real, existing file whose `open()` raises
    `FileNotFoundError` still returns ([], False), not a raise."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    _open_raises_after_stat_succeeded(hist, monkeypatch)

    assert read_history_after(hist, after_seq=0) == ([], False)


def test_read_history_after_permission_error_is_not_swallowed(tmp_path: Path, monkeypatch):
    """Tier 2: #4676 — a permission error must propagate, not be
    silently reported as an empty/no-content read."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])

    real_open = Path.open

    def _raiser(self, *args, **kwargs):
        if self == hist:
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raiser)

    with pytest.raises(PermissionError):
        read_history_after(hist, after_seq=0)


# ── read_history_before ──────────────────────────────────────────────────


def test_read_history_before_missing_file_returns_empty(tmp_path: Path):
    """Tier 2: no history.jsonl yet — [], not an error (accept-side)."""
    assert read_history_before(
        tmp_path / "does-not-exist" / "history.jsonl", before_seq=100,
    ) == []


def test_read_history_before_vanishing_mid_generator_still_returns_empty(
    tmp_path: Path, monkeypatch,
):
    """Tier 2: #4676 — same shared-generator shape as
    `read_history_tail`'s own race test: `_iter_raw_lines_reverse`'s
    `stat()` raising must be caught by the caller's `try`."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])

    real_stat = Path.stat

    def _raiser(self, *args, **kwargs):
        if self == hist:
            raise FileNotFoundError(f"simulated race: {self} vanished before stat()")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _raiser)

    assert read_history_before(hist, before_seq=100) == []


def test_read_history_before_permission_error_is_not_swallowed(tmp_path: Path, monkeypatch):
    """Tier 2: #4676 — a permission error must propagate, not be
    silently reported as an empty file."""
    hist = tmp_path / "history.jsonl"
    _write_history(hist, ['{"seq": 1}'])
    _stat_raises_permission_error(hist, monkeypatch)

    with pytest.raises(PermissionError):
        read_history_before(hist, before_seq=100)
