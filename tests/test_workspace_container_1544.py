"""Tier 2: OS invariant — container shadow-git runner + registry wiring (#1544).

Real instances + a hand-written Fake backend (LLMReplay-style — NOT a MagicMock;
returns canned SandboxResults to exercise the runner's real argv/rc/decode logic).
Covers the deterministic core of #1544 increment-2:
  - _ContainerGitRunner builds the CONTAINER --git-dir/--work-tree path context
    and runs via backend.run (docker exec).
  - container git-absence (rc 127) → GitUnavailable → the store degrades.
  - AgentRegistry routes container mode (environment_backend.name == "container")
    to a container-backed WorkspaceVersionStore.

The full live container capture→restore round-trip (a real reyn-base container,
which ships git) is the (B) tmux live gate's job — a deterministic round-trip here
would need a git-containing image + bind-mount + network, which the
security-hardened launch (network off) + the git-less _E2E_IMAGE preclude.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.workspace_version_store import (
    GitUnavailable,
    WorkspaceVersionStore,
    _ContainerGitRunner,
)
from reyn.security.sandbox.backend import SandboxResult


class _FakeBackend:
    """Hand-written Fake of the FS+exec backend: canned ``run`` results + argv log.

    NOT a mock — it implements the real ``run(argv, policy, *, stdin, cwd)``
    contract and records argv so tests assert the runner's real behavior.
    """

    name = "container"
    repo_dir = "/workspace"

    def __init__(self, returncode: int = 0, stdout: bytes = b"") -> None:
        self._rc = returncode
        self._stdout = stdout
        self.calls: list[list[str]] = []

    async def run(self, argv, policy, *, stdin=None, cwd=None) -> SandboxResult:
        self.calls.append(list(argv))
        return SandboxResult(returncode=self._rc, stdout=self._stdout, stderr=b"")


# ── _ContainerGitRunner ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_container_runner_uses_container_path_context_via_backend():
    """Tier 2: the runner runs git in-container with the CONTAINER git-dir/work-tree."""
    backend = _FakeBackend(returncode=0, stdout=b"out")
    runner = _ContainerGitRunner(
        backend, git_dir="/workspace/.reyn/workspace-shadow.git", work_tree="/workspace",
    )

    rc, out = await runner.run(["tag", "--list"])

    assert rc == 0 and out == "out"           # stdout bytes decoded
    argv = backend.calls[0]
    assert argv[0] == "git"
    assert "--git-dir" in argv and "/workspace/.reyn/workspace-shadow.git" in argv
    assert "--work-tree" in argv and "/workspace" in argv   # CONTAINER paths, not host
    assert argv[-2:] == ["tag", "--list"]      # bare args appended


@pytest.mark.asyncio
async def test_container_runner_rc127_raises_git_unavailable():
    """Tier 2: container git-absence (rc 127 = command not found) → GitUnavailable.

    Checks git WHERE it runs (the container), not the host PATH — the #1544 (b)
    correctness point.
    """
    backend = _FakeBackend(returncode=127, stdout=b"")
    runner = _ContainerGitRunner(backend, git_dir="/c/.git", work_tree="/c")

    with pytest.raises(GitUnavailable):
        await runner.run(["add", "-A"])


@pytest.mark.asyncio
async def test_store_degrades_when_container_git_absent(tmp_path):
    """Tier 2: with a rc-127 container runner, the store degrades to no-ops (exec-time)."""
    backend = _FakeBackend(returncode=127)
    runner = _ContainerGitRunner(backend, git_dir="/c/.git", work_tree="/c")
    store = WorkspaceVersionStore(
        tmp_path, tmp_path / ".reyn" / "workspace-shadow.git", git_runner=runner,
    )

    # exec-time degrade (the runner raises GitUnavailable on the first git call).
    assert await store.capture(10) is None
    assert await store.seqs() == []
    assert await store.restore_to_seq(10) is None


# ── AgentRegistry container routing ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_routes_container_backend_through_backend_run(tmp_path):
    """Tier 2: container mode routes the store's git through backend.run (in-container).

    Behavioral: with environment_backend.name == "container", using the registry's
    workspace store issues git via the backend with the CONTAINER path context —
    proving the routing without asserting internal structure.
    """
    from reyn.chat.registry import AgentRegistry
    from reyn.events.state_log import StateLog

    backend = _FakeBackend(returncode=0, stdout=b"")
    reg = AgentRegistry(
        project_root=tmp_path,
        session_factory=lambda _p: (_ for _ in ()).throw(AssertionError("no factory")),
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        environment_backend=backend,
    )

    await reg.workspace_store.seqs()   # drives a git invocation through the backend

    assert backend.calls, "container store did not route git through backend.run"
    argv = backend.calls[0]
    assert argv[0] == "git"
    assert "/workspace/.reyn/workspace-shadow.git" in argv   # CONTAINER git-dir context
