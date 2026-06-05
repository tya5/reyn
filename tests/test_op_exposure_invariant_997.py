"""Tier 2: OS invariant — Tier-3 op exposure + wiring-gap event (#997).

Generalises the #1133 sandboxed_exec fix into the structural invariant that
prevents the whole #1133 / FP-0008 class:

> For every Tier-3 op X, if the runtime config enables X, the LLM's op catalog
> MUST advertise X's schema; and if a phase declares X in allowed_ops while the
> runtime did NOT enable it (so X is filtered to nothing), the OS must surface
> the wiring gap as an event before the LLM hallucinates a fake schema.

Direction 1 (op-exposure invariant): ``available_ops()`` advertises shell iff
``shell_allowed``, call_mcp_tool iff mcp servers are configured, and the
unconditional set (sandboxed_exec / web_fetch / web_search / fine file ops /
invoke_skill / lint / ask_user) regardless of flags — the last pinning #1133
(sandboxed_exec was the op that went missing).

#1240 Wave 2b: available_ops() now advertises the chat names "invoke_skill" and
"call_mcp_tool" (instead of "run_skill" / "mcp") as ControlIROpSpec.kind values.
The underlying execution op kinds and allowed_ops frontmatter are unchanged;
_PHASE_TOOL_NAME_ALIAS + build_frame filter bridge the gap.

Direction 3 (wiring-gap event): ``build_frame`` emits a ``phase_op_catalog_gap``
event (once per phase per run) when a phase's ``allowed_ops`` references an op
the executor does not advertise — the exact pre-#1133 failure shape (phase says
"use shell", shell gated off, op filtered to nothing, LLM hallucinates).

No mocks — real ControlIRExecutor + real OSRuntime.build_frame.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor
from reyn.kernel.runtime import OSRuntime
from reyn.permissions.permissions import PermissionResolver
from reyn.schemas.models import Phase, Skill, SkillGraph
from reyn.workspace.workspace import Workspace

# Ops the catalog advertises regardless of runtime flags. sandbox_exec is the
# #1133 regression anchor (it was the op that went missing). The list is derived
# from the invariant intent, not the implementation — a future change that gates
# one of these behind a flag must update this guard deliberately.
# #1240 Wave 2b: coarse "file" replaced by fine file kinds (read_file/write_file/
# edit_file/delete_file/glob_files/grep_files) — these are now the unconditionally-
# advertised file ops via available_ops() → _fine_file_op_specs().
# #1240 Wave 2b: "run_skill" → "invoke_skill" (chat name alias; available_ops()
# now advertises the invoke_skill spec). The execution op kind "run_skill" and
# allowed_ops frontmatter are UNCHANGED.
_UNCONDITIONAL_OPS = {
    "read_file",
    "write_file",
    "edit_file",
    "delete_file",
    "glob_files",
    "grep_files",
    "ask_user",
    "sandboxed_exec",
    "lint",
    "invoke_skill",
    "web_fetch",
    "web_search",
}


def _executor(
    tmp_path: Path, *, shell_allowed: bool = False, mcp_servers: dict | None = None
) -> ControlIRExecutor:
    events = EventLog()
    ws = Workspace(events=events)
    return ControlIRExecutor(
        workspace=ws,
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False
        ),
        skill_name="t",
        shell_allowed=shell_allowed,
        mcp_servers=mcp_servers,
    )


def _kinds(executor: ControlIRExecutor) -> set[str]:
    return {spec.kind for spec in executor.available_ops()}


# ── Direction 1: op-exposure invariant ──────────────────────────────────────


@pytest.mark.parametrize(
    "mcp_servers, expect",
    [
        (None, False),
        ({"servers": {}}, False),
        ({"servers": {"gh": {"type": "http", "url": "http://x"}}}, True),
    ],
)
def test_mcp_advertised_iff_servers_configured(
    tmp_path: Path, mcp_servers: dict | None, expect: bool
) -> None:
    """Tier 2: call_mcp_tool is in the op catalog exactly when mcp servers are configured.

    #1240 Wave 2b: available_ops() advertises "call_mcp_tool" (chat name) instead
    of "mcp" (op kind).  The execution backend and allowed_ops frontmatter are
    unchanged; _PHASE_TOOL_NAME_ALIAS bridges the gap at the parse boundary.
    """
    kinds = _kinds(_executor(tmp_path, mcp_servers=mcp_servers))
    assert ("call_mcp_tool" in kinds) is expect, (
        f"call_mcp_tool advertised={('call_mcp_tool' in kinds)} but servers configured={expect}"
    )


@pytest.mark.parametrize(
    "shell_allowed, mcp_servers",
    [
        (False, None),
        (True, None),
        (False, {"servers": {"gh": {"type": "http", "url": "http://x"}}}),
        (True, {"servers": {"gh": {"type": "http", "url": "http://x"}}}),
    ],
)
def test_unconditional_ops_always_advertised(
    tmp_path: Path, shell_allowed: bool, mcp_servers: dict | None
) -> None:
    """Tier 2: the flag-independent ops are always advertised (incl. sandboxed_exec = #1133).

    This is the generalised #1133 regression lock: no matter the shell/mcp flags,
    the unconditional Tier-3 + core ops must stay in the catalog. The original bug
    was sandboxed_exec silently absent from the spec list.
    """
    kinds = _kinds(_executor(tmp_path, shell_allowed=shell_allowed, mcp_servers=mcp_servers))
    missing = _UNCONDITIONAL_OPS - kinds
    assert not missing, (
        f"unconditional ops missing from op catalog: {sorted(missing)} "
        f"(shell_allowed={shell_allowed}, mcp={bool(mcp_servers)})"
    )


# ── Direction 3: phase_op_catalog_gap wiring-gap event ───────────────────────


def _skill_with_allowed_ops(allowed_ops: list[str]) -> Skill:
    phase = Phase(
        name="act",
        instructions="do work",
        input_schema={"type": "object", "properties": {}},
        allowed_ops=allowed_ops,
    )
    return Skill(
        name="gap_test_skill",
        entry_phase="act",
        phases={"act": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["act"]),
        final_output_schema={"type": "object", "properties": {}},
        final_output_name="result",
    )


def _gap_events(events: EventLog) -> list:
    return [e for e in events.all() if e.type == "phase_op_catalog_gap"]


def test_phase_op_catalog_gap_emitted_when_declared_op_not_advertised(tmp_path: Path) -> None:
    """Tier 2: a phase declaring shell while shell is gated off emits phase_op_catalog_gap.

    The pre-#1133 failure shape: allowed_ops references an op the executor does
    not advertise (shell, shell_allowed=False) → op filtered to nothing. The
    event surfaces the caller-side wiring gap proactively.
    """
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    rt = OSRuntime(
        _skill_with_allowed_ops(["read_file", "shell"]),  # shell declared, shell_allowed defaults False
        model="stub/model",
        run_id="gap_test",
        workspace_base_dir=tmp_path,
    )
    rt.build_frame("act", {"type": "input", "data": {}}, [], "en")

    gaps = _gap_events(rt.events)
    assert gaps, "expected a phase_op_catalog_gap event for the gated-off shell op"
    d = gaps[-1].data
    assert d["phase"] == "act"
    assert "shell" in d["missing_ops"], f"missing_ops should name shell: {d['missing_ops']}"
    assert "read_file" not in d["missing_ops"], "read_file is advertised — must not be flagged as a gap"


def test_no_gap_event_when_all_declared_ops_advertised(tmp_path: Path) -> None:
    """Tier 2: a phase declaring only advertised ops emits no wiring-gap event."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    rt = OSRuntime(
        _skill_with_allowed_ops(["read_file", "sandboxed_exec"]),  # both unconditional
        model="stub/model",
        run_id="no_gap_test",
        workspace_base_dir=tmp_path,
    )
    rt.build_frame("act", {"type": "input", "data": {}}, [], "en")
    assert not _gap_events(rt.events), (
        "no gap event expected when every declared op is advertised"
    )


def test_phase_op_catalog_gap_emitted_once_per_phase(tmp_path: Path) -> None:
    """Tier 2: the gap event is deduped — emitted once per phase per run, not per turn."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    rt = OSRuntime(
        _skill_with_allowed_ops(["read_file", "shell"]),
        model="stub/model",
        run_id="dedup_test",
        workspace_base_dir=tmp_path,
    )
    rt.build_frame("act", {"type": "input", "data": {}}, [], "en")
    rt.build_frame("act", {"type": "input", "data": {}}, [], "en")
    rt.build_frame("act", {"type": "input", "data": {}}, [], "en")
    assert len(_gap_events(rt.events)) == 1, (
        "the static config gap should warn once per phase per run, not every build_frame"
    )
