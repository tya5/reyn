"""Tier 2: Seatbelt enforces deny_subprocess (was advisory-only) — #1914.

Before #1914 the SBPL profile emitted ``(allow process-fork)`` unconditionally, so
``deny_subprocess=True`` was advisory on macOS (subprocess spawn still worked).
The fix gates ``(allow process-fork)`` on ``policy.deny_subprocess``; ``process-exec*``
stays always-allowed (sandbox-exec needs it to execvp the target). Empirically
(sandbox-exec probe, py3.9/3.12 + sh/bash/node) the interpreter + threading run
fine without process-fork — only child spawning (which needs fork) is denied.
Linux-parity with the seccomp gate.

Two tiers of coverage:
- structural (CI-safe everywhere): the profile string gates process-fork.
- behavioral (darwin-only): sandbox-exec actually blocks a child spawn.

Policy: real _build_sbpl_profile + real SeatbeltBackend.run (no mocks). Tier first.

★ CI-visibility gap (#3881, owner ruling 2026-09-04: no macOS CI runner will
be added — every job in this repo's CI runs on ubuntu-latest): 1 of this
module's 3 tests (the "behavioral" one above) is Darwin-gated
(`skipif(sys.platform != "darwin", ...)`) and has therefore NEVER executed
in CI. A green CI run of this module does not mean that one passed — it
means it never ran. It exercises only on a real macOS machine.
"""
from __future__ import annotations

import sys

import pytest

from reyn.security.sandbox.backends.seatbelt import (
    SeatbeltBackend,
    _build_sbpl_profile,
)
from reyn.security.sandbox.policy import SandboxPolicy

# ── structural (CI-safe) ─────────────────────────────────────────────────────

def test_profile_denies_process_fork_when_subprocess_disallowed():
    """Tier 2: deny_subprocess=True → explicit (deny process-fork). The bsd.sb
    base GRANTS fork, so a last-match-wins deny is required to override it (mere
    omission is insufficient); (allow process-exec*) stays for the initial exec."""
    profile = _build_sbpl_profile(SandboxPolicy(deny_subprocess=True))
    assert "(deny process-fork)" in profile
    assert "(allow process-fork)" not in profile
    assert "(allow process-exec*)" in profile


def test_profile_allows_process_fork_when_subprocess_allowed():
    """Tier 2: deny_subprocess=False → (allow process-fork) emitted (spawning
    permitted), no (deny process-fork), process-exec* present."""
    profile = _build_sbpl_profile(SandboxPolicy(deny_subprocess=False))
    assert "(allow process-fork)" in profile
    assert "(deny process-fork)" not in profile
    assert "(allow process-exec*)" in profile


# ── behavioral (darwin-only: sandbox-exec is macOS-only) ─────────────────────

@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS-only")
@pytest.mark.asyncio
async def test_seatbelt_blocks_child_spawn_when_subprocess_disallowed():
    """Tier 2: with deny_subprocess=True the sandboxed process cannot spawn a
    child (fork denied); with deny_subprocess=False it can. The target shell runs
    either way (its own exec is governed by process-exec*, not fork)."""
    backend = SeatbeltBackend()
    if not backend.available():
        pytest.skip("sandbox-exec not available on this machine")

    # A pipeline forces a real fork; a single command would be exec-optimized by
    # the shell (self-replacement, not a child spawn) and wouldn't exercise fork.
    spawn_cmd = ["/bin/sh", "-c", "/bin/echo SPAWNED_CHILD | /bin/cat"]

    denied = await backend.run(
        spawn_cmd, SandboxPolicy(deny_subprocess=True, timeout_seconds=10)
    )
    assert b"SPAWNED_CHILD" not in denied.stdout, (
        f"child spawn must be blocked when deny_subprocess=True (stdout={denied.stdout!r})"
    )

    allowed = await backend.run(
        spawn_cmd, SandboxPolicy(deny_subprocess=False, timeout_seconds=10)
    )
    assert b"SPAWNED_CHILD" in allowed.stdout, (
        f"child spawn must work when deny_subprocess=False (stdout={allowed.stdout!r})"
    )
