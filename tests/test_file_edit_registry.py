"""Tier 2: edit_file ToolDefinition — FP-0040 (#178).

Pins the router-side `edit_file` capability + `file__edit` universal-
catalog routing. The op_runtime-level edit semantics (= unique-string
match, replace_all flag, not-found envelope) are covered separately by
`test_op_runtime_file_not_found_suggestions.py` and the op_runtime
contract tests; this file pins the **registry adapter** layer:

  1. `edit_file` is registered in `get_default_registry()` with
     `gates(router=allow, phase=allow)`, `purity=side_effect`, and the
     `path / old_string / new_string / replace_all` schema.
  2. `file__edit` qualified name dispatches to `edit_file` via
     `_OPERATION_RULES`.
  3. End-to-end invocation through `invoke_tool` correctly flows args
     to the op_runtime edit handler, including the error branches for
     0-match and multi-match-without-replace_all.

Tests use a real workspace + real op_runtime — no mocks.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from reyn.data.workspace.workspace import Workspace
from reyn.events.events import EventLog
from reyn.security.permissions.permissions import PermissionResolver
from reyn.tools import get_default_registry
from reyn.tools.dispatch import invoke_tool
from reyn.tools.types import RouterCallerState, ToolContext
from reyn.tools.universal_dispatch import _OPERATION_RULES, resolve_invoke_action

# ── helpers ────────────────────────────────────────────────────────────────────


def _resolver(tmp_path: Path) -> PermissionResolver:
    return PermissionResolver(
        config_permissions={"file.read": "allow", "file.write": "allow"},
        project_root=tmp_path,
        interactive=False,
    )


def _ctx(tmp_path: Path) -> ToolContext:
    events = EventLog()
    ws = Workspace(events=events)
    return ToolContext(
        events=events,
        permission_resolver=_resolver(tmp_path),
        workspace=ws,
        caller_kind="router",
        router_state=RouterCallerState(),
    )


def _run(coro):
    return asyncio.run(coro)


# ── 1. Registry registration ──────────────────────────────────────────────


def test_edit_file_registered() -> None:
    """Tier 2: edit_file ToolDefinition is in the default registry."""
    reg = get_default_registry()
    td = reg.lookup("edit_file")
    assert td is not None, "edit_file is missing from get_default_registry()"


def test_edit_file_gates_router_and_phase_allow() -> None:
    """Tier 2: edit_file is callable from both router and phase paths.

    Same gating tier as write_file / delete_file — partial edit is a
    write-class operation.
    """
    td = get_default_registry().lookup("edit_file")
    assert td is not None
    assert td.gates.router == "allow"
    assert td.gates.phase == "allow"


def test_edit_file_purity_is_side_effect() -> None:
    """Tier 2: edit_file declares side_effect purity (= writes workspace)."""
    td = get_default_registry().lookup("edit_file")
    assert td is not None
    assert td.purity == "side_effect"


def test_edit_file_parameters_require_path_old_new() -> None:
    """Tier 2: required params are path / old_string / new_string.

    replace_all is OPTIONAL — defaults to false per the FP-0040 design
    (= fail-loud unless explicitly opted in for rename use cases).
    """
    td = get_default_registry().lookup("edit_file")
    assert td is not None
    params = td.parameters
    assert set(params["required"]) == {"path", "old_string", "new_string"}
    assert "replace_all" in params["properties"]
    assert params["properties"]["replace_all"]["type"] == "boolean"


# ── 2. Universal-catalog dispatch ─────────────────────────────────────────


def test_file_edit_qualified_name_routes_to_edit_file() -> None:
    """Tier 2: file__edit dispatches to the edit_file tool."""
    target, _ = _OPERATION_RULES["file__edit"]
    assert target == "edit_file"


def test_file_edit_resolves_via_universal_dispatch() -> None:
    """Tier 2: resolve_invoke_action finds file__edit with args passthrough."""
    resolved = resolve_invoke_action(
        "file__edit",
        {"path": "x", "old_string": "a", "new_string": "b"},
    )
    assert resolved.target_tool_name == "edit_file"
    assert dict(resolved.target_args) == {
        "path": "x",
        "old_string": "a",
        "new_string": "b",
    }


# ── 3. End-to-end invocation through invoke_tool ──────────────────────────


def test_edit_single_unique_match_replaces(tmp_path, monkeypatch):
    """Tier 2: a unique old_string is replaced in place."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("hello world\ngoodbye moon\n")

    result = _run(invoke_tool(
        get_default_registry(),
        "edit_file",
        {"path": "f.txt", "old_string": "hello world", "new_string": "hi world"},
        _ctx(tmp_path),
    ))

    assert result["status"] == "ok"
    assert result["replacements"] == 1
    assert (tmp_path / "f.txt").read_text() == "hi world\ngoodbye moon\n"


def test_edit_multi_match_without_replace_all_errors(tmp_path, monkeypatch):
    """Tier 2: multiple matches + replace_all=false → error with count.

    Default fail-loud behavior so the LLM cannot silently rewrite the
    wrong occurrence.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("foo\nfoo\nfoo\n")

    result = _run(invoke_tool(
        get_default_registry(),
        "edit_file",
        {"path": "f.txt", "old_string": "foo", "new_string": "bar"},
        _ctx(tmp_path),
    ))

    assert result["status"] == "error"
    # File unchanged.
    assert (tmp_path / "f.txt").read_text() == "foo\nfoo\nfoo\n"


def test_edit_multi_match_with_replace_all_replaces_all(tmp_path, monkeypatch):
    """Tier 2: explicit replace_all=true replaces every occurrence."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("foo\nfoo\nfoo\n")

    result = _run(invoke_tool(
        get_default_registry(),
        "edit_file",
        {
            "path": "f.txt",
            "old_string": "foo",
            "new_string": "bar",
            "replace_all": True,
        },
        _ctx(tmp_path),
    ))

    assert result["status"] == "ok"
    assert result["replacements"] == 3
    assert (tmp_path / "f.txt").read_text() == "bar\nbar\nbar\n"


def test_edit_zero_match_errors(tmp_path, monkeypatch):
    """Tier 2: old_string not found in file → error, no write.

    The op_runtime surfaces ``status="error"`` (not ``not_found``)
    because the FILE exists; only the anchor string is missing.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "f.txt").write_text("alpha\nbeta\n")

    result = _run(invoke_tool(
        get_default_registry(),
        "edit_file",
        {"path": "f.txt", "old_string": "missing", "new_string": "x"},
        _ctx(tmp_path),
    ))

    assert result["status"] == "error"
    assert (tmp_path / "f.txt").read_text() == "alpha\nbeta\n"


def test_edit_missing_file_errors(tmp_path, monkeypatch):
    """Tier 2: edit on a non-existent file returns not_found envelope.

    Same shape as read on a missing file (= per
    test_op_runtime_file_not_found_suggestions.py contract).
    """
    monkeypatch.chdir(tmp_path)

    result = _run(invoke_tool(
        get_default_registry(),
        "edit_file",
        {"path": "ghost.txt", "old_string": "x", "new_string": "y"},
        _ctx(tmp_path),
    ))

    # Either status=not_found or status=error — both indicate failure.
    # Tightening to status=not_found pins op_runtime contract from a
    # different test file; here we just confirm no silent success.
    assert result["status"] != "ok"
