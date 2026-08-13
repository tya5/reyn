"""Tier 2: #4364 PR-2 (C-1) — ``probe_argv`` + each backend's own
``probe_binary()``.

Real backends, real subprocess launches — no mocks. ``NoopBackend`` is
always available on every platform (the same falsification vehicle
``test_sandbox_self_test_2983.py`` uses), so the ``None``-return path is
platform-independent and never silently skips. The real-launch paths
(Seatbelt/Landlock) are platform-gated the same way the rest of
``tests/security/`` already gates its own backend-specific tests.
"""
from __future__ import annotations

import asyncio
import platform

import pytest

from reyn.security.sandbox import NoopBackend
from reyn.security.sandbox.backend import find_posix_true_binary
from reyn.security.sandbox.policy import SandboxPolicy
from reyn.security.sandbox.probe_argv import probe_argv


def _run(coro):
    return asyncio.run(coro)


# ── find_posix_true_binary — the shared control-binary lookup ───────────────


def test_find_posix_true_binary_resolves_a_real_executable_file():
    """Tier 2: on any POSIX host running this suite, the lookup must find
    a REAL, executable file (never a guessed, unverified path)."""
    from pathlib import Path

    found = find_posix_true_binary()
    assert found is not None
    (path_str,) = found
    path = Path(path_str)
    assert path.is_file()
    import os
    assert os.access(path, os.X_OK)


# ── NoopBackend — the platform-independent None-return witness ──────────────


def test_noop_backend_probe_binary_is_none():
    """Tier 2: NoopBackend enforces nothing, so it has nothing to
    differentiate a probe against — ``probe_binary()`` must say so
    directly, not fall through to a real lookup."""
    assert NoopBackend().probe_binary() is None


def test_probe_argv_against_noop_backend_returns_none_without_launching_anything():
    """Tier 2: (accept-side) ``probe_argv`` degrades to ``None`` cleanly
    when the backend itself cannot support a probe — never raises, never
    guesses a result."""
    result = _run(probe_argv(NoopBackend(), ["/usr/bin/true"], SandboxPolicy()))
    assert result is None


def test_probe_argv_with_empty_argv_returns_none():
    """Tier 2: (accept-side) nothing to probe -> None, before ever asking
    the backend for its control binary."""
    result = _run(probe_argv(NoopBackend(), [], SandboxPolicy()))
    assert result is None


# ── A real backend (whichever this host actually has) — the launch witness ──


def _live_probing_backend():
    """The real, available sandbox backend for THIS host — Seatbelt on
    macOS, Landlock on Linux — or ``None`` if this platform has neither
    (skip target, not a failure)."""
    system = platform.system()
    if system == "Darwin":
        from reyn.security.sandbox.backends.seatbelt import SeatbeltBackend
        backend = SeatbeltBackend()
    elif system == "Linux":
        from reyn.security.sandbox.backends.landlock import LandlockBackend
        backend = LandlockBackend()
    else:
        return None
    return backend if backend.available() else None


def test_docker_backend_probe_binary_is_none():
    """Tier 2: DockerEnvironmentBackend cannot assume anything about the
    operator-configured image's own filesystem — ``probe_binary()`` must
    say ``None`` rather than guess a host-side path that has no bearing on
    the container's contents."""
    from reyn.environment.container_backend import DockerEnvironmentBackend

    # Construction alone touches no daemon (no real Docker connection is
    # made until a run/exec call) — a real instance, not a bypassed one.
    backend = DockerEnvironmentBackend(container="unused", repo_dir="/repo")
    assert backend.probe_binary() is None


def test_probe_binary_of_the_live_backend_is_a_real_runnable_control():
    """Tier 2: whichever real backend this host has, its OWN
    ``probe_binary()`` must resolve to something (this host is expected to
    have ``/usr/bin/true`` or ``/bin/true`` — CI images do)."""
    backend = _live_probing_backend()
    if backend is None:
        pytest.skip("no real sandbox backend available on this platform")
    assert backend.probe_binary() is not None


def test_falsify_probe_argv_distinguishes_a_runnable_target_from_a_missing_one(tmp_path):
    """Tier 2: LOAD-BEARING falsification — the SAME real backend, the
    SAME real policy, probing a genuinely runnable argv[0] (its own good
    binary) returns "ok", and probing a nonexistent one returns
    "target_failed" — the pair proves the differential mechanism actually
    discriminates, not just returns a fixed answer for any input."""
    backend = _live_probing_backend()
    if backend is None:
        pytest.skip("no real sandbox backend available on this platform")
    good = backend.probe_binary()
    if good is None:
        pytest.skip("this host's backend has no control binary to probe with")
    policy = SandboxPolicy(write_paths=[str(tmp_path)])

    ok_result = _run(probe_argv(backend, good, policy))
    missing_result = _run(
        probe_argv(backend, ["/definitely/not/a/real/binary-4364-probe-argv"], policy),
    )

    assert ok_result == "ok"
    assert missing_result == "target_failed"


def test_probe_argv_never_passes_configured_arguments_to_the_target(tmp_path):
    """Tier 2: LOAD-BEARING falsification for D-2 — argv[0] is probed
    ALONE. A script that exits 0 with no args and 1 with any arg proves
    this: if the probe forwarded the configured extra argument, this
    would report "target_failed" instead of "ok"."""
    backend = _live_probing_backend()
    if backend is None:
        pytest.skip("no real sandbox backend available on this platform")

    script = tmp_path / "argv0_only.sh"
    script.write_text(
        "#!/bin/sh\nif [ \"$#\" -eq 0 ]; then exit 0; else exit 1; fi\n", encoding="utf-8",
    )
    script.chmod(0o755)
    policy = SandboxPolicy(write_paths=[str(tmp_path)])

    result = _run(probe_argv(backend, [str(script), "--would-fail-if-passed"], policy))

    assert result == "ok", (
        f"expected 'ok' (argv[0] alone exits 0) — got {result!r}, meaning the "
        "configured extra argument reached the target"
    )
