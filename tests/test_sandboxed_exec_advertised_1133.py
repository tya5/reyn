"""Tier 2: FP-0008 #1133 — sandboxed_exec is advertised in available_ops().

Root cause of the C7 verify-fail (live flask-5014): `available_ops()` never
emitted a `sandboxed_exec` ControlIROpSpec, so a phase that declared
`allowed_ops: [sandboxed_exec]` (after the #1126/#1127 migration) had the op
filtered to nothing — the LLM never saw `argv`, guessed a shell `command`
string, and failed ActOutput validation. Same class as
test ... wire-full-frontmatter-to-runtime path: handler/runtime wired, the
advertisement path was not.

These pin: the spec exists, its description makes `argv` a required LIST (not a
shell string / not a `command` field), its example is a valid op, and a phase
that allows sandboxed_exec actually surfaces it through the allowed_ops filter.

No mocks; real ControlIRExecutor + real SandboxedExecIROp. Docstrings open "Tier 2:".
"""
from __future__ import annotations

from pathlib import Path

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import SandboxedExecIROp
from reyn.workspace.workspace import Workspace


def _executor(tmp_path: Path) -> ControlIRExecutor:
    events = EventLog()
    ws = Workspace(events=events)
    return ControlIRExecutor(
        workspace=ws,
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False
        ),
        skill_name="t",
    )


def _spec(executor, kind):
    return next((s for s in executor.available_ops() if s.kind == kind), None)


def test_sandboxed_exec_is_advertised(tmp_path: Path) -> None:
    """Tier 2: available_ops() includes a sandboxed_exec spec (the missing advertisement)."""
    spec = _spec(_executor(tmp_path), "sandboxed_exec")
    assert spec is not None, (
        "available_ops() must advertise sandboxed_exec — without it a phase that "
        "lists it in allowed_ops gets the op filtered to nothing."
    )


def test_sandboxed_exec_description_requires_argv_list_not_command_string(tmp_path: Path) -> None:
    """Tier 2: the description steers the LLM to argv-as-list, not a shell `command` string.

    This directly counters the observed failure (model emitted `command: "git …"`).
    """
    desc = _spec(_executor(tmp_path), "sandboxed_exec").description.lower()
    assert "argv" in desc, "description must name the argv field"
    assert "list" in desc, "description must say argv is a list"
    # Must warn it is not a single shell string / not a command field.
    assert "command" in desc or "cmd" in desc, (
        "description should explicitly contrast with a shell command/cmd field"
    )
    assert "shell" in desc, "description should state there is no shell interpretation"


def test_sandboxed_exec_example_is_a_valid_op(tmp_path: Path) -> None:
    """Tier 2: the advertised example validates as a real SandboxedExecIROp with a list argv."""
    spec = _spec(_executor(tmp_path), "sandboxed_exec")
    assert isinstance(spec.example.get("argv"), list), "example argv must be a list"
    op = SandboxedExecIROp(**spec.example)  # raises if the advertised example is invalid
    assert op.kind == "sandboxed_exec"
    assert op.argv == spec.example["argv"]


def test_phase_allowing_sandboxed_exec_surfaces_it_through_filter(tmp_path: Path) -> None:
    """Tier 2: with allowed_ops={sandboxed_exec}, the op survives the available-ops filter.

    Mirrors the runtime build_frame filter (runtime.py: op.kind in allowed). This is
    the exact regression: pre-#1133, sandboxed_exec ∈ allowed_ops but ∉ available_ops()
    → filtered to nothing.
    """
    executor = _executor(tmp_path)
    allowed = {"file", "sandboxed_exec"}
    filtered = [op for op in executor.available_ops() if op.kind in allowed]
    kinds = {op.kind for op in filtered}
    assert "sandboxed_exec" in kinds, (
        f"a phase allowing sandboxed_exec must surface it; filtered kinds={kinds}"
    )
