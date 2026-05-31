"""Tier 2: FP-0008 #1115 Stage 2 — per-run backend injection threading.

The C7 harness injects ONE dual-Protocol container backend at both run-level
seams. This file pins that an injected backend actually reaches those seams
through the Agent → OSRuntime → {Workspace (FS), OpContext (exec)} threading
added for Stage 2:

  (a) an injected ``environment_backend`` reaches Workspace FS ops (so repo
      file ops would hit the container);
  (b) an injected ``sandbox_backend`` reaches the sandboxed_exec OpContext (so
      exec would hit the same container) — both via ControlIRExecutor and via
      PreprocessorExecutor.

No mocks: a real recording EnvironmentBackend Fake + the (real, non-mock)
SandboxBackend stub pattern from test_op_sandboxed_exec.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.environment.host_backend import HostBackend
from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.kernel.runtime import OSRuntime
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.sandbox.backend import SandboxResult
from reyn.schemas.models import Phase, RunOpStep, SandboxedExecIROp, Skill, SkillGraph
from reyn.workspace.workspace import Workspace


class _RecordingEnvBackend:
    """Real EnvironmentBackend Fake recording which ops it served (delegates IO)."""

    name = "recording"

    def __init__(self) -> None:
        self._inner = HostBackend()
        self.calls: list[str] = []

    def read_bytes(self, p): self.calls.append("read_bytes"); return self._inner.read_bytes(p)
    def write_bytes(self, p, d): self.calls.append("write_bytes"); self._inner.write_bytes(p, d)
    def delete(self, p): self.calls.append("delete"); return self._inner.delete(p)
    def mkdir(self, p, *, parents=True): self.calls.append("mkdir"); return self._inner.mkdir(p, parents=parents)
    def move(self, s, d): self.calls.append("move"); return self._inner.move(s, d)
    def stat(self, p): self.calls.append("stat"); return self._inner.stat(p)
    def glob(self, pat, *, root=None): self.calls.append("glob"); return self._inner.glob(pat, root=root)
    def grep(self, root, rx, **kw): self.calls.append("grep"); return self._inner.grep(root, rx, **kw)


class _StubExecBackend:
    """Real (non-mock) SandboxBackend stub — proves the injected exec backend ran."""

    name = "stub-injected"

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None) -> SandboxResult:
        return SandboxResult(returncode=0, stdout=b"from-stub", stderr=b"")


def _one_phase_skill() -> Skill:
    p = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name="thread_test", entry_phase="draft", phases={"draft": p},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}}, final_output_name="result",
    )


def test_osruntime_threads_environment_backend_to_workspace(tmp_path: Path) -> None:
    """Tier 2: (a) an injected environment_backend serves the run's workspace FS."""
    rec = _RecordingEnvBackend()
    rt = OSRuntime(
        _one_phase_skill(), model="stub/model", run_id="r1",
        workspace_base_dir=tmp_path, environment_backend=rec,
    )
    rt.workspace.write_file("note.txt", "hi")
    content, found = rt.workspace.read_file("note.txt")

    assert (content, found) == ("hi", True)
    assert "write_bytes" in rec.calls and "read_bytes" in rec.calls


def _sandboxed_exec_executor(tmp_path: Path) -> ControlIRExecutor:
    events = EventLog()
    return ControlIRExecutor(
        workspace=Workspace(events=events, base_dir=tmp_path),
        events=events,
        permission_resolver=PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False),
        skill_name="thread_test",
        sandbox_backend=_StubExecBackend(),
    )


def test_control_ir_executor_threads_sandbox_backend_to_op_context(tmp_path: Path) -> None:
    """Tier 2: (b) an injected sandbox_backend reaches the sandboxed_exec OpContext.

    Run a sandboxed_exec op through the executor (constructed with the injected
    backend) and assert the injected instance ran — proving _build_ctx threaded
    sandbox_backend onto the OpContext.
    """
    executor = _sandboxed_exec_executor(tmp_path)
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/echo", "x"],
        env_passthrough=["PATH"], timeout_seconds=10,
    )
    results = asyncio.run(
        executor.execute([op], phase="p", decl=PermissionDecl(), allowed_ops={"sandboxed_exec"})
    )
    [res] = results
    assert res["backend"] == "stub-injected"
    assert res["stdout"] == "from-stub"


def test_preprocessor_executor_accepts_sandbox_backend(tmp_path: Path) -> None:
    """Tier 2: (b) PreprocessorExecutor threads sandbox_backend onto its OpContext.

    A preprocessor sandboxed_exec must hit the same injected backend, so the
    executor stores it and builds it into the op context. Run a one-step
    preprocessor with a sandboxed_exec run_op and assert the injected backend ran.
    """
    from reyn.events.events import EventLog

    events = EventLog()
    skill = _one_phase_skill()
    phase = Phase(
        name="pp", instructions="d", input_schema={"type": "object", "properties": {}},
        allowed_ops=["sandboxed_exec"],
        preprocessor=[
            RunOpStep(
                type="run_op",
                op=SandboxedExecIROp(
                    kind="sandboxed_exec", argv=["/bin/echo", "x"],
                    env_passthrough=["PATH"], timeout_seconds=10,
                ),
                into="data._exec",
            )
        ],
    )
    pe = PreprocessorExecutor(
        skill=skill,
        workspace=Workspace(events=events, base_dir=tmp_path),
        model="standard", events=events, subscribers=[], resolver=None,
        permission_resolver=PermissionResolver(config_permissions={}, project_root=tmp_path, interactive=False),
        sandbox_backend=_StubExecBackend(),
    )
    enriched, _usage = asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    assert enriched["data"]["_exec"]["backend"] == "stub-injected"
