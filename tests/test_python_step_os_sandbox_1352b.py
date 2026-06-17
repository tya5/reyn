"""Tier 2: python step routes through the OS sandbox backend (#1352-B).

The audit (#1352) found python preprocessor/postprocessor steps ran with only
restricted-builtins + a soft write_paths cap — no OS sandbox. Owner directive:
python steps must run under the sandbox (file-sharing substrate). PythonRunner.run
now routes the `_python_harness` subprocess through `SandboxBackend.run` when a
real backend is configured (Seatbelt / Landlock / container — same model as
sandboxed_exec); noop / None falls back to a direct subprocess (unchanged).

No mocks of real collaborators: a real `_RecordingBackend` implementing the
SandboxBackend interface (name + available + run) captures the routed argv +
policy and returns a canned harness-success result (it stands in for the OS
backend without actually applying isolation, which is platform-specific).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.python_runner import PythonRunner
from reyn.security.sandbox.backend import SandboxResult


class _RecordingBackend:
    """Real (non-mock) SandboxBackend stand-in: records run() args, returns a
    canned harness-success payload so the routing is observable without applying
    platform-specific isolation."""

    name = "seatbelt"  # pretend a real backend (not "noop")

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None, cancel_event=None) -> SandboxResult:
        self.calls.append({"argv": list(argv), "policy": policy, "stdin": stdin, "cwd": cwd})
        payload = {"ok": True, "result": {"echoed": "ok"}}
        return SandboxResult(
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8"),
            stderr=b"",
        )


def _write_safe_module(skill_dir: Path) -> None:
    """A trivial safe-mode module (re/json only) — its body never runs here
    (the recording backend short-circuits), it only needs to resolve."""
    (skill_dir / "mod.py").write_text(
        "def go(artifact):\n    return {'echoed': 'ok'}\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_python_step_routes_through_sandbox_backend(tmp_path: Path) -> None:
    """Tier 2: #1352-B reproduce-first — with a real backend, the harness
    subprocess is routed through backend.run (OS sandbox), carrying the agent
    policy (write_paths) + the per-step timeout. FAILS pre-B (run was sync +
    ignored sandbox_backend → direct subprocess; backend.run never called)."""
    _write_safe_module(tmp_path)
    backend = _RecordingBackend()
    runner = PythonRunner()

    result = await runner.run(
        skill_dir=tmp_path,
        module="./mod.py",
        function="go",
        mode="safe",
        artifact={"data": {}},
        timeout=30,
        sandbox_backend=backend,
        sandbox_policy={"write_paths": [str(tmp_path)], "network": False},
    )

    # routed through the OS backend (not a direct subprocess)
    assert backend.calls, "harness must be routed through backend.run"
    call = backend.calls[0]
    # the harness module is the routed command (behavioral, not arg-format pin)
    assert "reyn.core.kernel._python_harness" in call["argv"]
    # agent policy caps reached the backend; per-step timeout overrode policy
    assert call["policy"].write_paths == [str(tmp_path)]
    assert call["policy"].network is False
    assert call["policy"].timeout_seconds == 30
    # the harness JSON request was passed on stdin
    assert call["stdin"] is not None
    # the canned harness result is parsed + returned
    assert result == {"echoed": "ok"}


@pytest.mark.asyncio
async def test_noop_backend_falls_back_to_direct_subprocess(tmp_path: Path) -> None:
    """Tier 2: #1352-B — a noop backend (or None) does NOT route through backend.run;
    the direct (unsandboxed) subprocess path is used (unchanged behavior). The
    recording backend's run must never be called."""

    class _NoopRecording(_RecordingBackend):
        name = "noop"

    _write_safe_module(tmp_path)
    backend = _NoopRecording()
    runner = PythonRunner()

    result = await runner.run(
        skill_dir=tmp_path,
        module="./mod.py",
        function="go",
        mode="safe",
        artifact={"data": {}},
        timeout=30,
        sandbox_backend=backend,
        sandbox_policy={"write_paths": [str(tmp_path)]},
    )
    assert not backend.calls  # noop → not routed through the backend
    assert result == {"echoed": "ok"}  # ran via the real direct subprocess
