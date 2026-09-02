"""Tier 2: DockerEnvironmentBackend's exec path has the same output cap every
other command-level launch route has (#3822's own measurement: Docker was
the one launch route still doing an unbounded ``capture_output=True`` /
``proc.communicate()`` read).

Real subprocess throughout — no Docker daemon required: ``_sync_runner`` /
``_async_runner`` spawn a real local ``argv`` (Docker's own run() would
prepend a ``docker exec`` wrapper onto the SAME argv shape; the cap lives in
the runner, independent of what's in front of it).
"""
from __future__ import annotations

import sys

import pytest

from reyn.environment.container_backend import (
    DockerEnvironmentBackend,
    _async_runner,
    _sync_runner,
)
from reyn.security.sandbox._subprocess_io import MAX_SUBPROCESS_OUTPUT_BYTES
from reyn.security.sandbox.policy import SandboxPolicy


def test_sync_runner_caps_output_and_reports_truncated():
    """Tier 2: #3822 — _sync_runner drains a real over-cap child through
    communicate_capped instead of an unbounded subprocess.run(capture_output=True)
    read. Strip the communicate_capped call (revert to subprocess.run(capture_output=True))
    and this goes RED — truncated would never be set and stdout would exceed the cap."""
    over = MAX_SUBPROCESS_OUTPUT_BYTES + 1024
    result = _sync_runner(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {over})"],
        max_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )
    assert result.truncated is True
    assert len(result.stdout) <= MAX_SUBPROCESS_OUTPUT_BYTES


def test_sync_runner_does_not_truncate_output_under_the_cap():
    """Tier 2: #3822 — ordinary, well-under-cap output is untouched (no
    false-positive truncation)."""
    result = _sync_runner([sys.executable, "-c", "print('ok')"])
    assert result.truncated is False
    assert result.stdout.strip() == b"ok"


@pytest.mark.asyncio
async def test_async_runner_caps_output_and_reports_truncated():
    """Tier 2: #3822 — _async_runner (what DockerEnvironmentBackend.run()
    actually calls) has the SAME cap as the sync path. Strip the
    communicate_capped call (revert to asyncio.create_subprocess_exec +
    plain .communicate()) and this goes RED."""
    over = MAX_SUBPROCESS_OUTPUT_BYTES + 1024
    result = await _async_runner(
        [sys.executable, "-c", f"import sys; sys.stdout.write('x' * {over})"],
        max_bytes=MAX_SUBPROCESS_OUTPUT_BYTES,
    )
    assert result.truncated is True
    assert len(result.stdout) <= MAX_SUBPROCESS_OUTPUT_BYTES


@pytest.mark.asyncio
async def test_docker_backend_run_passes_policy_max_output_bytes_through():
    """Tier 2: #3822 — DockerEnvironmentBackend.run() threads
    policy.max_output_bytes into the runner, not just a hardcoded default —
    an operator-lowered cap (set here well below the child's real output)
    actually takes effect end-to-end through the real backend, not just the
    runner function in isolation."""
    over = 2048
    backend = DockerEnvironmentBackend(container="unused", repo_dir="/unused")
    # No real docker binary is invoked: swap docker_bin for a no-op prefix so
    # the "docker exec ..." argv resolves to a real local command instead.
    # bash -lc 'exec "$@"' -- <argv> is argv-faithful (matches run()'s own
    # exec_argv shape), so this exercises the SAME construction run() builds,
    # just without a daemon.
    backend.docker_bin = "bash"
    backend.container = ""  # unused once docker_bin is bash; argv shape below ignores it

    async def _local_runner(argv, *, stdin=None, timeout=None, max_bytes=None, sink=None):
        # Strip everything before the real payload argv (mirrors the existing
        # "local runner Fake" pattern in test_container_backend_1115_stage2.py)
        # and run it for real, honoring max_bytes exactly like the production
        # runner would.
        payload = argv[argv.index("reyn-exec") + 1:]
        return await _async_runner(
            payload, stdin=stdin, timeout=timeout, max_bytes=max_bytes, sink=sink,
        )

    backend._runner = _local_runner
    policy = SandboxPolicy(timeout_seconds=30, max_output_bytes=over)
    result = await backend.run(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 8192)"], policy,
    )
    assert result.truncated is True
    assert len(result.stdout) <= over
