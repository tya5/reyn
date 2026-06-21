"""Tier 2: OS invariant — op advertisement completeness (#1993).

Reframe (verified via is_op_allowed: all OP_KIND_MODEL_MAP kinds are phase-
permitted): available_ops() advertised only ~13 kinds, so the rest (task ops,
compact, recall, mcp_install, mcp_drop_server, index/judge/skill_resolve) were
phase-permitted + dispatchable but UNSCHEMATIZED — a phase declaring one received
no schema and a phase_op_catalog_gap. #1993 widens available_ops() to EVERY map
kind (completeness-by-construction) and canonicalizes op_catalog (the meta-skill
reference) so the names it hands skill_builder/improver/importer pass the DSL
linter in generated allowed_ops.

Real ControlIRExecutor + real OSRuntime.build_frame; no mocks. The gap-warning is
KEPT, narrowed (D-C) — guards a config-gated-OFF op (mcp with no server) +
stale/bogus names; the regression-guard below pins that it still fires.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.kernel.control_ir_executor import ControlIRExecutor
from reyn.core.kernel.runtime import OSRuntime
from reyn.core.op_runtime.registry import _PHASE_TOOL_NAME_ALIAS
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import ALL_OP_KINDS, Phase, Skill, SkillGraph
from reyn.security.permissions.permissions import PermissionResolver

_SERVERS = {"servers": {"gh": {"type": "http", "url": "http://x"}}}


def _executor(tmp_path: Path, *, mcp_servers: dict | None = None) -> ControlIRExecutor:
    events = EventLog()
    return ControlIRExecutor(
        workspace=Workspace(events=events),
        events=events,
        permission_resolver=PermissionResolver(
            config_permissions={}, project_root=tmp_path, interactive=False
        ),
        skill_name="t",
        mcp_servers=mcp_servers,
    )


def _resolved_kinds(specs) -> set[str]:
    return {_PHASE_TOOL_NAME_ALIAS.get(s.kind, s.kind) for s in specs}


def _skill(allowed_ops: list[str]) -> Skill:
    phase = Phase(
        name="act", instructions="do work",
        input_schema={"type": "object", "properties": {}}, allowed_ops=allowed_ops,
    )
    return Skill(
        name="adv_test", entry_phase="act", phases={"act": phase},
        graph=SkillGraph(transitions={}, can_finish_phases=["act"]),
        final_output_schema={"type": "object", "properties": {}}, final_output_name="result",
    )


def _runtime(skill: Skill, tmp_path: Path) -> OSRuntime:
    return OSRuntime(skill, model="stub/model", run_id="r", workspace_base_dir=tmp_path)


def _frame(rt: OSRuntime):
    return rt.build_frame("act", {"type": "input", "data": {}}, [], "en")


def _gaps(rt: OSRuntime) -> list:
    return [e for e in rt.events.all() if e.type == "phase_op_catalog_gap"]


# ── completeness of the catalog ──────────────────────────────────────────────


def test_available_ops_complete_with_servers(tmp_path: Path) -> None:
    """Tier 2: with a server configured, available_ops() advertises EVERY op kind
    in OP_KIND_MODEL_MAP (alias-resolved) — completeness-by-construction."""
    kinds = _resolved_kinds(_executor(tmp_path, mcp_servers=_SERVERS).available_ops())
    assert set(ALL_OP_KINDS) - kinds == set(), f"unadvertised map kinds: {set(ALL_OP_KINDS) - kinds}"


def test_available_ops_complete_except_gated_mcp_without_servers(tmp_path: Path) -> None:
    """Tier 2: with NO server, only the config-gated mcp call op is absent — every
    other map kind is advertised (the gate is the sole exception)."""
    kinds = _resolved_kinds(_executor(tmp_path).available_ops())
    assert set(ALL_OP_KINDS) - kinds == {"mcp"}, f"unexpected gaps: {set(ALL_OP_KINDS) - kinds}"


# ── reachability (the headline gate, RED on current main) ────────────────────


def test_declared_task_op_is_advertised_no_gap(tmp_path: Path) -> None:
    """Tier 2: a phase declaring a previously-unadvertised op (task.create) now
    receives its schema in available_control_ops and emits NO wiring-gap.

    RED on current main: task.create is filtered to nothing (not advertised) and a
    phase_op_catalog_gap fires. GREEN after the widen."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    rt = _runtime(_skill(["task.create"]), tmp_path)
    frame = _frame(rt)
    advertised = {s.kind for s in frame.available_control_ops}
    assert "task.create" in advertised, f"task.create not advertised: {sorted(advertised)}"
    assert not _gaps(rt), f"unexpected gap for an advertised op: {[g.data for g in _gaps(rt)]}"


def test_previously_omitted_registry_ops_advertised(tmp_path: Path) -> None:
    """Tier 2: the four ops that were registry phase==allow but absent from the
    hand-list (compact / recall / mcp_install / mcp_drop_server) are now
    advertised when declared, with no gap."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    ops = ["compact", "recall", "mcp_install", "mcp_drop_server"]
    rt = _runtime(_skill(ops), tmp_path)
    advertised = {s.kind for s in _frame(rt).available_control_ops}
    assert set(ops) <= advertised, f"still unadvertised: {set(ops) - advertised}"
    assert not _gaps(rt)


# ── op_catalog canonical + linter-valid ──────────────────────────────────────


def test_op_catalog_is_canonical_and_linter_valid(tmp_path: Path) -> None:
    """Tier 2: op_catalog (the meta-skill reference) lists CANONICAL kinds — no
    chat aliases — and every kind is a name the DSL linter accepts in allowed_ops
    (ALL_TOOL_NAMES). A meta-skill copying a kind into a generated phase's
    allowed_ops therefore produces a linter-valid frontmatter."""
    pytest.importorskip("litellm")
    import os

    from reyn.core.op_runtime.registry import ALL_TOOL_NAMES

    os.chdir(tmp_path)
    catalog_kinds = {s.kind for s in _frame(_runtime(_skill(["read_file"]), tmp_path)).op_catalog}
    assert "invoke_skill" not in catalog_kinds and "call_mcp_tool" not in catalog_kinds, (
        "op_catalog must use canonical kinds (run_skill/mcp), not chat aliases"
    )
    assert "run_skill" in catalog_kinds  # the canonicalized form is present
    unknown = catalog_kinds - set(ALL_TOOL_NAMES)
    assert not unknown, f"op_catalog kinds not linter-valid (not in ALL_TOOL_NAMES): {sorted(unknown)}"


# ── additive-safety + the kept (narrowed) gap-warning ────────────────────────


def test_phase_offers_only_its_declared_ops_no_bloat(tmp_path: Path) -> None:
    """Tier 2: additive-safety — widening the catalog does NOT inject all kinds
    into a phase. available_control_ops is still the per-phase allowed_ops set,
    so a phase declaring only read_file is offered only read_file."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    advertised = {s.kind for s in _frame(_runtime(_skill(["read_file"]), tmp_path)).available_control_ops}
    assert advertised == {"read_file"}, f"per-phase filter leaked extra ops: {sorted(advertised)}"


def test_mcp_with_no_servers_still_warns(tmp_path: Path) -> None:
    """Tier 2: regression-guard for the kept/narrowed warning (D-C) — a phase
    declaring mcp with NO server configured still emits phase_op_catalog_gap, the
    #997/FP-0008 guard the widen did NOT make inert."""
    pytest.importorskip("litellm")
    import os

    os.chdir(tmp_path)
    rt = _runtime(_skill(["read_file", "mcp"]), tmp_path)  # no mcp servers
    _frame(rt)
    gaps = _gaps(rt)
    assert gaps, "expected a gap for mcp declared with no server configured"
    assert "mcp" in gaps[-1].data["missing_ops"], f"mcp should be the gap: {gaps[-1].data['missing_ops']}"
