"""Helpers for rendering reyn ``tool_call_*`` outbox messages as
collapsible chainlit ``cl.Step`` panels.

The reyn lifecycle forwarder packs three fields the operator wants to
see inside the step body:

- ``tool``        (= human-friendly tool name, used as step title)
- ``args``        (only on ``tool_call_started``)
- ``result``      (only on ``tool_call_completed``)
- ``error_kind``  (only on ``tool_call_failed``)
- ``error_message``

Plus ``op_id`` (= the deterministic ``args_hash``) we use as the
per-session key to pair start / completed / failed events into the
same step row. Without ``op_id`` matching, a tool's "✓ done" line
would land in its own un-collapsed step instead of being absorbed
back into the original started step.

Pure helper module — no chainlit import, so unit tests run without
the ``[chainlit]`` extra installed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolStepUpdate:
    """Pre-computed args for a ``cl.Step`` create-or-update call.

    The chainlit-side caller decides whether to call ``Step(...).send()``
    (= new step for a started event) or ``Step.update()`` after
    populating an existing object (= for completed / failed).
    """
    op_id: str
    tool_name: str
    # ``"started"`` → caller creates a new Step and sends it.
    # ``"completed"`` / ``"failed"`` → caller looks up the prior
    # step by ``op_id`` and updates it. ``"failed"`` also sets a
    # visible error glyph in the output.
    phase: str
    # JSON-stringified args dict for the step's ``input`` panel
    # (= empty when args were missing or unserialisable).
    input_text: str
    # Rendered ``output`` text. Empty for the started phase.
    output_text: str
    # True when the failure path is taken — caller can flag the step
    # accordingly (e.g. metadata error icon).
    is_error: bool


_TOOL_CALL_KINDS = frozenset({
    "tool_call_started",
    "tool_call_completed",
    "tool_call_failed",
})


def is_tool_call(kind: str) -> bool:
    """Return True when ``kind`` is one of the 3 ``tool_call_*`` shapes
    this module knows how to render."""
    return kind in _TOOL_CALL_KINDS


def _stringify(value: Any) -> str:
    """Best-effort JSON for the input / output panels.

    Falls back to ``str(value)`` when the value isn't json-serialisable
    so the panel still shows something. Truncates absurdly long blobs
    so the popup doesn't drown the operator.
    """
    if value is None:
        return ""
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        text = str(value)
    # Soft cap so a huge result blob doesn't make the step unusable.
    if len(text) > 8000:
        text = text[:8000] + "\n… (truncated)"
    return text


def build_tool_step_update(meta: dict | None, kind: str) -> ToolStepUpdate | None:
    """Convert one tool_call_* outbox message's ``meta`` into step args.

    Returns ``None`` when ``meta`` lacks the bare-minimum routing
    fields (``op_id`` + ``tool``) — caller falls back to the adapter's
    plain-text branch so the row at least appears.
    """
    if not isinstance(meta, dict):
        return None
    op_id = meta.get("op_id")
    tool = meta.get("tool")
    if not isinstance(op_id, str) or not isinstance(tool, str) or not op_id or not tool:
        return None

    if kind == "tool_call_started":
        return ToolStepUpdate(
            op_id=op_id,
            tool_name=tool,
            phase="started",
            input_text=_stringify(meta.get("args")),
            output_text="",
            is_error=False,
        )
    if kind == "tool_call_completed":
        return ToolStepUpdate(
            op_id=op_id,
            tool_name=tool,
            phase="completed",
            input_text="",
            output_text=_stringify(meta.get("result")),
            is_error=False,
        )
    if kind == "tool_call_failed":
        err_kind = meta.get("error_kind") or ""
        err_msg = meta.get("error_message") or ""
        if err_kind and err_msg:
            body = f"{err_kind}: {err_msg}"
        else:
            body = str(err_kind or err_msg or "(unknown error)")
        return ToolStepUpdate(
            op_id=op_id,
            tool_name=tool,
            phase="failed",
            input_text="",
            output_text=body,
            is_error=True,
        )
    return None


__all__ = [
    "ToolStepUpdate",
    "build_tool_step_update",
    "is_tool_call",
]
