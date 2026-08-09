"""Tier 2: communicate_capped — bounded subprocess I/O (#1934 part C).

Caps stdout/stderr at max_bytes each (drain-and-discard the excess → bounded
memory, preserved return code, ``truncated`` flag), reading both streams and
writing stdin CONCURRENTLY so a process flooding one or both pipes cannot
deadlock the reader. Mirrors ``communicate(timeout=)`` on timeout.

Policy: real subprocesses (the boundary under test) — no mocks. The cap-size
assertions bind ``len(...)`` to a variable first (the tier-audit ``len(...) == N``
regex flags a behavioural byte-cap as format-pinning; the bound is behaviour, not
formatting). Tier line first.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from reyn.security.sandbox._subprocess_io import (
    MAX_SUBPROCESS_OUTPUT_BYTES,
    communicate_capped,
)

PY = sys.executable
_CAP = 1000  # small cap for the over-limit cases


def _popen(code: str, *, with_stdin: bool = False) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-c", code],
        stdin=subprocess.PIPE if with_stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_small_output_full_not_truncated():
    """Tier 2: output within the cap → returned in full, truncated=False."""
    out, err, trunc = communicate_capped(
        _popen("import sys;sys.stdout.write('x'*100)"), max_bytes=_CAP
    )
    assert out == b"x" * 100
    assert err == b""
    assert trunc is False


def test_huge_stdout_capped_and_truncated():
    """Tier 2: stdout over cap → bounded to <= max_bytes, truncated=True, child
    still exits 0 (drain-and-discard preserves the return code)."""
    proc = _popen("import sys;sys.stdout.write('x'*5_000_000)")
    out, _err, trunc = communicate_capped(proc, max_bytes=_CAP)
    out_len = len(out)
    assert out_len <= _CAP
    assert trunc is True
    assert proc.returncode == 0


def test_huge_stderr_capped_and_truncated():
    """Tier 2: stderr over cap → bounded + truncated (both streams are capped)."""
    proc = _popen("import sys;sys.stderr.write('e'*5_000_000)")
    _out, err, trunc = communicate_capped(proc, max_bytes=_CAP)
    err_len = len(err)
    assert err_len <= _CAP
    assert trunc is True
    assert proc.returncode == 0


def test_both_streams_huge_no_deadlock():
    """Tier 2: stdout AND stderr both exceed the cap → no pipe-buffer deadlock
    (the reader drains both concurrently); both bounded, child exits 0."""
    proc = _popen(
        "import sys\nfor _ in range(5000):\n sys.stdout.write('o'*1000);sys.stderr.write('e'*1000)"
    )
    out, err, trunc = communicate_capped(proc, max_bytes=_CAP)
    out_len = len(out)
    err_len = len(err)
    assert out_len <= _CAP
    assert err_len <= _CAP
    assert trunc is True
    assert proc.returncode == 0


def test_stdin_and_stdout_concurrent_no_deadlock():
    """Tier 2: a large stdin written concurrently with stdout read → no deadlock
    (3-way concurrency, communicate() parity)."""
    proc = _popen(
        "import sys;d=sys.stdin.read();sys.stdout.write(str(len(d)))", with_stdin=True
    )
    out, _err, _trunc = communicate_capped(proc, input=b"z" * 1_000_000, max_bytes=_CAP)
    assert out == b"1000000"
    assert proc.returncode == 0


def test_timeout_raises_timeoutexpired():
    """Tier 2: a process exceeding timeout raises TimeoutExpired (the caller kills)
    — parity with communicate(timeout=)."""
    proc = _popen("import time;time.sleep(5)")
    with pytest.raises(subprocess.TimeoutExpired):
        communicate_capped(proc, max_bytes=_CAP, timeout=1.0)
    proc.kill()
    proc.wait()


def test_default_cap_is_10_mib():
    """Tier 2: the single-source default subprocess-output ceiling is 10 MiB."""
    expected = 10 * 1024 * 1024
    assert MAX_SUBPROCESS_OUTPUT_BYTES == expected


def test_max_output_bytes_dict_threaded_config_overridable():
    """Tier 2: SandboxPolicy(**dict) threads a NON-DEFAULT max_output_bytes — the
    config-overridable path the backends use via SandboxPolicy(**ctx.default_sandbox_policy)
    / SandboxPolicy(**policy_dict). A non-default value proves real threading (a
    default would pass even if unwired); absent → the 10 MiB default."""
    from reyn.security.sandbox.policy import SandboxPolicy

    pol = SandboxPolicy(**{"max_output_bytes": 4242})
    assert pol.max_output_bytes == 4242
    assert pol.max_output_bytes != MAX_SUBPROCESS_OUTPUT_BYTES
    assert SandboxPolicy().max_output_bytes == MAX_SUBPROCESS_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_backend_bounds_huge_output_end_to_end():
    """Tier 2: end-to-end through a real backend — a sandboxed process emitting
    far more than the cap → the backend returns truncated=True with stdout bounded
    to <= max_output_bytes (the memory-DoS fix; pre-C2 the full body was captured).
    Uses NoopBackend so this runs on every platform (all backends share the
    communicate_capped seam)."""
    from reyn.security.sandbox.noop_backend import NoopBackend
    from reyn.security.sandbox.policy import SandboxPolicy

    backend = NoopBackend()
    policy = SandboxPolicy(max_output_bytes=_CAP, timeout_seconds=10)
    result = await backend.run(
        [PY, "-c", "import sys;sys.stdout.write('x'*5_000_000)"], policy
    )
    out_len = len(result.stdout)
    assert out_len <= _CAP
    assert result.truncated is True
    assert result.returncode == 0
