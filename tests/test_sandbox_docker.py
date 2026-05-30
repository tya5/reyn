"""Tier 2: DockerSandboxBackend orchestration + availability invariants (FP-0008 C7 #2).

DockerSandboxBackend runs argv inside a Docker container, clean-resetting the
container repo to ``base_ref`` and applying the host workspace diff before each
run. The live ``docker exec`` path requires a Docker daemon (validated by the
PR-B e2e faithful run, not here); these tests pin the docker-independent
contract via the public ``run()`` surface with an injected recording runner:

- Protocol conformance + name.
- ``available()`` reports False when the docker binary is absent.
- ``run()`` issues host-diff → clean-reset → apply → exec in order, with the
  base_ref / repo_dir / container / timeout / stdin threaded correctly.
- ``run()`` skips ``git apply`` when the host diff is empty.
- ``run()`` short-circuits (surfacing the real returncode) when host-diff,
  clean-reset, or apply fails.

No mocks of collaborators — the runner double is a real recording callable
(like NoopBackend, not a MagicMock).
"""
from __future__ import annotations

import pytest

from reyn.sandbox import SandboxBackend, SandboxPolicy, SandboxResult
from reyn.sandbox.backends.docker import DockerSandboxBackend


class _RecordingRunner:
    """Real (non-mock) runner double: records calls, returns queued results.

    Defaults to a success result when the queue is exhausted so a test only
    needs to enqueue the results it cares about.
    """

    def __init__(self, results: list[SandboxResult] | None = None) -> None:
        self.calls: list[dict] = []
        self._results: list[SandboxResult] = list(results) if results else []

    async def __call__(
        self,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: int | None = None,
    ) -> SandboxResult:
        self.calls.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        if self._results:
            return self._results.pop(0)
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


def _backend(runner: _RecordingRunner) -> DockerSandboxBackend:
    return DockerSandboxBackend(
        container="cont",
        repo_dir="/testbed",
        base_ref="BASECOMMIT",
        host_workspace_dir="/host/ws",
        runner=runner,
    )


# ── Protocol + availability ──────────────────────────────────────────────────


def test_protocol_conformance_and_name():
    """Tier 2: DockerSandboxBackend conforms to SandboxBackend with name 'docker'."""
    backend = _backend(_RecordingRunner())
    assert isinstance(backend, SandboxBackend)
    assert backend.name == "docker"


def test_available_false_when_docker_binary_missing():
    """Tier 2: available() is False when the docker binary is not on PATH."""
    backend = DockerSandboxBackend(
        container="c",
        repo_dir="/testbed",
        base_ref="b",
        host_workspace_dir="/ws",
        docker_bin="reyn-nonexistent-docker-xyz",
    )
    assert backend.available() is False


# ── run() orchestration ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_orchestrates_diff_reset_apply_exec_in_order():
    """Tier 2: run() issues host-diff → reset → clean → apply → exec with correct args."""
    runner = _RecordingRunner(
        results=[
            SandboxResult(0, b"DIFFBYTES", b""),  # host diff (non-empty)
            SandboxResult(0, b"", b""),  # reset
            SandboxResult(0, b"", b""),  # clean
            SandboxResult(0, b"", b""),  # apply
            SandboxResult(0, b"PYTEST-OUT", b""),  # exec
        ]
    )
    backend = _backend(runner)

    result = await backend.run(["pytest", "-x"], SandboxPolicy(timeout_seconds=42))

    assert result.returncode == 0
    assert result.stdout == b"PYTEST-OUT"

    argvs = [c["argv"] for c in runner.calls]

    # 1. host diff vs base, on the host clone
    assert argvs[0] == ["git", "-C", "/host/ws", "diff", "BASECOMMIT"]
    # 2. clean-reset to base in the container
    assert argvs[1][:2] == ["docker", "exec"]
    assert {"reset", "--hard", "BASECOMMIT"} <= set(argvs[1])
    assert "/testbed" in argvs[1]
    # 3. clean -fd
    assert {"clean", "-fd"} <= set(argvs[2])
    # 4. apply receives the diff via stdin
    assert "apply" in argvs[3]
    assert runner.calls[3]["stdin"] == b"DIFFBYTES"
    # 5. exec the caller argv (cwd=repo_dir) is the FINAL step, under the timeout
    assert argvs[-1] == ["docker", "exec", "-w", "/testbed", "cont", "pytest", "-x"]
    assert runner.calls[-1]["timeout"] == 42


@pytest.mark.asyncio
async def test_run_skips_apply_when_diff_empty():
    """Tier 2: run() skips `git apply` when the host diff is empty (clean base)."""
    runner = _RecordingRunner(
        results=[
            SandboxResult(0, b"", b""),  # host diff EMPTY
            SandboxResult(0, b"", b""),  # reset
            SandboxResult(0, b"", b""),  # clean
            SandboxResult(0, b"OUT", b""),  # exec
        ]
    )
    backend = _backend(runner)

    result = await backend.run(["pytest"], SandboxPolicy())

    assert result.stdout == b"OUT"
    argvs = [c["argv"] for c in runner.calls]
    # apply step is absent (empty diff) and exec still runs as the final step
    assert not any("apply" in a for a in argvs)
    assert argvs[-1] == ["docker", "exec", "-w", "/testbed", "cont", "pytest"]


@pytest.mark.asyncio
async def test_run_short_circuits_on_host_diff_failure():
    """Tier 2: run() surfaces host-diff failure and stops before touching the container."""
    runner = _RecordingRunner(results=[SandboxResult(128, b"", b"not a git repo")])
    backend = _backend(runner)

    result = await backend.run(["pytest"], SandboxPolicy())

    assert result.returncode == 128
    assert b"host diff failed" in result.stderr
    # stopped before touching the container (no docker exec issued)
    assert not any(c["argv"][:2] == ["docker", "exec"] for c in runner.calls)


@pytest.mark.asyncio
async def test_run_short_circuits_on_reset_failure():
    """Tier 2: run() surfaces clean-reset failure and does not exec the command."""
    runner = _RecordingRunner(
        results=[
            SandboxResult(0, b"DIFF", b""),  # host diff
            SandboxResult(1, b"", b"reset boom"),  # reset fails
        ]
    )
    backend = _backend(runner)

    result = await backend.run(["pytest"], SandboxPolicy())

    assert result.returncode == 1
    assert b"container clean-reset failed" in result.stderr
    # the caller command never ran (short-circuited at clean-reset)
    assert not any("pytest" in c["argv"] for c in runner.calls)


@pytest.mark.asyncio
async def test_run_short_circuits_on_apply_failure():
    """Tier 2: run() surfaces `git apply` failure and does not exec the command."""
    runner = _RecordingRunner(
        results=[
            SandboxResult(0, b"DIFF", b""),  # host diff
            SandboxResult(0, b"", b""),  # reset
            SandboxResult(0, b"", b""),  # clean
            SandboxResult(1, b"", b"patch does not apply"),  # apply fails
        ]
    )
    backend = _backend(runner)

    result = await backend.run(["pytest"], SandboxPolicy())

    assert result.returncode == 1
    assert b"git apply failed" in result.stderr
    # the caller command never ran (short-circuited at git apply)
    assert not any("pytest" in c["argv"] for c in runner.calls)
