"""Tier 2: OS invariant — SandboxBackend.wrap_command() uniformity (#2620).

Every sandbox backend must implement ``wrap_command(argv, policy) ->
WrappedCommand`` so a PERSISTENT-subprocess launch (stdio MCP) can be
command-level wrapped without any agent-reachable caller bypassing the
abstraction. This pins the per-backend contract directly (independent of the
MCPClient caller, covered separately in test_mcp_client_sandbox_wrap.py):

- NoopBackend: argv unchanged, no cleanup — passthrough THROUGH the
  abstraction, not a bypass.
- SeatbeltBackend: prepends sandbox-exec -f <profile>; cleanup unlinks the
  temp profile.
- LandlockBackend: prepends the landlock_exec re-exec shim; no cleanup
  resource owned.

No mocks — real backend instances throughout.
"""
from __future__ import annotations

import sys
from pathlib import Path

from reyn.security.sandbox.backend import WrappedCommand
from reyn.security.sandbox.backends.landlock import LandlockBackend
from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
from reyn.security.sandbox.noop_backend import NoopBackend
from reyn.security.sandbox.policy import SandboxPolicy


def test_noop_wrap_command_returns_argv_unchanged():
    """Tier 2: NoopBackend.wrap_command is a passthrough — argv comes back
    byte-identical and no cleanup resource is allocated. This is the
    owner-acceptable no-enforcement outcome (#2620): the call went THROUGH
    wrap_command, it just enforces nothing."""
    backend = NoopBackend()
    argv = ["my-server", "--flag", "value"]
    wrapped = backend.wrap_command(argv, SandboxPolicy())
    assert isinstance(wrapped, WrappedCommand)
    assert wrapped.argv == argv
    assert wrapped.argv is not argv  # defensive copy, not aliasing the input
    assert wrapped.cleanup is None


def test_noop_wrap_command_does_not_mutate_input_argv():
    """Tier 2: mutating the returned argv must not alias the caller's list."""
    backend = NoopBackend()
    argv = ["cmd", "a"]
    wrapped = backend.wrap_command(argv, SandboxPolicy())
    wrapped.argv.append("b")
    assert argv == ["cmd", "a"]  # caller's list untouched


def test_seatbelt_wrap_command_prepends_sandbox_exec(tmp_path):
    """Tier 2: SeatbeltBackend.wrap_command prepends sandbox-exec -f <profile>,
    the profile is a real deny-default SBPL, and the trailing argv is the
    original command unchanged."""
    backend = SeatbeltBackend()
    wrapped = backend.wrap_command(["my-server", "--flag"], SandboxPolicy())
    assert wrapped.argv[0] == "sandbox-exec"
    assert wrapped.argv[1] == "-f"
    profile_path = Path(wrapped.argv[2])
    assert profile_path.suffix == ".sb"
    assert wrapped.argv[3:] == ["my-server", "--flag"]
    profile = profile_path.read_text()
    assert "(deny default)" in profile

    assert wrapped.cleanup is not None
    assert profile_path.exists()
    wrapped.cleanup()
    assert not profile_path.exists()  # cleanup unlinks the temp profile


def test_seatbelt_wrap_command_cleanup_idempotent():
    """Tier 2: calling cleanup twice must not raise (best-effort unlink)."""
    backend = SeatbeltBackend()
    wrapped = backend.wrap_command(["cmd"], SandboxPolicy())
    wrapped.cleanup()
    wrapped.cleanup()  # must not raise on a missing file


def test_landlock_wrap_command_uses_reexec_shim():
    """Tier 2: LandlockBackend.wrap_command wraps as the landlock_exec re-exec
    shim (python -m reyn.security.sandbox.landlock_exec --policy ... -- cmd
    args) — the COMMAND-level analog of the Seatbelt wrap. No cleanup
    resource is owned."""
    backend = LandlockBackend()
    wrapped = backend.wrap_command(["my-server", "--flag"], SandboxPolicy())
    assert wrapped.argv[0] == sys.executable
    assert wrapped.argv[1:3] == ["-m", "reyn.security.sandbox.landlock_exec"]
    sep = wrapped.argv.index("--")
    assert wrapped.argv[sep + 1:] == ["my-server", "--flag"]
    assert wrapped.cleanup is None
