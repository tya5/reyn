"""Tier 2: skill_resolve op invariants (R-PURE-MODE Wave 5a).

Tests the op handler through the public execute_op path so handler
registration and the ControlIROp discriminated union are both exercised.

No mocks — uses real EventLog, Workspace, OpContext, and the actual
stdlib filesystem (read-only path existence checks only).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.op_runtime import execute_op
from reyn.op_runtime.context import OpContext
from reyn.op_runtime.registry import OP_KIND_MODEL_MAP, OP_PURITY, OpPurity
from reyn.schemas.models import SkillResolveIROp
from reyn.security.permissions.permissions import PermissionDecl

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path: Path) -> OpContext:
    events = EventLog()
    ws = Workspace(events=events)
    return OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )


def _make_op(name: str) -> SkillResolveIROp:
    return SkillResolveIROp(kind="skill_resolve", name=name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_resolve_resolves_stdlib_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: resolving a known stdlib skill returns resolved=True and source="stdlib"."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = _make_op("eval")  # "eval" is always present in stdlib

    result = await execute_op(op, ctx, caller="preprocessor")

    assert result.get("status") != "error", result
    assert result["resolved"] is True
    assert result["source"] == "stdlib"
    assert result["name"] == "eval"
    assert result["skill_md_path"] is not None
    assert Path(result["skill_md_path"]).exists()
    assert result["skill_dir"] is not None
    assert Path(result["skill_dir"]).is_dir()


@pytest.mark.asyncio
async def test_skill_resolve_unknown_returns_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: resolving a nonexistent skill name returns resolved=False with null fields, no exception."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = _make_op("__nonexistent_skill_xyzzy_12345__")

    result = await execute_op(op, ctx, caller="preprocessor")

    # Must not raise or return status=error — unresolved is a valid state
    assert result.get("status") != "error", result
    assert result["resolved"] is False
    assert result["skill_md_path"] is None
    assert result["source"] is None
    assert result["skill_dir"] is None
    assert result["name"] == "__nonexistent_skill_xyzzy_12345__"


@pytest.mark.asyncio
async def test_skill_resolve_emits_completion_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: skill_resolve_completed event is emitted after every call (P6)."""
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )

    op = _make_op("eval")
    await execute_op(op, ctx, caller="preprocessor")

    event_types = [e.type for e in events.all()]
    assert "skill_resolve_completed" in event_types


@pytest.mark.asyncio
async def test_skill_resolve_emits_completion_event_on_unresolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: skill_resolve_completed is also emitted when the skill is not found (P6 always-emit contract)."""
    monkeypatch.chdir(tmp_path)
    events = EventLog()
    ws = Workspace(events=events)
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
    )

    op = _make_op("__does_not_exist__")
    await execute_op(op, ctx, caller="preprocessor")

    event_types = [e.type for e in events.all()]
    assert "skill_resolve_completed" in event_types


def test_skill_resolve_op_purity_is_world() -> None:
    """Tier 2: OP_PURITY["skill_resolve"] is OpPurity.world (read-only fs metadata)."""
    assert "skill_resolve" in OP_PURITY
    assert OP_PURITY["skill_resolve"] == OpPurity.world


def test_skill_resolve_in_model_map() -> None:
    """Tier 2: OP_KIND_MODEL_MAP["skill_resolve"] maps to SkillResolveIROp."""
    assert "skill_resolve" in OP_KIND_MODEL_MAP
    assert OP_KIND_MODEL_MAP["skill_resolve"] is SkillResolveIROp


@pytest.mark.asyncio
async def test_skill_resolve_can_be_dispatched_via_execute_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: full round-trip through execute_op returns the expected dict shape."""
    monkeypatch.chdir(tmp_path)
    ctx = _make_ctx(tmp_path)
    op = _make_op("skill_improver")  # known stdlib skill

    result = await execute_op(op, ctx, caller="preprocessor")

    # Check all expected keys are present
    assert set(result.keys()) >= {"name", "resolved", "skill_md_path", "source", "skill_dir"}
    # Check types are correct
    assert isinstance(result["name"], str)
    assert isinstance(result["resolved"], bool)
    if result["resolved"]:
        assert isinstance(result["skill_md_path"], str)
        assert isinstance(result["source"], str)
        assert isinstance(result["skill_dir"], str)


@pytest.mark.asyncio
async def test_skill_resolve_local_skill_takes_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier 2: a skill in reyn/local shadows the stdlib skill of the same name."""
    monkeypatch.chdir(tmp_path)

    # Create a local skill directory that shadows stdlib "eval"
    local_skill_dir = tmp_path / "reyn" / "local" / "eval"
    local_skill_dir.mkdir(parents=True)
    (local_skill_dir / "skill.md").write_text("# local eval override\n")

    ctx = _make_ctx(tmp_path)
    op = _make_op("eval")

    result = await execute_op(op, ctx, caller="preprocessor")

    assert result["resolved"] is True
    assert result["source"] == "local"
    assert "reyn/local/eval" in result["skill_dir"]
