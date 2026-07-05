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

(#2572) :func:`schema_registry_from_dict` is the same round-trip idea applied
to a launch's :class:`~reyn.core.pipeline.schema.SchemaRegistry` —
``PipelineWorkOrder.schema_defs`` persists ``SchemaRegistry.as_dict()`` (an
already plain-JSON-primitive ``name -> schema dict`` map, no custom encoder
needed), and this rebuilds the registry so a ``verify: schema`` step is
enforceable on a fresh driver-session, including one re-created from disk
alone on a crash-resume.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable

from reyn.core.pipeline.executor import (
    AgentStep,
    CallStep,
    ExprRef,
    FoldStep,
    MatchCase,
    MatchStep,
    Pipeline,
    Step,
    ToolStep,
    TransformStep,
)

if TYPE_CHECKING:
    from reyn.core.pipeline.schema import SchemaRegistry

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


def _encode_transform(step: "TransformStep") -> "dict[str, Any]":
    return {"kind": "transform", "value": step.value, "output": step.output}


def _encode_tool(step: "ToolStep") -> "dict[str, Any]":
    return {
        "kind": "tool",
        "name": step.name,
        "args": {k: _encode_arg(step.name, k, v) for k, v in step.args.items()},
        "output": step.output,
        "schema": step.schema,
    }


def _encode_agent(step: "AgentStep") -> "dict[str, Any]":
    return {
        "kind": "agent",
        "prompt": step.prompt,
        "identity": step.identity,
        "capabilities": list(step.capabilities) if step.capabilities is not None else None,
        "schema": step.schema,
        "output": step.output,
    }


def _decode_transform(data: "dict[str, Any]") -> "TransformStep":
    return TransformStep(value=data["value"], output=data.get("output"))


def _decode_tool(data: "dict[str, Any]") -> "ToolStep":
    return ToolStep(
        name=data["name"],
        args={k: _decode_arg(v) for k, v in dict(data.get("args") or {}).items()},
        output=data.get("output"),
        schema=data.get("schema"),
    )


def _decode_agent(data: "dict[str, Any]") -> "AgentStep":
    caps = data.get("capabilities")
    return AgentStep(
        prompt=data["prompt"],
        identity=data.get("identity"),
        capabilities=list(caps) if caps is not None else None,
        schema=data.get("schema"),
        output=data.get("output"),
    )


def _encode_call(step: "CallStep") -> "dict[str, Any]":
    # ``pass_`` (the Python field — ``pass`` is a keyword) serializes under the
    # Appendix-B wire/DSL key ``"pass"``. This is the one place a step field name
    # differs from its wire key.
    return {
        "kind": "call",
        "pipeline": step.pipeline,
        "pass": list(step.pass_),
        "output": step.output,
    }


def _decode_call(data: "dict[str, Any]") -> "CallStep":
    return CallStep(
        pipeline=data["pipeline"],
        pass_=list(data.get("pass") or []),
        output=data.get("output"),
    )


def _encode_match_case(case: "MatchCase") -> "dict[str, Any]":
    return {"pipeline": case.pipeline, "pass": list(case.pass_)}


def _decode_match_case(data: "dict[str, Any]") -> "MatchCase":
    return MatchCase(pipeline=data["pipeline"], pass_=list(data.get("pass") or []))


def _encode_match(step: "MatchStep") -> "dict[str, Any]":
    return {
        "kind": "match",
        "on": step.on,
        "cases": {label: _encode_match_case(case) for label, case in step.cases.items()},
        "default": _encode_match_case(step.default) if step.default is not None else None,
        "output": step.output,
    }


def _decode_match(data: "dict[str, Any]") -> "MatchStep":
    raw_default = data.get("default")
    return MatchStep(
        on=data["on"],
        cases={
            label: _decode_match_case(case)
            for label, case in dict(data.get("cases") or {}).items()
        },
        default=_decode_match_case(raw_default) if raw_default is not None else None,
        output=data.get("output"),
    )


def _encode_fold(step: "FoldStep") -> "dict[str, Any]":
    # ``do`` is itself a Step — recurse through `step_to_dict` so a fold whose
    # `do` is a `call` (or a nested `fold`) round-trips just as faithfully.
    return {
        "kind": "fold",
        "over": step.over,
        "items": list(step.items) if step.items is not None else None,
        "init": step.init,
        "do": step_to_dict(step.do),
        "output": step.output,
        "max_items": step.max_items,
    }


def _decode_fold(data: "dict[str, Any]") -> "FoldStep":
    return FoldStep(
        init=data["init"],
        do=step_from_dict(data["do"]),
        output=data["output"],
        over=data.get("over"),
        items=list(data["items"]) if data.get("items") is not None else None,
        max_items=data.get("max_items"),
    )


# Dispatch tables: encoder keyed by step type, decoder keyed by ``kind`` marker.
# A future primitive ADDS one entry to each (mirroring the executor's
# ``STEP_DISPATCH`` and the parser's ``_STEP_PARSERS``) rather than editing a
# shared isinstance/``kind==`` chain.
ENCODERS: "dict[type, Callable[[Step], dict[str, Any]]]" = {
    TransformStep: _encode_transform,
    ToolStep: _encode_tool,
    AgentStep: _encode_agent,
    CallStep: _encode_call,
    MatchStep: _encode_match,
    FoldStep: _encode_fold,
}
DECODERS: "dict[str, Callable[[dict[str, Any]], Step]]" = {
    "transform": _decode_transform,
    "tool": _decode_tool,
    "agent": _decode_agent,
    "call": _decode_call,
    "match": _decode_match,
    "fold": _decode_fold,
}


def step_to_dict(step: "Step") -> "dict[str, Any]":
    """One executor step dataclass → a JSON-serializable dict (``kind`` tagged)."""
    encoder = ENCODERS.get(type(step))
    if encoder is None:
        raise PipelineSerdeError(f"unknown step type: {step!r}")
    return encoder(step)


def step_from_dict(data: "dict[str, Any]") -> "Step":
    """The inverse of :func:`step_to_dict`."""
    kind = data.get("kind")
    decoder = DECODERS.get(kind) if isinstance(kind, str) else None
    if decoder is None:
        raise PipelineSerdeError(f"unknown step kind in stored pipeline: {kind!r}")
    return decoder(data)


def pipeline_to_dict(pipeline: "Pipeline") -> "dict[str, Any]":
    """A :class:`Pipeline` → a JSON-serializable dict (the work-order shape)."""
    return {
        "description": pipeline.description,
        # #2575: the declared ``pipeline:`` name travels with the work-order so
        # a recovered pipeline keeps its identity (``call``/``match`` targets).
        "name": pipeline.name,
        "steps": [step_to_dict(s) for s in pipeline.steps],
    }


def pipeline_from_dict(data: "dict[str, Any]") -> "Pipeline":
    """The inverse of :func:`pipeline_to_dict`.

    ``name`` is default-tolerant (#2575): an invocation.json persisted before
    the field existed has no ``name`` key → ``""`` (a benign identity gap for a
    long-since-launched inline run, which does not re-resolve by name)."""
    return Pipeline(
        steps=[step_from_dict(s) for s in data.get("steps", [])],
        description=str(data.get("description") or ""),
        name=str(data.get("name") or ""),
    )


def schema_registry_from_dict(data: "dict[str, dict[str, Any]] | None") -> "SchemaRegistry":
    """(#2572) The inverse of :meth:`~reyn.core.pipeline.schema.SchemaRegistry.
    as_dict`: rebuild a :class:`~reyn.core.pipeline.schema.SchemaRegistry` from
    a persisted ``name -> schema dict`` map (``PipelineWorkOrder.schema_defs``),
    mirroring how :func:`pipeline_from_dict` rebuilds the ``Pipeline`` from its
    own work-order field.

    Registers each entry via ``SchemaRegistry.register`` — the same
    shape/cycle validation a live registration goes through, so a corrupted
    ``schema_defs`` fails loudly here rather than silently at first use. Entry
    order does not matter: the cycle check tolerates forward refs among
    schemas not yet registered (see ``SchemaRegistry.register``'s docstring),
    so registering in dict-iteration order round-trips correctly regardless of
    the original registration order. ``None`` (no schemas at launch) and
    ``{}`` both yield an empty registry."""
    from reyn.core.pipeline.schema import SchemaRegistry

    registry = SchemaRegistry()
    for name, schema in (data or {}).items():
        registry.register(name, schema)
    return registry


__all__ = [
    "PipelineSerdeError",
    "pipeline_to_dict",
    "pipeline_from_dict",
    "step_to_dict",
    "step_from_dict",
    "schema_registry_from_dict",
]
