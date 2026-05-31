"""Tier 2: FP-0008 #1115 Stage 2 (β2b) — swe_bench_runner per-instance container lifecycle.

``run_reyn_in_container`` owns the per-instance Docker lifecycle for a faithful
in-container run: ``docker run -d`` the pre-built image → ``reyn run swe_bench``
with the generic ``--env-backend=docker`` flags → ``docker rm -f`` (always).

Lifecycle (docker) is exercised via an injectable recording runner — no daemon.
The reyn subprocess is a small recording shim that captures the flags it received
and prints a Final Output block, so the flag wiring + patch extraction are pinned
end-to-end without a real reyn install. No mocks. Docstrings open "Tier 2:".
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

from scripts.swe_bench_runner import run_reyn_in_container

_INSTANCE = {
    "instance_id": "marshmallow-code__marshmallow-1359",
    "repo": "marshmallow-code/marshmallow",
    "base_commit": "abc123",
    "problem_statement": "Fix a bug.",
    "test_patch": "diff --git a/tests/t.py b/tests/t.py",
}


class _RecordingDocker:
    """Injectable docker runner: records argv, returns a configurable result."""

    def __init__(self, *, run_rc: int = 0, run_stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._run_rc = run_rc
        self._run_stderr = run_stderr

    def __call__(self, argv, *, timeout=180):
        self.calls.append(list(argv))
        is_run = "run" in argv
        return types.SimpleNamespace(
            returncode=self._run_rc if is_run else 0,
            stdout="container_id_123\n",
            stderr=self._run_stderr if is_run else "",
        )


def _recording_reyn(tmp_path: Path):
    """A reyn shim that writes its received argv to a file and prints a patch.

    Returns ``(reyn_base, argv_out_path)``.
    """
    script = tmp_path / "rec_reyn.py"
    script.write_text(
        "import sys, os, json\n"
        "open(os.environ['REYN_ARGV_OUT'], 'w', encoding='utf-8')"
        ".write('\\n'.join(sys.argv[1:]))\n"
        "print('=== Final Output ===')\n"
        "print(json.dumps({'patch': 'diff --git a/f b/f\\n--- a/f\\n+++ b/f\\n'}))\n",
        encoding="utf-8",
    )
    out = tmp_path / "argv.txt"
    base = ["env", f"REYN_ARGV_OUT={out}", sys.executable, str(script)]
    return base, out


def test_container_lifecycle_happy_path(tmp_path: Path) -> None:
    """Tier 2: docker run → reyn → docker rm, returns ok+patch, run precedes rm, same name."""
    docker = _RecordingDocker()
    reyn_base, _argv_out = _recording_reyn(tmp_path)

    result = run_reyn_in_container(
        _INSTANCE,
        image="swebench/sweb.eval.x86_64.example:latest",
        repo_dir="/testbed",
        state_dir=str(tmp_path / "state"),
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    assert result["ok"] is True, f"expected ok, got {result!r}"
    assert result["patch"].startswith("diff --git a/f b/f")

    # Behavior: a `docker run` precedes a `docker rm -f` for the SAME container.
    run_idx = next(i for i, c in enumerate(docker.calls) if c[:2] == ["docker", "run"])
    rm_idx = next(i for i, c in enumerate(docker.calls) if c[:3] == ["docker", "rm", "-f"])
    assert run_idx < rm_idx, f"container must be started before teardown: {docker.calls}"
    run_call, rm_call = docker.calls[run_idx], docker.calls[rm_idx]
    assert "swebench/sweb.eval.x86_64.example:latest" in run_call
    name = run_call[run_call.index("--name") + 1]
    assert rm_call[-1] == name, f"teardown must target the started container: {docker.calls}"


def test_container_passes_generic_env_backend_flags_to_reyn(tmp_path: Path) -> None:
    """Tier 2: reyn is invoked with the generic --env-backend=docker flags + the container name."""
    docker = _RecordingDocker()
    reyn_base, argv_out = _recording_reyn(tmp_path)
    state = str(tmp_path / "state")

    run_reyn_in_container(
        _INSTANCE,
        image="img:latest",
        repo_dir="/testbed",
        state_dir=state,
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    received = argv_out.read_text(encoding="utf-8").splitlines()
    name = docker.calls[0][docker.calls[0].index("--name") + 1]
    assert "--env-backend=docker" in received
    assert "--container" in received and received[received.index("--container") + 1] == name
    assert "--repo-dir" in received and received[received.index("--repo-dir") + 1] == "/testbed"
    assert "--state-dir" in received and received[received.index("--state-dir") + 1] == state
    # run_reyn appends the instance JSON as the final argv.
    assert any(_INSTANCE["instance_id"] in tok for tok in received)


def test_container_teardown_runs_even_on_reyn_failure(tmp_path: Path) -> None:
    """Tier 2: a reyn non-zero exit still tears the container down (finally)."""
    docker = _RecordingDocker()
    # A reyn base that exits non-zero (no Final Output).
    reyn_base = ["env", "X=1", sys.executable, "-c", "import sys; sys.exit(1)"]

    result = run_reyn_in_container(
        _INSTANCE,
        image="img:latest",
        state_dir=str(tmp_path / "s"),
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    assert result["ok"] is False
    # rm -f must still have been called after the failed reyn run.
    assert any(c[:3] == ["docker", "rm", "-f"] for c in docker.calls), docker.calls


def test_container_docker_run_failure_skips_reyn_and_teardown(tmp_path: Path) -> None:
    """Tier 2: a failed `docker run` returns an error and does not run reyn or rm."""
    docker = _RecordingDocker(run_rc=1, run_stderr="no such image")
    reyn_base, argv_out = _recording_reyn(tmp_path)

    result = run_reyn_in_container(
        _INSTANCE,
        image="img:missing",
        state_dir=str(tmp_path / "s"),
        reyn_base=reyn_base,
        timeout=30,
        docker_runner=docker,
    )

    assert result["ok"] is False
    assert "docker run failed" in result["error"]
    # Behavior: the run was attempted, but no teardown (nothing started) and
    # reyn was never invoked.
    assert any(c[:2] == ["docker", "run"] for c in docker.calls), docker.calls
    assert not any(c[:3] == ["docker", "rm", "-f"] for c in docker.calls), (
        f"no teardown when the container never started: {docker.calls}"
    )
    assert not argv_out.exists(), "reyn must not run when the container failed to start"


def test_runner_parser_has_container_flags() -> None:
    """Tier 2: build_parser wires the --env-backend/--image/--repo-dir/--state-dir flags."""
    import argparse
    import contextlib
    import io

    from scripts.swe_bench_runner import build_parser

    parser = build_parser()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
        parser.parse_args(["--help"])
    help_text = buf.getvalue()
    for flag in ("--env-backend", "--image", "--repo-dir", "--state-dir"):
        assert flag in help_text, f"{flag} must be wired in swe_bench_runner"
    assert isinstance(parser, argparse.ArgumentParser)
