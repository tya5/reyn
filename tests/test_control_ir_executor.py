"""Tests for PR37 wave 2C — dispatch_tool wrapping in ControlIRExecutor.

Verifies that each op invocation emits the correct event sequence:
    tool_called → tool_executed → tool_returned   (success path)
    tool_called → tool_failed                      (permission denied)
and that unknown op kinds (not in allowed_ops catalog) produce an
unknown_tool error rather than crashing.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor, _build_phase_tool_catalog
from reyn.schemas.models import FileIROp
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.workspace.workspace import Workspace

# ── helpers ────────────────────────────────────────────────────────────────────


def _make_executor(
    tmp_path: Path,
    *,
    skill_name: str = "test_skill",
    chain_id: str | None = "chain-abc",
    permission_resolver: PermissionResolver | None = None,
) -> tuple[ControlIRExecutor, EventLog]:
    events = EventLog()
    ws = Workspace(events=events)
    resolver = permission_resolver or PermissionResolver(
        config_permissions={},
        project_root=tmp_path,
        interactive=False,
    )
    executor = ControlIRExecutor(
        workspace=ws,
        events=events,
        permission_resolver=resolver,
        skill_name=skill_name,
        chain_id=chain_id,
    )
    return executor, events


def _event_types(events: EventLog) -> list[str]:
    """Extract ordered list of event type strings from an EventLog."""
    return [e.type for e in events.all()]


def _run(coro) -> Any:
    return asyncio.run(coro)


# ── unit tests for _build_phase_tool_catalog ───────────────────────────────────


def test_build_phase_tool_catalog_known_ops():
    """Tier 2: OS invariant — _build_phase_tool_catalog produces entries with function.parameters for known op kinds (read_file, sandboxed_exec)."""
    catalog = _build_phase_tool_catalog({"read_file", "sandboxed_exec"})
    assert "read_file" in catalog
    assert "sandboxed_exec" in catalog
    # Each entry should have a 'function' key with 'parameters'
    assert "parameters" in catalog["read_file"]["function"]
    assert "parameters" in catalog["sandboxed_exec"]["function"]


def test_build_phase_tool_catalog_kind_not_in_required():
    """Tier 2: OS invariant — 'kind' must be removed from required fields in the catalog schema so the LLM is not asked to supply a field the OS already knows."""
    catalog = _build_phase_tool_catalog({"read_file"})
    required = catalog["read_file"]["function"]["parameters"].get("required", [])
    assert "kind" not in required


def test_build_phase_tool_catalog_kind_not_in_properties():
    """Tier 2: OS invariant — 'kind' must be removed from properties in the catalog schema so it does not appear in the LLM-facing tool description."""
    catalog = _build_phase_tool_catalog({"read_file"})
    props = catalog["read_file"]["function"]["parameters"].get("properties", {})
    assert "kind" not in props


def test_build_phase_tool_catalog_unknown_op_kind_gets_schema_less_entry():
    """Tier 2: OS invariant — op kinds with no IROp model get a schema-less catalog entry; unknown kinds are not silently dropped from the catalog."""
    catalog = _build_phase_tool_catalog({"totally_unknown_kind"})
    assert "totally_unknown_kind" in catalog
    assert "parameters" not in catalog["totally_unknown_kind"].get("function", {})


# ── integration tests: event sequence ─────────────────────────────────────────


def test_skill_op_emits_tool_called_and_returned(tmp_path: Path, monkeypatch):
    """Tier 2: P6 invariant — successful file op emits tool_called → tool_executed → tool_returned in order."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hello world")

    executor, events = _make_executor(tmp_path)
    op = FileIROp(kind="file", op="read", path="hello.txt")
    # Default PermissionDecl allows reads under CWD
    decl = PermissionDecl()

    _run(executor.execute([op], phase="read_phase", decl=decl, allowed_ops={"file"}))

    types = _event_types(events)
    # tool_called must come before tool_executed, tool_returned must be last
    assert "tool_called" in types
    assert "tool_executed" in types
    assert "tool_returned" in types
    tc_idx = types.index("tool_called")
    te_idx = types.index("tool_executed")
    tr_idx = types.index("tool_returned")
    assert tc_idx < te_idx < tr_idx, (
        f"Expected tool_called < tool_executed < tool_returned, got indices "
        f"{tc_idx}, {te_idx}, {tr_idx} in {types}"
    )


def test_skill_op_emits_tool_called_event_with_correct_fields(tmp_path: Path, monkeypatch):
    """Tier 2: P6 invariant — tool_called event payload carries caller_kind, caller_id (skill.phase), tool name, and chain_id for audit traceability."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hi")

    executor, events = _make_executor(tmp_path, skill_name="my_skill", chain_id="c99")
    op = FileIROp(kind="file", op="read", path="hello.txt")
    # Default PermissionDecl allows reads under CWD
    decl = PermissionDecl()

    _run(executor.execute([op], phase="p1", decl=decl, allowed_ops={"file"}))

    called_events = [e for e in events.all() if e.type == "tool_called"]
    assert called_events, "Expected at least one tool_called event"
    ev = called_events[0]
    assert ev.data["caller_kind"] == "skill_phase"
    assert ev.data["caller_id"] == "my_skill.p1"
    assert ev.data["tool"] == "file"
    assert ev.data["chain_id"] == "c99"


def test_skill_op_failure_emits_tool_failed(tmp_path: Path, monkeypatch):
    """Tier 2: P6 invariant — permission denied path emits tool_called → tool_failed with no tool_returned; failure is always recorded in the event log.

    Trigger: write op to a path outside the default write zone (.reyn/, reyn/).
    """
    monkeypatch.chdir(tmp_path)

    executor, events = _make_executor(tmp_path)
    # Write to a path outside the default zone (not under .reyn/ or reyn/)
    decl = PermissionDecl()  # no file.write permissions declared
    op = FileIROp(kind="file", op="write", path="output/report.txt", content="data")

    results = _run(
        executor.execute([op], phase="write_phase", decl=decl, allowed_ops={"file"})
    )

    types = _event_types(events)
    assert "tool_called" in types
    assert "tool_failed" in types
    assert "tool_returned" not in types

    # The returned result should be an error shape
    assert results, "Expected at least one result entry"
    assert results[0]["status"] in ("error", "denied")


def test_skill_op_failure_error_kind_is_permission_denied(tmp_path: Path, monkeypatch):
    """Tier 2: P6 invariant — tool_failed event carries error_kind=permission_denied for PermissionError; error classification is stable across refactors."""
    monkeypatch.chdir(tmp_path)

    executor, events = _make_executor(tmp_path)
    # Write outside default zone to trigger PermissionError
    decl = PermissionDecl()
    op = FileIROp(kind="file", op="write", path="output/report.txt", content="data")

    _run(executor.execute([op], phase="write_phase", decl=decl, allowed_ops={"file"}))

    failed_events = [e for e in events.all() if e.type == "tool_failed"]
    assert failed_events, "Expected at least one tool_failed event"
    assert failed_events[0].data["error_kind"] == "permission_denied"


def test_unknown_op_kind_caught_by_dispatch_tool(tmp_path: Path):
    """Tier 2: OS invariant — op kind absent from dispatch catalog yields status=error/kind=unknown_tool; dispatch_tool does not crash on unrecognised op names."""
    executor, events = _make_executor(tmp_path)

    # Synthesize an op with a kind that IS in allowed_ops but NOT in the registry.
    # We abuse FileIROp as a carrier — the kind field is overridden post-construction
    # via a real minimal stub class (no MagicMock per policy).
    class _FakeOp:
        kind = "nonexistent_op"
        def model_dump(self) -> dict:
            return {"path": "x"}

    fake_op = _FakeOp()

    # Build catalog that DOES NOT include "nonexistent_op"
    allowed = {"read_file"}  # fake_op kind won't pass name validation
    decl = PermissionDecl()

    results = _run(
        executor.execute([fake_op], phase="p1", decl=decl, allowed_ops=allowed)
    )

    # The op kind is not in allowed_ops → skipped at frontend level, not reaching dispatch_tool
    # Let's instead test: op kind in allowed_ops but absent from catalog (via direct catalog call)
    # We test _build_phase_tool_catalog + dispatch_tool directly here.
    from reyn.dispatch import DispatchContext, dispatch_tool

    class FakeEvents:
        def __init__(self):
            self.events: list[tuple] = []
        def emit(self, t, **kw):
            self.events.append((t, kw))

    async def run_unknown():
        ev = FakeEvents()
        catalog = _build_phase_tool_catalog({"read_file"})  # no "mystery_op"
        dctx = DispatchContext(
            caller_kind="skill_phase",
            caller_id="sk.ph",
            chain_id=None,
            tool_catalog=catalog,
            events=ev,
        )
        async def _stub_invoker(args: dict) -> dict:
            return {}

        result = await dispatch_tool(
            name="mystery_op",
            args={},
            ctx=dctx,
            invoker=_stub_invoker,
        )
        return result, ev.events

    result, emitted = asyncio.run(run_unknown())
    assert result["status"] == "error"
    assert result["error"]["kind"] == "unknown_tool"
    failed_types = [e[0] for e in emitted]
    assert "tool_failed" in failed_types
    assert "tool_called" not in failed_types


def test_not_allowed_in_phase_skip_no_dispatch_events(tmp_path: Path):
    """Tier 2: OS invariant — ops filtered by allowed_ops before dispatch emit control_ir_skipped but not tool_called/tool_returned; pre-dispatch filtering is silent to the event audit trail."""
    executor, events = _make_executor(tmp_path)
    op = FileIROp(kind="file", op="read", path="anything.txt")
    decl = PermissionDecl()

    # allowed_ops excludes "file" → skipped at frontend before dispatch_tool
    _run(executor.execute([op], phase="p", decl=decl, allowed_ops={"sandboxed_exec"}))

    types = _event_types(events)
    assert "control_ir_skipped" in types
    assert "tool_called" not in types
    assert "tool_returned" not in types
    assert "tool_failed" not in types


def test_multiple_ops_each_get_own_tool_called_returned(tmp_path: Path, monkeypatch):
    """Tier 2: P6 invariant — each op in a batch execute() call gets its own tool_called/tool_returned bracket; events are not collapsed or shared across ops."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    executor, events = _make_executor(tmp_path)
    ops = [
        FileIROp(kind="file", op="read", path="a.txt"),
        FileIROp(kind="file", op="read", path="b.txt"),
    ]
    # Default PermissionDecl allows reads under CWD
    decl = PermissionDecl()

    _run(executor.execute(ops, phase="multi", decl=decl, allowed_ops={"file"}))

    types = _event_types(events)
    assert types.count("tool_called") == 2
    assert types.count("tool_returned") == 2
