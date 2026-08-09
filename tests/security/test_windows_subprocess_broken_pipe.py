"""Tier 2: Windows broken-pipe fix — thread-based subprocess drain + killpg fallback.

On Windows ``select()`` cannot poll pipe fds (``OSError [WinError 10038] not a socket``), so
``communicate_capped`` drains via one thread per stream instead of the POSIX selectors loop. The
thread-reader is CONTRACT-IDENTICAL to the selectors path (per-stream cap→truncated, drain-and-
discard past the cap so the child never blocks, ``TimeoutExpired`` carrying partial output) — the
reader is cross-platform, so parity is asserted DIRECTLY on the dev env. The real-Windows check
(the actual broken-pipe gone) is the OWNER'S real-env gate; it is NOT faked with importorskip.

Also: ``kill_process_tree`` (the shared cancel/timeout reaper) falls back to the Windows tree-kill
path when ``os.killpg`` is absent. Deep tree-kill coverage lives in
``test_windows_kill_tree_2292_2715.py``.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reyn.security.sandbox import _subprocess_io
from reyn.security.sandbox._subprocess_io import (
    _communicate_capped_selectors,
    _communicate_capped_threads,
    communicate_capped,
)


def _popen(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


# ── the thread-reader (cross-platform, exercised directly on the dev env) ─────────────────────


def test_threads_drain_full_output():
    """Tier 2: the thread-reader drains stdout + stderr fully (under cap), rc preserved."""
    p = _popen("import sys; sys.stdout.write('hello'); sys.stderr.write('world')")
    out, err, trunc = _communicate_capped_threads(p, input=None, max_bytes=10**7, timeout=None)
    assert (out, err, trunc) == (b"hello", b"world", False)
    assert p.returncode == 0


def test_threads_caps_per_stream_and_is_drain_safe():
    """Tier 2: each stream is capped independently; past the cap the reader keeps draining-and-
    discarding so the child never blocks on a full pipe → its return code is preserved."""
    p = _popen("import sys; sys.stdout.write('A' * 100000); sys.stderr.write('B' * 50)")
    out, err, trunc = _communicate_capped_threads(p, input=None, max_bytes=1000, timeout=None)
    assert out == b"A" * 1000, "stdout capped to the first max_bytes (cap keeps the prefix)"
    assert err == b"B" * 50, "stderr under its own cap → full"
    assert trunc is True, "truncated flag set when a stream exceeds the cap"
    assert p.returncode == 0, "drain-safe: the 100k-writing child did not deadlock on a full pipe"


def test_threads_stdin_write():
    """Tier 2: the stdin-write thread feeds input without deadlocking the concurrent reads."""
    p = _popen("import sys; sys.stdout.write(sys.stdin.read().upper())")
    out, _err, _trunc = _communicate_capped_threads(p, input=b"abc", max_bytes=10**7, timeout=None)
    assert out == b"ABC"


def test_threads_timeout_raises_with_partial_output():
    """Tier 2: a still-draining stream at the deadline → TimeoutExpired carrying the partial output
    (parity with the selectors path)."""
    p = _popen("import sys, time; sys.stdout.write('partial'); sys.stdout.flush(); time.sleep(30)")
    try:
        with pytest.raises(subprocess.TimeoutExpired) as ei:
            _communicate_capped_threads(p, input=None, max_bytes=10**7, timeout=0.5)
        assert b"partial" in (ei.value.output or b""), "partial output must be carried on timeout"
    finally:
        p.kill()
        p.wait()


# ── contract parity: thread path == selectors path (lead's required guard) ────────────────────


@pytest.mark.parametrize("max_bytes,expect_trunc", [(2000, True), (10**7, False)])
def test_parity_threads_match_selectors(max_bytes, expect_trunc):
    """Tier 2: the thread path and the selectors path produce IDENTICAL (stdout, stderr, truncated)
    for the same child — so the Windows path behaves exactly like POSIX, just via threads."""
    code = "import sys; sys.stdout.write('X' * 5000); sys.stderr.write('Y' * 30)"
    r_threads = _communicate_capped_threads(_popen(code), input=None, max_bytes=max_bytes, timeout=None)
    r_selectors = _communicate_capped_selectors(_popen(code), input=None, max_bytes=max_bytes, timeout=None)
    assert r_threads == r_selectors, (
        f"thread path must match selectors exactly; threads={r_threads} selectors={r_selectors}"
    )
    assert r_threads[2] is expect_trunc


# ── platform dispatch ─────────────────────────────────────────────────────────────────────────


def test_dispatch_win32_uses_thread_reader(monkeypatch):
    """Tier 2: on win32, communicate_capped routes to the thread-reader (NOT the pipe-incompatible
    selectors path). Spy delegates to the real reader — not a mock."""
    called: dict[str, bool] = {}
    real = _subprocess_io._communicate_capped_threads

    def spy(*a, **k):
        called["threads"] = True
        return real(*a, **k)

    monkeypatch.setattr(_subprocess_io, "_communicate_capped_threads", spy)
    monkeypatch.setattr(_subprocess_io.sys, "platform", "win32")
    out, _err, _trunc = communicate_capped(_popen("print('ok')"), max_bytes=10**7)
    assert called.get("threads"), "win32 MUST use the thread-reader"
    assert out.strip() == b"ok"


def test_dispatch_posix_keeps_selectors(monkeypatch):
    """Tier 2: on POSIX, communicate_capped keeps the proven selectors path (no thread overhead)."""
    called: dict[str, bool] = {}
    real = _subprocess_io._communicate_capped_selectors

    def spy(*a, **k):
        called["selectors"] = True
        return real(*a, **k)

    monkeypatch.setattr(_subprocess_io, "_communicate_capped_selectors", spy)
    monkeypatch.setattr(_subprocess_io.sys, "platform", "linux")
    communicate_capped(_popen("print('ok')"), max_bytes=10**7)
    assert called.get("selectors"), "POSIX MUST keep the selectors path"


# ── kill_process_tree: killpg (POSIX) vs terminate/tree-kill (no-killpg / Windows) ────────────


@pytest.mark.asyncio
async def test_kill_process_tree_posix_uses_killpg():
    """Tier 2: with os.killpg present (POSIX), the process is killed via the group."""
    from reyn.security.sandbox import kill_process_tree
    p = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True,
    )
    await kill_process_tree(p, grace_seconds=1.0)
    assert p.poll() is not None, "the process must be killed via killpg"


@pytest.mark.asyncio
async def test_kill_process_tree_without_killpg_falls_back_to_terminate(monkeypatch):
    """Tier 2: when os.killpg is absent (Windows), fall back to the terminate/tree-kill path. RED if
    the guard weren't present (os.killpg would AttributeError and the process would never be
    killed)."""
    from reyn.security.sandbox import _subprocess_io
    monkeypatch.delattr(_subprocess_io.os, "killpg", raising=False)
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    await _subprocess_io.kill_process_tree(p, grace_seconds=1.0)
    assert p.poll() is not None, "without killpg, the terminate/tree-kill path must still kill it"
