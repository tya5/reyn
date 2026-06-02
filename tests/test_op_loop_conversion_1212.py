"""Tier 2: #1212 PR2 — native tool_call → ControlIROp conversion (kind-level).

The op-loop bridges native tool_calls to the SHARED control_ir_executor by
building a ControlIROp from each tool_call. PR2 uses kind-level tool names (the
tool name is the op kind), so the op is the discriminated-union member for that
kind with the call's arguments. Real ControlIROp models, no mocks.
"""
from __future__ import annotations

import pytest

from reyn.kernel.op_loop import tool_call_to_control_ir_op
from reyn.schemas.models import FileIROp


def test_tool_call_json_string_arguments_builds_op() -> None:
    """Tier 2: a tool_call with JSON-string arguments → the matching ControlIROp."""
    tc = {
        "id": "c1",
        "type": "function",
        "function": {"name": "file", "arguments": '{"op": "read", "path": "x.py"}'},
    }
    op = tool_call_to_control_ir_op(tc)
    assert isinstance(op, FileIROp)
    assert op.kind == "file" and op.op == "read" and op.path == "x.py"


def test_tool_call_dict_arguments_builds_op() -> None:
    """Tier 2: arguments already a dict (not a JSON string) also build the op."""
    tc = {"function": {"name": "file", "arguments": {"op": "grep", "path": ".", "pattern": "def "}}}
    op = tool_call_to_control_ir_op(tc)
    assert isinstance(op, FileIROp)
    assert op.op == "grep" and op.pattern == "def "


def test_tool_call_unknown_kind_raises() -> None:
    """Tier 2: an unknown op kind raises (caught by the op-loop's per-turn validation)."""
    tc = {"function": {"name": "not_a_real_kind", "arguments": "{}"}}
    with pytest.raises(Exception):  # pydantic ValidationError on the discriminated union
        tool_call_to_control_ir_op(tc)
