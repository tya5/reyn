"""DockerSandboxBackend — execute argv inside a prepared Docker container.

A :class:`~reyn.sandbox.backend.SandboxBackend` that runs commands inside an
already-created Docker container, syncing a host git workspace into the
container's repo directory before each run. On every ``run()`` the container's
``repo_dir`` is **clean-reset** to ``base_ref`` and the host workspace's current
diff (vs ``base_ref``) is applied, so the command observes the host's cumulative
changes layered on top of the container's prepared environment — preserving any
built dependencies (compiled C extensions, installed packages) that live in the
image but not in the host clone.

Construction is caller-driven: whoever owns the container + host workspace (e.g.
an evaluation harness) builds the instance and injects it via
``OpContext.sandbox_backend``. This module is **axis-agnostic / P7-clean** — it
contains no skill names, phase names, or benchmark-specific strings. It is a
generic exec backend bound to a ``(container, repo_dir, base_ref, workspace)``
tuple, and doubles as a real isolation backend for ``sandboxed_exec``.

The actual subprocess execution is delegated to an injectable ``runner`` so the
``run()`` orchestration (host-diff → clean-reset → apply → exec) is testable
without a live Docker daemon. The default runner uses ``asyncio`` subprocesses.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Awaitable, Callable

from reyn.sandbox.backend import SandboxResult
from reyn.sandbox.policy import SandboxPolicy

# A runner executes one argv (optionally feeding *stdin* bytes, optionally
# bounded by *timeout* seconds) and returns a SandboxResult. Injected so run()
# is unit-testable without Docker; the default is _subprocess_runner.
Runner = Callable[..., Awaitable[SandboxResult]]


async def _subprocess_runner(
    argv: list[str],
    *,
    stdin: bytes | None = None,
    timeout: int | None = None,
) -> SandboxResult:
    """Real runner: spawn argv as an asyncio subprocess and capture output."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input=stdin), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return SandboxResult(
            returncode=-1,
            stdout=b"",
            stderr=f"Command timed out after {timeout}s".encode(),
        )
    return SandboxResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out or b"",
        stderr=err or b"",
    )


class DockerSandboxBackend:
    """Run argv inside a Docker container with a host git workspace synced in.

    Args:
        container:           Container name or id to ``docker exec`` into.
        repo_dir:            Path of the git repo *inside* the container
                             (e.g. ``/testbed``). reset/apply/exec target it.
        base_ref:            Commit the container repo is reset to before each
                             run (diff-accumulation guard).
        host_workspace_dir:  Host path of the git clone whose diff (vs
                             ``base_ref``) is applied into the container.
        docker_bin/git_bin:  Binary names (overridable for tests / non-PATH).
        runner:              Injected executor (defaults to ``_subprocess_runner``).
    """

    name: str = "docker"

    def __init__(
        self,
        *,
        container: str,
        repo_dir: str,
        base_ref: str,
        host_workspace_dir: str,
        docker_bin: str = "docker",
        git_bin: str = "git",
        runner: Runner | None = None,
    ) -> None:
        self.container = container
        self.repo_dir = repo_dir
        self.base_ref = base_ref
        self.host_workspace_dir = host_workspace_dir
        self.docker_bin = docker_bin
        self.git_bin = git_bin
        self._runner: Runner = runner or _subprocess_runner

    # ── command construction ─────────────────────────────────────────────────

    def _host_diff_argv(self) -> list[str]:
        # Cumulative host diff (tracked changes) vs the base commit, on the host.
        return [self.git_bin, "-C", self.host_workspace_dir, "diff", self.base_ref]

    def _reset_argv(self) -> list[str]:
        return [
            self.docker_bin, "exec", self.container,
            self.git_bin, "-C", self.repo_dir, "reset", "--hard", self.base_ref,
        ]

    def _clean_argv(self) -> list[str]:
        return [
            self.docker_bin, "exec", self.container,
            self.git_bin, "-C", self.repo_dir, "clean", "-fd",
        ]

    def _apply_argv(self) -> list[str]:
        # The diff is fed to `git apply` via stdin (`docker exec -i`).
        return [
            self.docker_bin, "exec", "-i", self.container,
            self.git_bin, "-C", self.repo_dir, "apply",
        ]

    def _exec_argv(self, argv: list[str]) -> list[str]:
        # Run the caller's command with cwd = repo_dir inside the container.
        return [self.docker_bin, "exec", "-w", self.repo_dir, self.container, *argv]

    # ── availability ─────────────────────────────────────────────────────────

    def available(self) -> bool:
        """True when the docker binary exists and the daemon is reachable."""
        if shutil.which(self.docker_bin) is None:
            return False
        try:
            completed = subprocess.run(
                [self.docker_bin, "info"],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return completed.returncode == 0

    # ── execution ────────────────────────────────────────────────────────────

    async def run(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        *,
        stdin: bytes | None = None,
    ) -> SandboxResult:
        """Clean-reset the container repo, apply the host diff, then exec argv.

        Steps (each via the injected runner):
          1. capture the host diff (vs base_ref);
          2. ``git reset --hard base_ref`` + ``git clean -fd`` in the container
             (prevents cross-call diff accumulation);
          3. ``git apply`` the host diff (skipped when the diff is empty);
          4. exec argv with cwd=repo_dir under ``policy.timeout_seconds``.

        Any setup-step failure short-circuits with that step's returncode so the
        caller sees the real cause rather than a misleading test result.
        """
        diff_res = await self._runner(self._host_diff_argv())
        if diff_res.returncode != 0:
            return SandboxResult(
                returncode=diff_res.returncode or 1,
                stdout=b"",
                stderr=b"host diff failed: " + diff_res.stderr,
            )
        diff = diff_res.stdout

        for step_argv in (self._reset_argv(), self._clean_argv()):
            res = await self._runner(step_argv)
            if res.returncode != 0:
                return SandboxResult(
                    returncode=res.returncode or 1,
                    stdout=b"",
                    stderr=b"container clean-reset failed: " + res.stderr,
                )

        if diff.strip():
            apply_res = await self._runner(self._apply_argv(), stdin=diff)
            if apply_res.returncode != 0:
                return SandboxResult(
                    returncode=apply_res.returncode or 1,
                    stdout=b"",
                    stderr=b"git apply failed: " + apply_res.stderr,
                )

        return await self._runner(
            self._exec_argv(list(argv)),
            stdin=stdin,
            timeout=policy.timeout_seconds,
        )
