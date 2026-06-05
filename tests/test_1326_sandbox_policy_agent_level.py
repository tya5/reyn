"""Tier 2: #1326 — sandbox-policy re-leveled from phase-scope to agent-level.

The phase-scoped ``default_sandbox_policy`` (an FP-0017 remnant, declared only by
swe_bench phases whose policy is byte-identical across all 5 phases) is being
retired in favour of an agent-level (operator) policy carried by
``reyn.yaml sandbox.policy``. This PR lands the non-breaking *mechanism*:

  - ``SandboxConfig.policy`` (optional nested mapping; absent → None → the
    SandboxLayer stays ⊤, so any run that declares no policy is unchanged).
  - ``resolve_sandbox_policy_source(agent, phase)``: the agent-level policy WINS
    (deterministic, the LLM/phase cannot widen it); falls back to the migrating
    phase policy while the swe_bench eval lane has not yet moved its policy to
    agent/run config; the retire follow-up deletes that fallback.
  - The policy is threaded OSRuntime → Phase / Orchestrator / pre- + post-
    processor executors, so it reaches the postprocessor path — whose
    ``_PostprocessorScope`` carries no phase policy — which is what makes the
    index write-gate fire there (dissolves #1321).

Equivalence: when no operator policy is set (= every current run), the resolver
returns the phase policy → byte-equivalent to the pre-#1326 behavior.

No mocks: a real recording SandboxBackend Fake (mirrors test_op_sandboxed_exec /
test_backend_injection_threading_1115_stage2) + a real recording PythonRunner
Fake capture the resolved policy at the seam.
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
from reyn.op_runtime.context import resolve_sandbox_policy_source
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

# A broad operator policy mirroring swe_bench's (the live migration target).
_AGENT_POLICY = {
    "network": True,
    "read_paths": ["/agent"],
    "write_paths": ["/agent"],
    "allow_subprocess": True,
    "env_passthrough": ["PATH"],
    "timeout_seconds": 600,
}
_PHASE_POLICY = {
    "network": False,
    "read_paths": ["/phase"],
    "write_paths": ["/phase"],
    "allow_subprocess": False,
    "env_passthrough": ["HOME"],
    "timeout_seconds": 120,
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


# ── Tier 2: resolve_sandbox_policy_source invariant ──────────────────────────


def test_resolve_agent_policy_wins() -> None:
    """Tier 2: the agent-level (operator) policy WINS over the phase policy."""
    assert resolve_sandbox_policy_source(_AGENT_POLICY, _PHASE_POLICY) == _AGENT_POLICY


def test_resolve_falls_back_to_phase_during_migration() -> None:
    """Tier 2: agent absent → phase policy governs (the migration fallback)."""
    assert resolve_sandbox_policy_source(None, _PHASE_POLICY) == _PHASE_POLICY


def test_resolve_absent_both_is_none() -> None:
    """Tier 2: neither set → None → SandboxLayer ⊤ + op-level fields govern."""
    assert resolve_sandbox_policy_source(None, None) is None


# ── Tier 2: agent policy reaches the sandboxed_exec seam + WINS ──────────────


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
    tmp_path: Path, *, agent_policy: dict | None, phase_policy: dict | None,
    backend: _RecordingExecBackend,
) -> tuple[PreprocessorExecutor, Phase]:
    events = EventLog()
    phase = Phase(
        name="pp", instructions="d",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=["sandboxed_exec"],
        # op declares network=False / no subprocess: distinct from the agent
        # policy so "WINS" is observable on the recorded policy.
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
        default_sandbox_policy=phase_policy,
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


def test_agent_policy_wins_at_sandboxed_exec_no_phase_policy(tmp_path: Path) -> None:
    """Tier 2: the agent policy reaches the sandboxed_exec seam and WINS over the
    op-declared fields, with NO phase policy present (the steady-state shape)."""
    backend = _RecordingExecBackend()
    pe, phase = _exec_preprocessor(
        tmp_path, agent_policy=_AGENT_POLICY, phase_policy=None, backend=backend,
    )
    asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    # The op declared network=False; the agent policy (network=True) WINS.
    assert backend.policy is not None
    assert backend.policy.network is True
    assert backend.policy.write_paths == ["/agent"]


def test_phase_policy_governs_when_agent_absent_equivalence(tmp_path: Path) -> None:
    """Tier 2: equivalence — agent policy absent → the phase policy still governs
    (byte-equivalent to pre-#1326), NOT the op fields. Falsifies an "agent always
    wins" mis-wire."""
    backend = _RecordingExecBackend()
    pe, phase = _exec_preprocessor(
        tmp_path, agent_policy=None, phase_policy=_PHASE_POLICY, backend=backend,
    )
    asyncio.run(pe.run(phase, {"type": "x", "data": {}}, None))
    assert backend.policy is not None
    assert backend.policy.read_paths == ["/phase"]
    assert backend.policy.network is False


# ── Tier 2: #1321 dissolve — agent policy reaches the POSTPROCESSOR path ──────


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
    """Tier 2: #1321 dissolve — a postprocessor python step receives the agent
    policy's write_paths cap, even though the _PostprocessorScope carries no phase
    policy. This is the seam the index write self-gates against — the gap that
    made the write-gate never fire in the postprocessor (#1321)."""
    runner = _RecordingPythonRunner()
    _run_postprocessor(tmp_path, agent_policy=_AGENT_POLICY, runner=runner)
    assert runner.sandbox_write_paths == ["/agent"]


def test_postprocessor_cap_absent_without_agent_policy(tmp_path: Path) -> None:
    """Tier 2: falsification for the dissolve — with NO agent policy, the
    postprocessor python step receives no cap (None) — the pre-#1326 #1321 gap
    state. Proves the dissolve test above is detecting the agent-policy source,
    not a constant."""
    runner = _RecordingPythonRunner()
    _run_postprocessor(tmp_path, agent_policy=None, runner=runner)
    assert runner.sandbox_write_paths is None
