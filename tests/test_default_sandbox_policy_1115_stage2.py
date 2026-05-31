"""Tier 2: FP-0008 #1115 Stage 2 (D) — phase default_sandbox_policy mechanism.

A phase may declare a ``default_sandbox_policy`` in its frontmatter. The OS
applies it to every ``sandboxed_exec`` op in that phase, **phase-default WINS**
over the op's own policy fields — so the policy is declared once, deterministic,
and the LLM cannot override it (P8-clean: the skill body never describes the
Control IR policy). This is the generic enabler for migrating skills off the
deprecated ``shell`` op to ``sandboxed_exec`` (#1115 Stage 2, exec-routing A).

Generic (P7): no skill / phase / artifact strings — ANY phase can declare a
default policy. Tests use a real recording SandboxBackend Fake (not a mock) to
capture the policy the handler built.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.sandbox.backend import SandboxResult
from reyn.sandbox.policy import SandboxPolicy
from reyn.schemas.models import SandboxedExecIROp
from reyn.workspace.workspace import Workspace


class _PolicyRecordingBackend:
    """Real SandboxBackend Fake that records the SandboxPolicy it received."""

    name = "policy-recording"

    def __init__(self) -> None:
        self.received: SandboxPolicy | None = None
        self.received_cwd: str | None = None

    def available(self) -> bool:
        return True

    async def run(self, argv, policy, *, stdin=None, cwd=None) -> SandboxResult:
        self.received = policy
        self.received_cwd = cwd
        return SandboxResult(returncode=0, stdout=b"", stderr=b"")


def _executor(tmp_path: Path) -> tuple[ControlIRExecutor, _PolicyRecordingBackend]:
    from reyn.events.events import EventLog

    events = EventLog()
    backend = _PolicyRecordingBackend()
    ex = ControlIRExecutor(
        workspace=Workspace(events=events, base_dir=tmp_path),
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False
        ),
        skill_name="dsp_test",
        sandbox_backend=backend,
    )
    return ex, backend


def _run_exec(ex, *, default_sandbox_policy):
    op = SandboxedExecIROp(
        kind="sandboxed_exec", argv=["/bin/echo", "x"],
        # op-level fields = restrictive defaults (network False, subprocess False)
        env_passthrough=["PATH"], timeout_seconds=10,
    )
    return asyncio.run(
        ex.execute(
            [op], phase="p", decl=PermissionDecl(),
            allowed_ops={"sandboxed_exec"},
            default_sandbox_policy=default_sandbox_policy,
        )
    )


def test_phase_default_policy_wins_over_op_fields(tmp_path: Path) -> None:
    """Tier 2: phase default_sandbox_policy overrides the op's own fields."""
    ex, backend = _executor(tmp_path)
    _run_exec(ex, default_sandbox_policy={
        "network": True,
        "allow_subprocess": True,
        "read_paths": ["/a"],
        "write_paths": ["/b"],
        "env_passthrough": ["PATH", "HOME"],
        "timeout_seconds": 99,
    })
    pol = backend.received
    assert pol is not None
    # The phase default won — NOT the op's restrictive defaults.
    assert pol.network is True
    assert pol.allow_subprocess is True
    assert pol.timeout_seconds == 99
    assert pol.read_paths == ["/a"] and pol.write_paths == ["/b"]


def test_no_phase_default_falls_back_to_op_fields(tmp_path: Path) -> None:
    """Tier 2: with no phase default, the op's own policy fields are used."""
    ex, backend = _executor(tmp_path)
    _run_exec(ex, default_sandbox_policy=None)
    pol = backend.received
    assert pol is not None
    # The op's restrictive defaults (network/subprocess False) — unchanged path.
    assert pol.network is False
    assert pol.allow_subprocess is False
    assert pol.timeout_seconds == 10


# ── Loader wiring: default_sandbox_policy frontmatter → Phase ─────────────────


def _write_phase_md(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "p.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_parser_and_expander_wire_default_sandbox_policy_from_frontmatter(tmp_path: Path) -> None:
    """Tier 2: a default_sandbox_policy frontmatter key reaches Phase.default_sandbox_policy.

    Behavioral pin for the parser→ir→expander wiring. Without it the D mechanism
    is unreachable from a real skill.md (#1115 Stage 2 found this gap). Generic
    (not swe_bench-specific): a synthetic phase .md is parsed + expanded.
    """
    from reyn.compiler.expander import expand_phase
    from reyn.compiler.parser import parse_phase

    md = (
        "---\n"
        "type: phase\n"
        "name: p\n"
        "allowed_ops: [sandboxed_exec]\n"
        "default_sandbox_policy:\n"
        "  network: true\n"
        "  allow_subprocess: true\n"
        "  read_paths: [\"/\"]\n"
        "  timeout_seconds: 42\n"
        "---\n\n"
        "Body.\n"
    )
    phase_def = parse_phase(_write_phase_md(tmp_path, md))
    assert phase_def.default_sandbox_policy == {
        "network": True, "allow_subprocess": True, "read_paths": ["/"], "timeout_seconds": 42,
    }
    phase = expand_phase(phase_def, input_arts=[])
    assert phase.default_sandbox_policy == phase_def.default_sandbox_policy
    # And the dict must be valid SandboxPolicy kwargs.
    SandboxPolicy(**phase.default_sandbox_policy)


def test_parser_omitted_default_sandbox_policy_is_none(tmp_path: Path) -> None:
    """Tier 2: omitting the frontmatter key yields default_sandbox_policy=None (op fields used)."""
    from reyn.compiler.expander import expand_phase
    from reyn.compiler.parser import parse_phase

    md = (
        "---\n"
        "type: phase\n"
        "name: p\n"
        "allowed_ops: [sandboxed_exec]\n"
        "---\n\n"
        "Body.\n"
    )
    phase_def = parse_phase(_write_phase_md(tmp_path, md))
    assert phase_def.default_sandbox_policy is None
    assert expand_phase(phase_def, input_arts=[]).default_sandbox_policy is None


def test_parser_rejects_non_mapping_default_sandbox_policy(tmp_path: Path) -> None:
    """Tier 2: a non-mapping default_sandbox_policy is a load-time error."""
    import pytest

    from reyn.compiler.parser import parse_phase

    md = (
        "---\n"
        "type: phase\n"
        "name: p\n"
        "default_sandbox_policy: [not, a, mapping]\n"
        "---\n\n"
        "Body.\n"
    )
    with pytest.raises(ValueError, match="default_sandbox_policy"):
        parse_phase(_write_phase_md(tmp_path, md))
