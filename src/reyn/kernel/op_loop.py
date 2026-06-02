"""#1212 PR2: native-tools op-loop helpers.

The op-loop (ADR-0035 D1-D4) executes a phase's ops via native function-calling
instead of the json-mode ``control_ir`` batch: the model emits ``tool_calls``,
each is converted to a ``ControlIROp`` and run through the SHARED
``control_ir_executor`` (D8 — same dispatch / permission / events / WAL as the
json-mode path), and the result is fed back as a tool-role message until the
model stops requesting tools (``end_turn``); a separate json-mode transition call
then yields ``{control, artifact}``.

PR2 uses KIND-level tool names (the tool name IS the op kind, from
``_build_phase_tool_catalog``). Tool-name granularity (``file__read``) is PR4.
"""
from __future__ import annotations

import json

from pydantic import TypeAdapter

from reyn.schemas.models import ControlIROp

_CONTROL_IR_ADAPTER: TypeAdapter = TypeAdapter(ControlIROp)


def tool_call_to_control_ir_op(tool_call: dict) -> ControlIROp:
    """Convert one native ``tool_call`` to a ``ControlIROp`` for the shared executor.

    A tool_call is ``{"id": ..., "function": {"name": <kind>, "arguments": <json>}}``.
    PR2: the tool name is the op kind (kind-level catalog); ``arguments`` is the
    provider's JSON string (or already a dict) of op fields. The op is built by
    validating ``{"kind": <name>, **args}`` against the ``ControlIROp``
    discriminated union (``discriminator="kind"``).

    Raises ``json.JSONDecodeError`` on malformed arguments and
    ``pydantic.ValidationError`` on an unknown kind / invalid fields — both are
    caught by the op-loop's per-turn validation (mirrors the json-mode
    ``ActOutput.model_validate`` failure path).
    """
    fn = tool_call.get("function") or {}
    kind = fn.get("name") or ""
    raw_args = fn.get("arguments")
    if isinstance(raw_args, str):
        args = json.loads(raw_args) if raw_args.strip() else {}
    elif isinstance(raw_args, dict):
        args = dict(raw_args)
    else:
        args = {}
    return _CONTROL_IR_ADAPTER.validate_python({"kind": kind, **args})
