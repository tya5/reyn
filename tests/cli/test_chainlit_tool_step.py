"""Tier 1: ``reyn.chainlit_app.tool_step.build_tool_step_update`` contract.

The drain loop's ``_handle_tool_call`` consumes ``ToolStepUpdate``
to drive ``cl.Step.send()`` / ``cl.Step.update()`` for the
collapsible tool panel. Pins:

1. The 3 ``tool_call_*`` kinds each produce a distinct
   ``phase`` (= ``"started"`` / ``"completed"`` / ``"failed"``).
2. ``op_id`` round-trips so start / complete / fail share a step.
3. Failed phase surfaces ``is_error=True``; the body combines
   ``error_kind`` + ``error_message`` when both are present.
4. ``args`` / ``result`` get JSON-stringified into the panel; non-
   serialisable values fall back to ``str(value)`` instead of
   crashing.
5. Missing ``op_id`` / ``tool`` returns ``None`` so the caller falls
   back to the plain-text adapter render.
6. ``is_tool_call`` covers exactly the 3 kinds.
"""
from __future__ import annotations

import pytest

from reyn.chainlit_app.tool_step import (
    ToolStepUpdate,
    build_tool_step_update,
    is_tool_call,
)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("tool_call_started", True),
        ("tool_call_completed", True),
        ("tool_call_failed", True),
        ("tool_called", False),  # legacy event name
        ("agent", False),
        ("status", False),
        ("", False),
    ],
)
def test_is_tool_call_truth_table(kind: str, expected: bool):
    """Tier 1: helper recognises exactly the 3 lifecycle kinds."""
    assert is_tool_call(kind) is expected


def test_started_phase_carries_args_in_input_text():
    """Tier 1: ``tool_call_started`` → phase "started" + input_text
    JSON-encoded from ``meta.args``; output_text empty."""
    upd = build_tool_step_update(
        {
            "op_id": "abc123",
            "tool": "file__read",
            "args": {"path": "/etc/hosts"},
        },
        "tool_call_started",
    )
    assert upd is not None
    assert upd.phase == "started"
    assert upd.op_id == "abc123"
    assert upd.tool_name == "file__read"
    assert "/etc/hosts" in upd.input_text
    assert upd.output_text == ""
    assert upd.is_error is False


def test_completed_phase_carries_result_in_output_text():
    """Tier 1: ``tool_call_completed`` → phase "completed" + output_text
    JSON-encoded from ``meta.result``; input_text empty (= the step
    already has args from the started phase)."""
    upd = build_tool_step_update(
        {
            "op_id": "abc123",
            "tool": "file__read",
            "result": {"bytes": 1234, "content": "..."},
        },
        "tool_call_completed",
    )
    assert upd is not None
    assert upd.phase == "completed"
    assert upd.op_id == "abc123"
    assert "1234" in upd.output_text
    assert upd.input_text == ""
    assert upd.is_error is False


def test_failed_phase_combines_error_kind_and_message():
    """Tier 1: ``tool_call_failed`` → phase "failed", is_error=True,
    output combines kind + message when both present."""
    upd = build_tool_step_update(
        {
            "op_id": "abc123",
            "tool": "net__fetch",
            "error_kind": "TimeoutError",
            "error_message": "request timed out after 30s",
        },
        "tool_call_failed",
    )
    assert upd is not None
    assert upd.phase == "failed"
    assert upd.is_error is True
    assert "TimeoutError" in upd.output_text
    assert "timed out" in upd.output_text


def test_failed_phase_falls_back_to_unknown_label():
    """Tier 1: failure without kind / message → "(unknown error)" body."""
    upd = build_tool_step_update(
        {"op_id": "x", "tool": "t"},
        "tool_call_failed",
    )
    assert upd is not None
    assert upd.is_error is True
    assert upd.output_text == "(unknown error)"


def test_missing_op_id_returns_none():
    """Tier 1: missing ``op_id`` → None (caller falls back to adapter
    plain-text)."""
    assert build_tool_step_update(
        {"tool": "t"}, "tool_call_started",
    ) is None
    assert build_tool_step_update(
        {"op_id": "", "tool": "t"}, "tool_call_started",
    ) is None


def test_missing_tool_returns_none():
    """Tier 1: missing ``tool`` → None (no step title would render)."""
    assert build_tool_step_update(
        {"op_id": "x"}, "tool_call_started",
    ) is None
    assert build_tool_step_update(
        {"op_id": "x", "tool": ""}, "tool_call_started",
    ) is None


def test_non_dict_meta_returns_none():
    """Tier 1: defensive — None / non-dict meta → None."""
    assert build_tool_step_update(None, "tool_call_started") is None
    assert build_tool_step_update("not a dict", "tool_call_started") is None  # type: ignore[arg-type]


def test_non_serialisable_args_fall_back_to_str():
    """Tier 1: when ``meta.args`` contains a non-json type (= e.g. an
    object), the helper uses ``str(value)`` instead of crashing."""

    class _Unserialisable:
        def __repr__(self) -> str:
            return "<NotJsonAble>"

    upd = build_tool_step_update(
        {
            "op_id": "x",
            "tool": "t",
            "args": _Unserialisable(),
        },
        "tool_call_started",
    )
    assert upd is not None
    assert "NotJsonAble" in upd.input_text


def test_huge_output_is_truncated():
    """Tier 1: 8000-char cap on output_text so a giant result blob
    doesn't make the step unusable in the UI."""
    huge = "x" * 20_000
    upd = build_tool_step_update(
        {"op_id": "x", "tool": "t", "result": huge},
        "tool_call_completed",
    )
    assert upd is not None
    # Strictly shorter than the input (= proves the cap fired) and
    # contains the truncation marker (= proves we left a breadcrumb).
    # Avoids pinning the exact byte cap so a future tweak of the
    # 8000-char limit doesn't have to update this test.
    assert upd.output_text != huge
    assert "xxxxx" in upd.output_text
    assert "truncated" in upd.output_text


def test_unknown_kind_returns_none():
    """Tier 1: a kind outside the lifecycle set → None (= caller's
    branch never enters the step path for them)."""
    assert build_tool_step_update(
        {"op_id": "x", "tool": "t"}, "tool_called",
    ) is None
    assert build_tool_step_update(
        {"op_id": "x", "tool": "t"}, "agent",
    ) is None


def test_return_type_is_tool_step_update():
    """Tier 1: return is the public dataclass (= caller reads .op_id /
    .phase / .input_text / etc. without dict gymnastics)."""
    upd = build_tool_step_update(
        {"op_id": "x", "tool": "t"}, "tool_call_started",
    )
    assert isinstance(upd, ToolStepUpdate)
