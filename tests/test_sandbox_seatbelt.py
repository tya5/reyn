"""Tier 2: SeatbeltBackend invariants (FP-0017 Component C)."""
from __future__ import annotations

import sys

import pytest

from reyn.sandbox.backend import SandboxBackend
from reyn.sandbox.backends.seatbelt import (
    SeatbeltBackend,
    _build_sbpl_profile,
    _sbpl_quote,
)
from reyn.sandbox.policy import SandboxPolicy

# ─── 1. Availability ──────────────────────────────────────────────────────────


def test_seatbelt_unavailable_on_non_darwin(monkeypatch):
    """Tier 2: SeatbeltBackend.available() returns False on non-Darwin platforms."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert SeatbeltBackend().available() is False


def test_seatbelt_unavailable_when_sandbox_exec_missing(monkeypatch):
    """Tier 2: SeatbeltBackend.available() returns False when sandbox-exec is absent."""
    import platform
    import shutil

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "mac_ver", lambda: ("14.5.0", ("", "", ""), ""))
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert SeatbeltBackend().available() is False


# ─── 2. SBPL profile generation ──────────────────────────────────────────────


def test_sbpl_profile_default_deny():
    """Tier 2: _build_sbpl_profile() always includes (deny default)."""
    policy = SandboxPolicy()
    profile = _build_sbpl_profile(policy)
    assert "(deny default)" in profile


def test_sbpl_profile_read_paths():
    """Tier 2: read_paths produce file-read* rules with the resolved absolute path."""
    from pathlib import Path

    raw = "/tmp/x"
    resolved = str(Path(raw).resolve(strict=False))
    policy = SandboxPolicy(read_paths=[raw])
    profile = _build_sbpl_profile(policy)
    assert "file-read*" in profile
    assert resolved in profile


def test_sbpl_profile_write_paths_imply_read():
    """Tier 2: write_paths produce both file-write* and file-read* rules for each path."""
    from pathlib import Path

    raw = "/tmp/y"
    resolved = str(Path(raw).resolve(strict=False))
    policy = SandboxPolicy(write_paths=[raw])
    profile = _build_sbpl_profile(policy)
    assert f"(allow file-write* (subpath \"{resolved}\"))" in profile
    # write_paths must also emit a file-read* rule for the same path.
    assert f"(allow file-read* (subpath \"{resolved}\"))" in profile


def test_sbpl_profile_network_allow():
    """Tier 2: network=True adds (allow network*); network=False omits it."""
    profile_allow = _build_sbpl_profile(SandboxPolicy(network=True))
    assert "(allow network*)" in profile_allow

    profile_deny = _build_sbpl_profile(SandboxPolicy(network=False))
    assert "(allow network*)" not in profile_deny


# ─── 3. _sbpl_quote ──────────────────────────────────────────────────────────


def test_sbpl_quote_escapes_quotes_and_backslashes():
    """Tier 2: _sbpl_quote escapes backslashes and double-quotes correctly."""
    result = _sbpl_quote('/tmp/foo"bar\\baz')
    # backslash → \\, double-quote → \"
    assert result == '"/tmp/foo\\"bar\\\\baz"'


# ─── 4. Execution (darwin-only) ───────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_runs_echo_under_sandbox():
    """Tier 2: SeatbeltBackend runs /bin/echo under sandbox and captures stdout."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(
        read_paths=["/bin", "/usr/lib", "/System/Library"],
        timeout_seconds=10,
    )
    result = await backend.run(["/bin/echo", "hello"], policy)
    assert result.returncode == 0, f"stderr: {result.stderr!r}"
    assert b"hello" in result.stdout


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_timeout_returns_minus_one():
    """Tier 2: SeatbeltBackend returns returncode=-1 when the process times out."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    policy = SandboxPolicy(
        read_paths=["/bin", "/usr/lib", "/System/Library"],
        timeout_seconds=1,
    )
    result = await backend.run(["/bin/sleep", "5"], policy)
    assert result.returncode == -1


# ─── 5. Protocol conformance ─────────────────────────────────────────────────


def test_seatbelt_conforms_to_sandbox_backend_protocol():
    """Tier 2: SeatbeltBackend satisfies the runtime-checkable SandboxBackend Protocol."""
    assert isinstance(SeatbeltBackend(), SandboxBackend)
