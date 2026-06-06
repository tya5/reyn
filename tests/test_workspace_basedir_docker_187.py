"""Tier 2: #187 — the chat OpContext Workspace roots file ops on the CONTAINER repo.

5th structural defect (sandbox_2 step-3, primary evidence): run-once
--env-backend=docker had the agent's file ops (read/grep/glob/edit) rooted on the
HOST reyn cwd, not the in-container repo (/testbed) — the env-backend was forwarded
to the Workspace but its PARTNER container base_dir was discarded
(``_ws_base_dir``), so the Workspace defaulted ``base_dir=Path.cwd()``. The agent's
FS ops and the exec/``git diff`` seam then disagreed on the filesystem (astropy
reads 0/134; grep matched reyn's own tests/venv; model_patch empty).

Round-trip acceptance (lead's load-bearing check): a write must land where
``git diff -C /testbed HEAD`` (the model_patch extraction) looks — i.e. file ops
must resolve to ``/testbed`` IN the container. Otherwise the agent edits host-side
and the patch is empty even when it "fixes" the file.
"""
from __future__ import annotations

from pathlib import Path

from reyn.environment.container_backend import DockerEnvironmentBackend
from reyn.sandbox.backend import SandboxResult
from reyn.workspace.workspace import Workspace


class _Events:
    def emit(self, *a, **k) -> None:  # Workspace emits op events
        pass


def _recording_docker(captured: list) -> DockerEnvironmentBackend:
    """A real DockerEnvironmentBackend whose fs_runner records the docker-exec
    argv (no real daemon) — so we can assert the in-container target path."""

    def _run(argv, stdin=None):
        captured.append({"argv": argv, "stdin": stdin})
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")

    return DockerEnvironmentBackend(container="c1", repo_dir="/testbed", fs_runner=_run)


def test_read_resolves_to_container_repo_not_host(tmp_path) -> None:
    """Tier 2: with base_dir=/testbed + a docker backend, a relative file read
    resolves to /testbed/<path> and executes IN the container (not the host cwd).
    state_dir is host-side (tmp_path) — decoupled from the container base_dir, as
    the real fix forwards it (else the OS would write under the in-container repo)."""
    captured: list = []
    ws = Workspace(
        events=_Events(), base_dir=Path("/testbed"), state_dir=tmp_path,
        environment_backend=_recording_docker(captured),
    )
    ws.read_file("astropy/io/ascii/html.py")
    argv = captured[-1]["argv"]
    assert argv[:2] == ["docker", "exec"] and "c1" in argv
    assert "/testbed/astropy/io/ascii/html.py" in " ".join(argv), (
        "read must target the container repo path, not the host cwd"
    )


def test_write_lands_in_container_repo_round_trip(tmp_path) -> None:
    """Tier 2: round-trip — a write resolves to /testbed/<path> in the container —
    where `git diff -C /testbed HEAD` (the model_patch extraction) looks. The
    in-container `docker exec -i` keeps stdin open for the write payload."""
    captured: list = []
    ws = Workspace(
        events=_Events(), base_dir=Path("/testbed"), state_dir=tmp_path,
        environment_backend=_recording_docker(captured),
    )
    ws.write_file("astropy/io/ascii/html.py", "fixed = True\n")
    argv = captured[-1]["argv"]
    assert "c1" in argv
    assert "/testbed/astropy/io/ascii/html.py" in " ".join(argv), (
        "write must land in the container repo (where git diff looks), not host"
    )
    assert "-i" in argv, "an in-container write keeps `docker exec -i` open for stdin"


def test_no_base_dir_defaults_to_host_cwd() -> None:
    """Tier 2: no base_dir (host backend / interactive chat) → cwd default,
    unchanged. The fix only takes effect when a container base_dir is forwarded."""
    ws = Workspace(events=_Events())
    assert ws.base_dir == Path.cwd()
