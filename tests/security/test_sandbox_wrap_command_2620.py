"""Tier 2: OS invariant — SandboxBackend.wrap_command() uniformity (#2620).

Every sandbox backend must implement ``wrap_command(argv, policy) ->
WrappedCommand`` so a PERSISTENT-subprocess launch (stdio MCP) can be
command-level wrapped without any agent-reachable caller bypassing the
abstraction. This pins the per-backend contract directly (independent of the
MCPClient caller, covered separately in test_mcp_client_sandbox_wrap.py):

- NoopBackend: argv unchanged, no cleanup — passthrough THROUGH the
  abstraction, not a bypass.
- SeatbeltBackend: prepends sandbox-exec -f <profile>. #4434 (stage 1): for a
  policy whose write scope provably excludes the session-cache directory, the
  profile is a SHARED, session-lifetime file and cleanup() is a no-op (a
  second caller reusing the same policy still needs it); only when that
  precondition can't be proven does cleanup() unlink a private, per-call
  file — see test_sandbox_seatbelt.py's own #4434 section for both cases.
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


def test_seatbelt_wrap_command_prepends_sandbox_exec():
    """Tier 2: SeatbeltBackend.wrap_command prepends sandbox-exec -f <profile>,
    the profile is a real deny-default SBPL, and the trailing argv is the
    original command unchanged.

    #4434 (stage 1): a bare ``SandboxPolicy()`` (empty write_paths) is safe
    to session-cache, so its profile is SHARED — cleanup() on it is a no-op
    by design (a second caller reusing this policy still needs the file);
    see test_seatbelt_wrap_command_does_not_cache_when_write_scope_is_unsafe
    in test_sandbox_seatbelt.py for the DOES-unlink case."""
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
    assert profile_path.exists()  # cached (session-lifetime): cleanup() is a no-op

    import os

    os.unlink(profile_path)  # tidy up the shared cache file this test wrote


def test_seatbelt_wrap_command_cleanup_idempotent():
    """Tier 2: calling cleanup twice must not raise, on both the cached
    (no-op) path and the uncached (best-effort unlink) path."""
    from reyn.security.sandbox.backends.seatbelt import _seatbelt_cache_dir

    backend = SeatbeltBackend()

    cached = backend.wrap_command(["cmd"], SandboxPolicy())
    cached.cleanup()
    cached.cleanup()  # no-op both times — must not raise

    uncached = backend.wrap_command(
        ["cmd"], SandboxPolicy(write_paths=[str(_seatbelt_cache_dir())]),
    )
    uncached.cleanup()
    uncached.cleanup()  # must not raise on a missing file


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


# ── #3822: wrap_command() honestly returns env, not just argv — every
# persistent-process caller (CodeAct, MCP stdio) that only reads .argv and
# separately forgets to call resolve_passthrough_env silently inherits an env
# built OUTSIDE the shared contract (#3822's own finding, twice). These pin
# that wrap_command's env matches the SAME deny-list run() already uses, for
# every backend that implements it.
#
# #3901 PR-B ④ (owner ruling B, full compat): env is now a DENY-list
# (env_deny_names), empty by default = the WHOLE parent environment passes
# through — the opposite polarity from the old env_passthrough allow-list
# these tests originally pinned. The invariant these tests protect (env
# actually flows through the shared resolve_passthrough_env contract, not a
# backend-local shortcut) is unchanged; only which direction "explicit" moves
# the value flipped. ──────────────────────────────────────────────────────


def test_noop_wrap_command_env_passes_through_by_default(monkeypatch):
    """Tier 2: #3822/#3901 — a parent env var reaches wrapped.env under the bare
    compat default (env_deny_names empty), even on NoopBackend (no OS
    isolation, but the env-scoping contract is unrelated to OS enforcement).
    Strip the resolve_passthrough_env call in NoopBackend.wrap_command and
    this goes RED (env would then come from a raw os.environ copy or similar,
    not the shared contract — indistinguishable here, but the deny leg below
    would catch that divergence)."""
    monkeypatch.setenv("REYN_3822_WRAP_TEST_MARKER", "should-pass-through")
    backend = NoopBackend()
    wrapped = backend.wrap_command(["cmd"], SandboxPolicy())
    assert wrapped.env.get("REYN_3822_WRAP_TEST_MARKER") == "should-pass-through"


def test_seatbelt_wrap_command_env_passes_through_by_default(monkeypatch):
    """Tier 2: #3822/#3901 — same invariant as the Noop case above, for Seatbelt."""
    monkeypatch.setenv("REYN_3822_WRAP_TEST_MARKER", "should-pass-through")
    backend = SeatbeltBackend()
    wrapped = backend.wrap_command(["cmd"], SandboxPolicy())
    assert wrapped.env.get("REYN_3822_WRAP_TEST_MARKER") == "should-pass-through"
    wrapped.cleanup()


def test_landlock_wrap_command_env_passes_through_by_default(monkeypatch):
    """Tier 2: #3822/#3901 — same invariant as the Noop case above, for Landlock."""
    monkeypatch.setenv("REYN_3822_WRAP_TEST_MARKER", "should-pass-through")
    backend = LandlockBackend()
    wrapped = backend.wrap_command(["cmd"], SandboxPolicy())
    assert wrapped.env.get("REYN_3822_WRAP_TEST_MARKER") == "should-pass-through"


def test_wrap_command_env_honors_a_declared_deny_name(monkeypatch):
    """Tier 2: #3822/#3901 — a name IN policy.env_deny_names does NOT reach
    wrapped.env (the deny-list gates, it does not blanket-allow) — checked
    once across all 3 real backends so the positive default (above) and this
    opt-out leg both have a witness."""
    monkeypatch.setenv("REYN_3822_WRAP_TEST_DENIED", "should-not-pass-through")
    policy = SandboxPolicy(env_deny_names=["REYN_3822_WRAP_TEST_DENIED"])
    for backend in (NoopBackend(), SeatbeltBackend(), LandlockBackend()):
        wrapped = backend.wrap_command(["cmd"], policy)
        assert "REYN_3822_WRAP_TEST_DENIED" not in wrapped.env, (
            f"{backend.name}: declared env_deny_names name reached wrapped.env"
        )
        if wrapped.cleanup is not None:
            wrapped.cleanup()
