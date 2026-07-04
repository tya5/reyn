"""Linear Pipeline executor — R3 pipe-data threading + R4 step-boundary recovery.

The thin vertical-slice executor for
``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``: a **linear** sequence of
``transform`` / ``tool`` (``shell`` is just a ``ToolStep(name="shell", ...)``) steps.
Deliberately out of scope for this slice: `agent` steps, `for_each`/`parallel`/`fold`/
`match`/`call`/`refine`, the YAML DSL parser, and driver-as-session integration — those
are later slices; this module proves the core loop + recovery only.

**R3 (uniform pipe-data / output rule)**: every step produces exactly one return value.
That value is simultaneously (1) the pipe data handed to the next step and (2) written
to a named store under ``output`` iff the step declares one. A ``transform`` step's
``value`` is a R1 expression (``reyn.core.pipeline.expr`` — the total evaluator; this
module never hand-rolls expression evaluation), evaluated against a context of
``{"ctx": named_stores, "pipe": pipe_data}`` — so ``ctx.NAME`` reaches an earlier step's
named output and bare ``pipe`` reaches the immediately-preceding step's return value even
when it had no ``output:``. A ``tool`` step's ``args`` may mark any value as an
expression to resolve (rather than a literal) by wrapping it in :class:`ExprRef`; both
resolve through the same context via the same evaluator.

**R4 (step-boundary recovery)**: after each step completes (before advancing), the
executor calls :func:`reyn.core.events.pipeline_recovery.record_pipeline_state` with the
full control-plane state — ``{run_id, step_index, named_stores, pipe_data,
completed_step_results}`` — keyed at the durable WAL head. Side-effecting ``tool`` steps
use the awaited-durable path (narrowing the effect-done/snapshot-not-yet-durable crash
window); pure ``transform`` steps use the non-blocking path. :meth:`PipelineExecutor.run`
starts a run from scratch; :meth:`PipelineExecutor.resume` loads the latest recorded
generation for a run and REPLAYS every step already present in
``completed_step_results`` (not re-executing it — the exactly-once contract that keeps a
crash from re-running a `tool` step's side effect), resuming live execution at the first
step with no recorded result.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Union

from reyn.core.events.pipeline_recovery import latest_pipeline_state, record_pipeline_state
from reyn.core.pipeline.expr import ExprEvalError, evaluate_expr
from reyn.core.pipeline.schema import SchemaRegistry, validate

if TYPE_CHECKING:
    from reyn.core.events.state_log import StateLog

# ---------------------------------------------------------------------------
# Pipeline representation (pre-built dataclasses — no YAML parser in scope)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExprRef:
    """Marks a ``ToolStep.args`` value as an R1 expression SOURCE to resolve against
    the step's context, rather than a literal Python value passed through as-is. A bare
    Python value (``"shell"``, ``42``, ``["a", "b"]``) in ``args`` is always a literal —
    only an explicit ``ExprRef`` is evaluated, so there is no parse-ambiguity between "a
    literal string that happens to look like an expression" and "an expression"."""

    src: str


@dataclass(frozen=True)
class TransformStep:
    """A pure step: ``value`` (an R1 expression source) is evaluated against the
    current context; its result is the step's pipe data / named output per R3."""

    value: str
    output: "str | None" = None


@dataclass(frozen=True)
class ToolStep:
    """A side-effecting step: ``tool_dispatch(name, resolved_args)`` is invoked (``args``
    values wrapped in :class:`ExprRef` are resolved against the context first; other
    values pass through literally). ``shell`` is just ``ToolStep(name="shell", ...)`` —
    there is no separate shell-step type. ``schema``, if set, names a
    ``SchemaRegistry``-registered schema the step's result must conform to (verify:
    schema) — non-conformance fails the step."""

    name: str
    args: "dict[str, Any]" = field(default_factory=dict)
    output: "str | None" = None
    schema: "str | None" = None


Step = Union[TransformStep, ToolStep]


@dataclass(frozen=True)
class Pipeline:
    """A linear sequence of steps."""

    steps: "list[Step]"


@dataclass(frozen=True)
class PipelineResult:
    """The outcome of a completed `run`/`resume`: the final pipe data, every named
    store, and every step's result (keyed by ``step_path`` — the linear step index as a
    string; a later multi-scope slice would use dotted scope paths)."""

    run_id: str
    pipe_data: Any
    named_stores: "dict[str, Any]"
    completed_step_results: "dict[str, Any]"
    step_index: int


class PipelineError(Exception):
    """Base class for pipeline-execution errors."""


class PipelineExecutionError(PipelineError):
    """Raised when a step fails: an expression error, an unresolvable tool result
    against its declared schema, or an unrecognized step type. The executor never
    silently continues past a failed step."""


ToolDispatch = Callable[[str, "dict[str, Any]"], Any]


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PipelineExecutor:
    """Runs a :class:`Pipeline` sequentially, threading pipe data + named stores
    (R3) and recording a step-boundary recovery generation after each step (R4)."""

    async def run(
        self,
        pipeline: Pipeline,
        initial_context: "dict[str, Any] | None",
        *,
        tool_dispatch: ToolDispatch,
        state_log: "StateLog | None",
        run_id: str,
        schema_registry: "SchemaRegistry | None" = None,
    ) -> PipelineResult:
        """Run `pipeline` from the first step. `initial_context` seeds the named
        stores (``ctx.*``) available to the first step; there is no incoming pipe
        data (``pipe`` resolves to ``None`` until the first step produces one)."""
        named_stores: "dict[str, Any]" = dict(initial_context) if initial_context else {}
        return await self._run_from(
            pipeline,
            named_stores=named_stores,
            pipe_data=None,
            completed_step_results={},
            start_index=0,
            tool_dispatch=tool_dispatch,
            state_log=state_log,
            run_id=run_id,
            schema_registry=schema_registry,
        )

    async def resume(
        self,
        run_id: str,
        *,
        pipeline: Pipeline,
        tool_dispatch: ToolDispatch,
        state_log: "StateLog",
        schema_registry: "SchemaRegistry | None" = None,
    ) -> PipelineResult:
        """Resume `run_id`: load the latest recorded generation (R4) and replay every
        step already in ``completed_step_results`` (no re-execution — exactly-once),
        resuming live execution at the first step with no recorded result. With no
        snapshot at all, resume == run from scratch."""
        snapshot = latest_pipeline_state(run_id, state_log)
        if snapshot is None:
            return await self.run(
                pipeline,
                None,
                tool_dispatch=tool_dispatch,
                state_log=state_log,
                run_id=run_id,
                schema_registry=schema_registry,
            )
        return await self._run_from(
            pipeline,
            named_stores=dict(snapshot["named_stores"]),
            pipe_data=snapshot["pipe_data"],
            completed_step_results=dict(snapshot["completed_step_results"]),
            start_index=int(snapshot["step_index"]),
            tool_dispatch=tool_dispatch,
            state_log=state_log,
            run_id=run_id,
            schema_registry=schema_registry,
        )

    async def _run_from(
        self,
        pipeline: Pipeline,
        *,
        named_stores: "dict[str, Any]",
        pipe_data: Any,
        completed_step_results: "dict[str, Any]",
        start_index: int,
        tool_dispatch: ToolDispatch,
        state_log: "StateLog | None",
        run_id: str,
        schema_registry: "SchemaRegistry | None",
    ) -> PipelineResult:
        steps = pipeline.steps
        for i in range(start_index, len(steps)):
            step = steps[i]
            context = {"ctx": named_stores, "pipe": pipe_data}

            if isinstance(step, TransformStep):
                try:
                    result = evaluate_expr(step.value, context)
                except ExprEvalError as exc:
                    raise PipelineExecutionError(
                        f"step {i} (transform) failed: {exc}"
                    ) from exc
                durable = False
            elif isinstance(step, ToolStep):
                resolved_args = {
                    k: (evaluate_expr(v.src, context) if isinstance(v, ExprRef) else v)
                    for k, v in step.args.items()
                }
                raw = tool_dispatch(step.name, resolved_args)
                result = await raw if inspect.isawaitable(raw) else raw
                if step.schema is not None:
                    if schema_registry is None:
                        raise PipelineExecutionError(
                            f"step {i} (tool {step.name!r}) declares verify: schema "
                            f"{step.schema!r} but no schema_registry was provided"
                        )
                    validation = validate(result, step.schema, schema_registry)
                    if not validation.conforming:
                        raise PipelineExecutionError(
                            f"step {i} (tool {step.name!r}) output failed schema "
                            f"{step.schema!r}: {validation.errors}"
                        )
                durable = True
            else:  # pragma: no cover - Step is a closed union
                raise PipelineExecutionError(f"unknown step type: {step!r}")

            pipe_data = result
            step_index = i + 1
            completed_step_results = {**completed_step_results, str(i): result}
            if step.output:
                named_stores = {**named_stores, step.output: result}

            control_plane_state = {
                "run_id": run_id,
                "step_index": step_index,
                "named_stores": named_stores,
                "pipe_data": pipe_data,
                "completed_step_results": completed_step_results,
            }
            await record_pipeline_state(
                state_log, run_id, control_plane_state, durable=durable,
            )

        return PipelineResult(
            run_id=run_id,
            pipe_data=pipe_data,
            named_stores=named_stores,
            completed_step_results=completed_step_results,
            step_index=len(steps),
        )


__all__ = [
    "ExprRef",
    "TransformStep",
    "ToolStep",
    "Step",
    "Pipeline",
    "PipelineResult",
    "PipelineError",
    "PipelineExecutionError",
    "PipelineExecutor",
]
