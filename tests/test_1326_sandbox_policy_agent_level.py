"""Tier 2: #1326 — sandbox policy is an agent-level (operator) concern.

The phase-scoped ``default_sandbox_policy`` (an FP-0017 remnant, declared only by
swe_bench phases whose policy was byte-identical across all 5) is **retired**.
Sandbox authorization now lives at the agent level — ``reyn.yaml sandbox.policy``
(an operator/run config), threaded by the OS:

  - ``SandboxConfig.policy`` (optional nested mapping; absent → None → the
    SandboxLayer stays ⊤, so any run declaring no policy is unchanged + op-level
    fields govern).
  - When set, it is the deterministic policy applied to sandboxed ops + the
    SandboxLayer of the permission ∩ — WINNING over op-declared fields (the LLM /
    a skill cannot widen it).
  - It is threaded OSRuntime → Phase / Orchestrator / pre- + post-processor, so it
    reaches the postprocessor path — whose ``_PostprocessorScope`` is skill-level
    (not a phase) — which is what makes the index write-gate fire there (#1321).

No mocks: a real recording SandboxBackend Fake (mirrors test_op_sandboxed_exec /
test_backend_injection_threading_1115_stage2) + a real recording PythonRunner
Fake capture the policy / cap at the seam.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from reyn.config import SandboxConfig, _build_sandbox_config
from reyn.events.events import EventLog
from reyn.kernel.postprocessor_executor import PostprocessorExecutor
from reyn.kernel.preprocessor_executor import PreprocessorExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.sandbox.backend import SandboxResult
from reyn.schemas.models import (
    Phase,
    Postprocessor,
    PythonStep,
    RunOpStep,
    SandboxedExecIROp,
    Skill,
    SkillGraph,
)
from reyn.workspace.workspace import Workspace

# A broad operator policy mirroring swe_bench's (the live eval-lane value).
_AGENT_POLICY = {
    "network": True,
    "read_paths": ["/agent"],
    "write_paths": ["/agent"],
    "allow_subprocess": True,
    "env_passthrough": ["PATH"],
    "timeout_seconds": 600,
}


# ── Tier 1: SandboxConfig.policy contract ────────────────────────────────────


def test_sandbox_config_parses_policy() -> None:
    """Tier 1: ``sandbox.policy`` parses into SandboxConfig.policy as a dict."""
    cfg = _build_sandbox_config({"policy": dict(_AGENT_POLICY)})
    assert cfg.policy == _AGENT_POLICY


def test_sandbox_config_absent_policy_is_none() -> None:
    """Tier 1: no ``policy`` key → None (the SandboxLayer stays ⊤ = non-regression)."""
    assert _build_sandbox_config({"backend": "noop"}).policy is None
    assert SandboxConfig().policy is None


def test_sandbox_config_invalid_policy_raises() -> None:
    """Tier 1: an unknown policy key fails fast (operator typo surfaces at load)."""
    with pytest.raises(ValueError, match="sandbox.policy is invalid"):
        SandboxConfig(policy={"bogus_key": 1})


# ── Tier 2: agent policy reaches the sandboxed_exec seam ─────────────────────


class _RecordingExecBackend:
    """Real (non-mock) SandboxBackend Fake — records the policy it was handed."""

    name = "recording-exec"

    def __init__(self) -> None:
        self.policy = None

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None) -> SandboxResult:
        self.policy = policy
        return SandboxResult(returncode=0, stdout=b"ok", stderr=b"")


def _one_phase_skill() -> Skill:
    p = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name="policy_test", entry_phase="draft", phases={"draft": p},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _exec_preprocessor(
    tmp_path: Path, *, agent_policy: dict | None, backend: _RecordingExecBackend,
) -> tuple[PreprocessorExecutor, Phase]:
    events = EventLog()
    phase = Phase(
        name="pp", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["sandboxed_exec"],
        # The op declares network=False / no subprocess / a non-default timeout=10
        # so the recorded policy distinguishes "agent WINS" from "op fields govern".
        preprocessor=[
            RunOpStep(
                type="run_op",
                op=SandboxedExecIROp(
                    kind="sandboxed_exec", argv=["/bin/echo", "x"],
                    network=False, allow_subprocess=False,
                    env_passthrough=["PATH"], timeout_seconds=10,
                ),
                into="data._exec",
            )
        ],
    )
    pe = PreprocessorExecutor(
        skill=_one_phase_skill(),
        workspace=Workspace(events=events, base_dir=tmp_path),
        model="standard", events=events, subscribers=[], resolver=None,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False,
        ),
        sandbox_backend=backend,
        agent_sandbox_policy=agent_policy,
    )
    return pe, phase


def test_agent_policy_wins_at_sandboxed_exec(tmp_path: Path) -> None:
    """Tier 2: the agent policy reaches the sandboxed_exec seam and WINS over the
    op-declared fields (deterministic; the LLM cannot widen it)."""
    backend = _RecordingExecBackend()
    pe, phase = _exec_preprocessor(tmp_path, agent_policy=_AGENT_POLICY, backend=backend)
    asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    # The op declared network=False / timeout=10; the agent policy WINS.
    assert backend.policy is not None
    assert backend.policy.network is True
    assert backend.policy.write_paths == ["/agent"]
    assert backend.policy.timeout_seconds == 600


def test_op_fields_govern_when_agent_absent(tmp_path: Path) -> None:
    """Tier 2: equivalence / non-regression — with NO agent policy, the op's own
    declared fields govern (the SandboxLayer is ⊤) — byte-equivalent to a run that
    declares no sandbox policy. Falsifies an "agent always wins" mis-wire."""
    backend = _RecordingExecBackend()
    pe, phase = _exec_preprocessor(tmp_path, agent_policy=None, backend=backend)
    asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    assert backend.policy is not None
    # The op's non-default timeout=10 proves the OP fields flowed through (not a
    # SandboxPolicy() default), and network stayed the op's False.
    assert backend.policy.timeout_seconds == 10
    assert backend.policy.network is False


# ── Tier 2: #1321 — agent policy reaches the POSTPROCESSOR path ──────────────


class _RecordingPythonRunner:
    """Real (non-mock) PythonRunner Fake — records the forwarded sandbox cap.

    The postprocessor's index write runs as a safe-mode python step. This Fake
    captures the ``sandbox_write_paths`` the parent forwards into the subprocess
    (the value the host-direct SqliteIndexBackend self-gates against), without
    spawning the real subprocess."""

    def __init__(self) -> None:
        self.sandbox_write_paths: Any = "UNSET"

    def run(self, *, sandbox_write_paths=None, **kwargs):
        self.sandbox_write_paths = sandbox_write_paths
        return {}  # validated against the step's permissive output_schema


def _postprocessor_skill(tmp_path: Path) -> Skill:
    p = Phase(
        name="draft", instructions="d",
        input_schema={"type": "object", "properties": {}}, allowed_ops=[],
    )
    return Skill(
        name="post_policy_test", entry_phase="draft", phases={"draft": p},
        graph=SkillGraph(transitions={}, can_finish_phases=["draft"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
        # skill_dir must be non-empty for the python step path; the recording
        # runner never reads the module, so any real dir works.
        skill_dir=str(tmp_path),
        postprocessor=Postprocessor(
            output_schema={"type": "object"},
            steps=[
                PythonStep(
                    type="python", module="./probe.py", function="probe",
                    into="data._probe", output_schema={"type": "object"},
                )
            ],
        ),
    )


def _run_postprocessor(
    tmp_path: Path, *, agent_policy: dict | None, runner: _RecordingPythonRunner,
) -> None:
    events = EventLog()
    post = PostprocessorExecutor(
        skill=_postprocessor_skill(tmp_path),
        workspace=Workspace(events=events, base_dir=tmp_path),
        events=events, model="standard", resolver=None, subscribers=[],
        permission_resolver=None,  # → safe-mode default perm (no decl needed)
        python_runner=runner,
        agent_sandbox_policy=agent_policy,
    )
    asyncio.run(post.run({"type": "result", "data": {}}, output_language=None))


def test_postprocessor_python_step_receives_agent_policy_cap(tmp_path: Path) -> None:
    """Tier 2: #1321 — a postprocessor python step receives the agent policy's
    write_paths cap, even though the _PostprocessorScope is skill-level (carries no
    phase). This is the seam the index write self-gates against — the gap that made
    the write-gate never fire in the postprocessor (#1321)."""
    runner = _RecordingPythonRunner()
    _run_postprocessor(tmp_path, agent_policy=_AGENT_POLICY, runner=runner)
    assert runner.sandbox_write_paths == ["/agent"]


def test_postprocessor_cap_absent_without_agent_policy(tmp_path: Path) -> None:
    """Tier 2: falsification for the dissolve — with NO agent policy, the
    postprocessor python step receives no cap (None). Proves the dissolve test
    above is detecting the agent-policy source, not a constant."""
    runner = _RecordingPythonRunner()
    _run_postprocessor(tmp_path, agent_policy=None, runner=runner)
    assert runner.sandbox_write_paths is None
