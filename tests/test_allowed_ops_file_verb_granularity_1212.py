"""Tier 2: #1212 PR4 (D7) — op-native file verb granularity in allowed_ops.

`file` is the only op kind with a genuine tool-verb axis (FileIROp.op), so PR4
gives `allowed_ops` verb granularity for it alone: `file__read` allows read but
NOT delete (the P4-precision win), while a coarse `file` entry keeps allowing
every verb (behavior-preserving for existing frontmatter). Every other op kind
is single-verb (tool-name == kind, unchanged). The chat-router taxonomy is NOT
adopted (decision A — phase-op surface stays separate from the chat router).

Real `ControlIRExecutor` + real `PermissionResolver` + real `Workspace` for the
end-to-end gating proof (the allowed_ops frontend filter precedes dispatch, so a
real resolver — not `permission_resolver=None` auto-permit — keeps the test
honest). Registry / catalog / conversion / linter units use real objects too.
No mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from reyn.compiler.linter import _lint_allowed_ops
from reyn.events.events import EventLog
from reyn.kernel.control_ir_executor import ControlIRExecutor, _build_phase_tool_catalog
from reyn.kernel.op_loop import tool_call_to_control_ir_op
from reyn.op_runtime.registry import (
    FILE_VERB_TOOL_NAMES,
    is_op_instance_allowed,
    op_tool_name,
    split_tool_name,
)
from reyn.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.schemas.models import FileIROp, WebFetchIROp
from reyn.workspace.workspace import Workspace


def _make_executor(tmp_path: Path) -> tuple[ControlIRExecutor, EventLog]:
    events = EventLog()
    ws = Workspace(events=events)
    resolver = PermissionResolver(
        config_permissions={}, project_root=tmp_path, interactive=False,
    )
    executor = ControlIRExecutor(
        workspace=ws, events=events, permission_resolver=resolver,
        skill_name="pr4_test", chain_id="chain-pr4",
    )
    return executor, events


def _run(coro) -> Any:
    return asyncio.run(coro)


def _skipped_kinds(events: EventLog) -> list[str]:
    return [
        e.data.get("kind")
        for e in events.all()
        if e.type == "control_ir_skipped"
        and e.data.get("reason") == "not_allowed_in_phase"
    ]


# ── Registry unit ───────────────────────────────────────────────────────────


def test_file_verb_tool_names_derived_from_model() -> None:
    """Tier 2: the file verb tool-names are derived from FileIROp.op (no drift)."""
    assert "file__read" in FILE_VERB_TOOL_NAMES
    assert "file__delete" in FILE_VERB_TOOL_NAMES
    # round-trip: op_tool_name ∘ split_tool_name is identity for file verbs
    for name in FILE_VERB_TOOL_NAMES:
        kind, verb = split_tool_name(name)
        assert kind == "file" and verb is not None
        assert op_tool_name(kind, verb) == name


def test_is_op_instance_allowed_coarse_and_granular() -> None:
    """Tier 2: coarse `file` allows every verb (legacy); `file__read` allows read
    but NOT delete (P4 precision); non-file ops delegate to kind membership."""
    read = FileIROp(kind="file", op="read", path="x")
    delete = FileIROp(kind="file", op="delete", path="x")
    web = WebFetchIROp(kind="web_fetch", url="http://x")

    # coarse — behavior-preserving for existing `allowed_ops: [file]`
    assert is_op_instance_allowed(read, {"file"})
    assert is_op_instance_allowed(delete, {"file"})
    # granular — the read-but-not-delete win
    assert is_op_instance_allowed(read, {"file__read"})
    assert not is_op_instance_allowed(delete, {"file__read"})
    # non-file unchanged
    assert is_op_instance_allowed(web, {"web_fetch"})
    assert not is_op_instance_allowed(web, {"web_search"})


# ── End-to-end gating through the real executor ──────────────────────────────


def test_file_verb_gating_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the allowed_ops frontend filter enforces file verb granularity via
    the real executor — file__read admits a read op and skips a delete op, while
    a coarse `file` entry admits the delete (behavior-preserving)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hello.txt").write_text("hi")
    decl = PermissionDecl()
    read = FileIROp(kind="file", op="read", path="hello.txt")
    delete = FileIROp(kind="file", op="delete", path="hello.txt")

    # granular allow-read: read NOT skipped, delete skipped
    ex1, ev1 = _make_executor(tmp_path)
    _run(ex1.execute([read], phase="p", decl=decl, allowed_ops={"file__read"}))
    assert "file" not in _skipped_kinds(ev1), "file__read must admit a read op"

    ex2, ev2 = _make_executor(tmp_path)
    _run(ex2.execute([delete], phase="p", decl=decl, allowed_ops={"file__read"}))
    assert _skipped_kinds(ev2) == ["file"], "file__read must skip a delete op (P4 win)"

    # coarse file: delete NOT skipped (legacy behavior-preserving)
    ex3, ev3 = _make_executor(tmp_path)
    _run(ex3.execute([delete], phase="p", decl=decl, allowed_ops={"file"}))
    assert "file" not in _skipped_kinds(ev3), "coarse `file` must admit every verb"


# ── Catalog + conversion (op-loop offer) ─────────────────────────────────────


def test_catalog_file_verb_drops_implied_op(tmp_path: Path) -> None:
    """Tier 2: a file__read catalog tool carries the file schema with `op` dropped
    (the verb is implied by the name); coarse `file` keeps `op`; web_fetch is a
    plain kind-level tool."""
    catalog = _build_phase_tool_catalog({"file__read", "file", "web_fetch"})
    fr_props = catalog["file__read"]["function"]["parameters"]["properties"]
    coarse_props = catalog["file"]["function"]["parameters"]["properties"]
    assert "op" not in fr_props, "file__read implies op=read; op must be dropped"
    assert "op" in coarse_props, "coarse file keeps the op selector"
    assert catalog["web_fetch"]["function"]["name"] == "web_fetch"


def test_conversion_file_verb_injects_op() -> None:
    """Tier 2: a file__read tool_call converts to FileIROp(op=read); a coarse
    file tool_call keeps the op carried in its arguments."""
    op = tool_call_to_control_ir_op(
        {"function": {"name": "file__read", "arguments": '{"path": "x.py"}'}}
    )
    assert isinstance(op, FileIROp) and op.op == "read" and op.path == "x.py"
    coarse = tool_call_to_control_ir_op(
        {"function": {"name": "file", "arguments": '{"op": "write", "path": "y", "content": "z"}'}}
    )
    assert isinstance(coarse, FileIROp) and coarse.op == "write"


# ── Linter ───────────────────────────────────────────────────────────────────


def test_linter_accepts_file_verb_warns_unknown() -> None:
    """Tier 2: the linter accepts file__read (a known verb tool-name) and warns on
    an invented file verb."""
    ok = _lint_allowed_ops(Path("p.md"), {"allowed_ops": ["file__read", "file", "web_fetch"]})
    assert ok == [], f"known names must not warn, got {ok}"
    bad = _lint_allowed_ops(Path("p.md"), {"allowed_ops": ["file__bogus"]})
    assert any("file__bogus" in i.message for i in bad), "unknown verb tool-name must warn"
