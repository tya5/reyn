"""Pipeline ⇄ JSON-dict serialization (IS-2 work-order support).

The async pipeline driver-session (IS-2) is *born with its work-order*: the
full Pipeline definition is persisted to
``.reyn/pipeline/state/<run_id>/invocation.json`` at spawn so a crashed
driver-session can be re-created from disk alone — the same full-state,
file-not-WAL-event recovery philosophy as the R4 step-boundary generations
(``reyn.core.events.pipeline_recovery``). That requires a faithful round-trip
between the executor's frozen step dataclasses
(:class:`~reyn.core.pipeline.executor.Pipeline` /
``TransformStep``/``ToolStep``/``AgentStep``) and plain JSON — this module is
that round-trip, and nothing else (no YAML, no validation beyond shape: the
DSL parser already validated the pipeline when it was registered).

The one non-mechanical part is :class:`~reyn.core.pipeline.executor.ExprRef`
inside ``ToolStep.args``: a JSON dict cannot carry a Python type, so an
``ExprRef(src)`` value is encoded as the kind-marked dict
``{"__exprref__": src}``. Kind-markers invite collisions, so the ambiguity is
closed at ENCODE time: a *literal* args dict that itself contains the
``"__exprref__"`` key is refused with :class:`PipelineSerdeError` naming the
colliding arg — it must not silently round-trip into an ``ExprRef`` (decode
would misread it) nor silently survive (a literal that decodes differently
than it encoded is corruption). Decode only recognises the exact one-key
marker shape ``{"__exprref__": <str>}`` as an ``ExprRef``; any other dict is
a literal.
"""
from __future__ import annotations

from typing import Any

from reyn.core.pipeline.executor import (
    AgentStep,
    ExprRef,
    Pipeline,
    Step,
    ToolStep,
    TransformStep,
)

_EXPRREF_KEY = "__exprref__"


class PipelineSerdeError(ValueError):
    """Raised when a Pipeline cannot be faithfully serialized/deserialized —
    e.g. an ``ExprRef`` marker-key collision in a literal ``ToolStep.args``
    value, or an unknown step ``kind`` in a stored work-order."""


def _encode_arg(step_name: str, key: str, value: Any) -> Any:
    if isinstance(value, ExprRef):
        return {_EXPRREF_KEY: value.src}
    if isinstance(value, dict) and _EXPRREF_KEY in value:
        raise PipelineSerdeError(
            f"tool step {step_name!r} arg {key!r} is a literal dict containing "
            f"the reserved key {_EXPRREF_KEY!r} — it would be indistinguishable "
            "from an ExprRef marker on decode. Rename the key or wrap the "
            "value differently."
        )
    return value


def _decode_arg(value: Any) -> Any:
    if (
        isinstance(value, dict)
        and set(value) == {_EXPRREF_KEY}
        and isinstance(value[_EXPRREF_KEY], str)
    ):
        return ExprRef(value[_EXPRREF_KEY])
    return value


def step_to_dict(step: "Step") -> "dict[str, Any]":
    """One executor step dataclass → a JSON-serializable dict (``kind`` tagged)."""
    if isinstance(step, TransformStep):
        return {"kind": "transform", "value": step.value, "output": step.output}
    if isinstance(step, ToolStep):
        return {
            "kind": "tool",
            "name": step.name,
            "args": {k: _encode_arg(step.name, k, v) for k, v in step.args.items()},
            "output": step.output,
            "schema": step.schema,
        }
    if isinstance(step, AgentStep):
        return {
            "kind": "agent",
            "prompt": step.prompt,
            "identity": step.identity,
            "capabilities": list(step.capabilities) if step.capabilities is not None else None,
            "schema": step.schema,
            "output": step.output,
        }
    raise PipelineSerdeError(f"unknown step type: {step!r}")


def step_from_dict(data: "dict[str, Any]") -> "Step":
    """The inverse of :func:`step_to_dict`."""
    kind = data.get("kind")
    if kind == "transform":
        return TransformStep(value=data["value"], output=data.get("output"))
    if kind == "tool":
        return ToolStep(
            name=data["name"],
            args={k: _decode_arg(v) for k, v in dict(data.get("args") or {}).items()},
            output=data.get("output"),
            schema=data.get("schema"),
        )
    if kind == "agent":
        caps = data.get("capabilities")
        return AgentStep(
            prompt=data["prompt"],
            identity=data.get("identity"),
            capabilities=list(caps) if caps is not None else None,
            schema=data.get("schema"),
            output=data.get("output"),
        )
    raise PipelineSerdeError(f"unknown step kind in stored pipeline: {kind!r}")


def pipeline_to_dict(pipeline: "Pipeline") -> "dict[str, Any]":
    """A :class:`Pipeline` → a JSON-serializable dict (the work-order shape)."""
    return {
        "description": pipeline.description,
        "steps": [step_to_dict(s) for s in pipeline.steps],
    }


def pipeline_from_dict(data: "dict[str, Any]") -> "Pipeline":
    """The inverse of :func:`pipeline_to_dict`."""
    return Pipeline(
        steps=[step_from_dict(s) for s in data.get("steps", [])],
        description=str(data.get("description") or ""),
    )


__all__ = [
    "PipelineSerdeError",
    "pipeline_to_dict",
    "pipeline_from_dict",
    "step_to_dict",
    "step_from_dict",
]
