"""Pipeline executor — R3 pipe-data threading + R4 recovery + the non-linear
compositional foundation (``_run_scope`` + dotted-path recovery + ``call``).

The executor for ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``: a
sequence of ``transform`` / ``tool`` (``shell`` is just a ``ToolStep(name="shell",
...)``) / ``agent`` (R5) steps, plus two COMPOSITIONAL primitives: ``call`` (R7,
a STATIC callee) and ``match`` (``call``'s runtime-selected sibling — the ``on``
VALUE picks a LABEL, never a target; see :class:`MatchStep`). Still out of scope
for this slice: `for_each`/`parallel`/`fold`/`refine` — those are later
primitives that plug into the SAME dispatch + scope machinery this module now
exposes.

**Dispatch tables (R7 — the parallel-primitive seam).** Step execution is a
``dict``-dispatch keyed by the step's dataclass type
(:data:`STEP_DISPATCH`) rather than an ``isinstance`` chain — so a future
primitive ADDS one registry entry (here, plus a serde encoder/decoder, a parser,
and an analyzer facet) instead of editing a shared ``elif`` block. Every runner
has the uniform signature ``(_StepInvocation) -> (result, durable,
completed_step_results)``: leaf runners (``transform``/``tool``/``agent``) return
``completed_step_results`` unchanged and never record; a COMPOSITIONAL runner
(``call``) recurses into :meth:`PipelineExecutor._run_scope`, grows
``completed_step_results`` with its sub-scope's dotted keys, and records each
sub-step through the frozen recovery closure it is handed.

**R3 (uniform pipe-data / output rule)**: every step produces exactly one return
value. That value is simultaneously (1) the pipe data handed to the next step and
(2) written to a named store under ``output`` iff the step declares one. N2's
uniform rule extends this to compositional steps: a ``call`` step's return value
is its callee pipeline's FINAL step output (see :class:`CallStep`). A
``transform`` step's ``value`` is a R1 expression (``reyn.core.pipeline.expr``);
a ``tool`` step's ``args`` may mark a value as an expression via :class:`ExprRef`;
an ``agent`` step's ``prompt`` is an R5 ``TPL`` string interpolated by
:func:`_interpolate_prompt`. All resolve against a context of ``{"ctx":
named_stores, "pipe": pipe_data}``.

**R4 + dotted-path recovery (R7 — THE load-bearing decision)**: after each step
completes (before advancing), the executor records the full control-plane state —
``{run_id, step_index, named_stores, pipe_data, completed_step_results}`` — as a
truncation-surviving generation (``reyn.core.events.pipeline_recovery``). The
``completed_step_results`` key space is a DOTTED SCOPE PATH: a top-level step ``i``
keeps its flat key ``str(i)`` (byte-identical to the pre-``call`` linear format —
a pipeline with no ``call`` records exactly as before), and a ``call`` at index
``i`` records its callee's sub-steps under ``f"{i}.call.{j}"`` while the callee
runs, then records its OWN N2 scalar result at ``str(i)`` once the callee is done.
Nesting composes (``"3.call.1.call.0"``). The persisted ``named_stores`` /
``pipe_data`` / ``step_index`` stay the OUTER scope's throughout a call (a
callee's local stores NEVER leak into the outer snapshot — this is what gives
``pass:[...]`` isolation on the RECOVERY axis, not just the live one); a callee's
local state is reconstructed at resume by RE-WALKING its pipeline definition and
replaying the dotted keys already present — the same one recursive
:meth:`PipelineExecutor._run_scope` used live. Resume detects a completed
sub-step by EXACT key membership (``f"{prefix}.{j}" in completed_step_results``) —
never a string-prefix scan — so a mid-``call`` crash resumes replaying the
finished callee sub-steps EXACTLY-ONCE (a side-effecting sub-step does not
re-fire) and executes only the remainder.

**IS-6 (attached run: live events + step-boundary cancel)**: two optional hooks
let a caller *attach* to a run. ``events`` (an ``EventLog``) receives a
``pipeline_step_started`` / ``pipeline_step_completed`` pair around every
TOP-LEVEL step boundary (a ``call`` is ONE such boundary — its callee sub-steps
do not emit their own ``i/N`` events, keeping the "step i/N" display coherent),
each carrying ``total_steps``. ``cancel_check`` (a ``Callable[[], bool]``) is
polled at each TOP-LEVEL step boundary before the next step starts — never
mid-step — and a True reading raises :class:`PipelineCancelled` from that
boundary, leaving the last recorded snapshot as a consistent resume point. Both
hooks default off.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

from reyn.core.events.pipeline_recovery import latest_pipeline_state, record_pipeline_state
from reyn.core.pipeline.expr import ExprEvalError, ExprParseError, evaluate_expr
from reyn.core.pipeline.registry import PipelineNotFoundError
from reyn.core.pipeline.schema import SchemaRegistry, validate
from reyn.runtime.errors import AgentStepError
from reyn.runtime.session_api import run_agent_step

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.core.events.state_log import StateLog
    from reyn.core.pipeline.registry import PipelineRegistry
    from reyn.runtime.registry import AgentRegistry

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


@dataclass(frozen=True)
class AgentStep:
    """An LLM-driven step (R5): ``prompt`` (a ``TPL`` template — see
    :func:`_interpolate_prompt`) is interpolated against the current context and handed
    to :func:`reyn.runtime.session_api.run_agent_step`, which spawns an ephemeral
    session under ``identity`` (``None`` = inherit the run's ``default_identity``, per
    the design doc's "identity defaults to invoker"), runs one turn, and collects the
    result — capability-narrowed to ``capabilities`` plus a structural delegation deny
    (``run_agent_step``'s own contract; this step introduces no additional narrowing).
    ``schema``, if set, names a ``SchemaRegistry``-registered schema the parsed JSON
    reply must conform to — non-conformance fails the step exactly like a ``tool``
    step's ``verify: schema``."""

    prompt: str
    identity: "str | None" = None
    capabilities: "list[str] | None" = None
    schema: "str | None" = None
    output: "str | None" = None


@dataclass(frozen=True)
class CallStep:
    """A COMPOSITIONAL step (R7 — the first non-linear primitive): synchronously
    run a REGISTERED sub-pipeline and thread ITS final output out as this step's
    N2 return value (Appendix B: ``call = {pipeline: LIT, pass: [NAME*], output:
    NAME}``).

    - ``pipeline`` is a STATIC literal name (Hard rule 2 — never a runtime
      expression), resolved through the run's ``PipelineRegistry`` at execution.
      An absent target fails the step (never a silent no-op).
    - ``pass_`` (wire/DSL key ``pass`` — the Python field can't be the ``pass``
      keyword) is the ONLY channel by which the caller's named stores reach the
      callee: the callee's context is built FRESH from ``{name: caller_ctx[name]
      for name in pass_}``, so the callee structurally cannot see any caller
      store not listed here (Hard rule 8's ``{ctx.X}``-only-for-X-in-``pass``
      isolation). A ``pass_`` name absent from the caller's stores fails the step.
    - the callee's FIRST step receives the caller's pipe-data at the call site
      (Hard rule 5) — bare ``{pipe}`` in the callee's first step is the outer
      pipe-data.
    - the callee's FINAL step output is this ``call`` step's return value (N2),
      re-entering the caller's uniform pipe-data / ``output`` threading unchanged.
    - callee failure fails THIS step (the ``PipelineExecutionError`` propagates
      out of the sub-scope unchanged — Hard rule 5/8).

    Recovery: the callee runs under a dotted scope (``f"{i}.call.{j}"``); its
    completed sub-steps replay EXACTLY-ONCE on resume (see the module docstring)."""

    pipeline: str
    pass_: "list[str]" = field(default_factory=list)
    output: "str | None" = None


@dataclass(frozen=True)
class MatchCase:
    """One ``match`` case target: a REGISTERED sub-pipeline (``pipeline``, a
    STATIC literal — Hard rule 2, never a runtime expression) plus its own
    ``pass_`` caller-store projection, run exactly like a ``call`` step's
    callee (see :class:`CallStep`) once this case's LABEL is selected."""

    pipeline: str
    pass_: "list[str]" = field(default_factory=list)


@dataclass(frozen=True)
class MatchStep:
    """A COMPOSITIONAL step (R7 — ``call``'s runtime-selected sibling):
    evaluate ``on`` (an R1 expression source, resolved exactly like
    ``TransformStep.value``) against the current context to get a VALUE, then
    select the :class:`MatchCase` whose LABEL string-equals that value —
    ``default`` runs when no case LABEL matches, and a step with no matching
    case and no ``default`` fails cleanly (Appendix B: ``match = {on: PATH,
    cases: {LABEL: {pipeline: LIT, pass: [NAME*]}}+, default?: {pipeline: LIT,
    pass: [NAME*]}, output?: NAME}``).

    - Hard rule 2: every case/``default`` target is a STATIC literal pipeline
      name — the runtime VALUE only ever selects a LABEL, never a target.
    - Hard rule 7: ``on`` should reference a schema-declared field; the
      analyzer facet (P4) warns when it does not (see ``analyzer.py``).
    - the SELECTED case runs exactly like ``call``'s callee: its own
      ``pass_`` projects the caller's named stores into an isolated
      sub-context, the callee's first step sees the caller's pipe-data at the
      match site (Hard rule 5), and the callee's FINAL step output is this
      ``match`` step's N2 return value.
    - a non-string ``on`` value is stringified the same way
      ``_interpolate_prompt`` stringifies a non-string interpolation value,
      before the LABEL string-equality comparison — case LABELs are always
      strings (YAML mapping keys), so this is the one coercion needed to let
      e.g. a boolean or numeric ``on`` value select a LABEL at all.

    Recovery: the selected case's callee runs under a dotted scope
    (``f"{i}.match.{j}"``) — IDENTICAL mechanism to ``call`` (single chosen
    sub-scope; the other, unselected cases never execute and so leave no
    dotted keys at all)."""

    on: str
    cases: "dict[str, MatchCase]"
    default: "MatchCase | None" = None
    output: "str | None" = None


Step = Union[TransformStep, ToolStep, AgentStep, CallStep, MatchStep]


@dataclass(frozen=True)
class Pipeline:
    """A sequence of steps.

    ``description`` (IS-5): optional human-readable summary surfaced to the
    LLM by the universal catalog's ``pipeline`` category enumerator
    (``tools/universal_catalog.py:_enumerate_category``), so an agent
    deciding whether to ``run_pipeline`` a registered pipeline sees what it
    does, not just its bare name. Empty string when the registrant omits
    it — the enumerator still lists the pipeline (name is enough to invoke
    it), just with no description text."""

    steps: "list[Step]"
    description: str = ""


@dataclass(frozen=True)
class PipelineResult:
    """The outcome of a completed `run`/`resume`: the final pipe data, every named
    store, and every step's result keyed by ``step_path`` — a DOTTED SCOPE PATH
    (R7). A top-level step ``i`` keys as ``str(i)``; a ``call``'s callee sub-steps
    key as ``f"{i}.call.{j}"`` (nesting composes). ``step_index`` is the OUTER
    scope's next-step cursor (a mid-``call`` snapshot keeps it at the ``call``
    step's index — the callee's progress lives entirely in the dotted keys)."""

    run_id: str
    pipe_data: Any
    named_stores: "dict[str, Any]"
    completed_step_results: "dict[str, Any]"
    step_index: int


class PipelineError(Exception):
    """Base class for pipeline-execution errors."""


class PipelineExecutionError(PipelineError):
    """Raised when a step fails: an expression error, an unresolvable tool result
    against its declared schema, a ``call`` whose target/pass is unresolvable or
    whose callee failed, or an unrecognized step type. The executor never silently
    continues past a failed step."""


class PipelineCancelled(PipelineError):
    """Raised when a cooperative cancel (``cancel_check``) is observed at a step
    BOUNDARY (IS-6). ``step_index`` is the index of the step that would have run
    next — the run stopped cleanly *before* it, so every step ``< step_index``
    is complete and its R4 generation snapshot is on disk. This is NOT a failure:
    the caller (the driver-session) writes a TERMINAL ``cancelled`` marker but
    preserves the R4 snapshots, so the run is abortable-now yet resumable-later
    (R6 "clean abort OR later resume") from ``step_index``. Distinct from
    :class:`PipelineExecutionError` so the driver can tell an intentional stop
    from a genuine step failure."""

    def __init__(self, *, run_id: str, step_index: int) -> None:
        self.run_id = run_id
        self.step_index = step_index
        super().__init__(
            f"pipeline run {run_id!r} cancelled at step boundary {step_index} "
            "(steps before it are complete + snapshotted; resumable from here)"
        )


ToolDispatch = Callable[[str, "dict[str, Any]"], Any]

# An async recorder handed down into every nested scope: it writes a full-state
# R4 generation using a FROZEN outer frame (the top-level ``call`` step's
# step_index / named_stores / pipe_data at the moment the call was entered),
# with only ``completed_step_results`` growing. See ``_run_from``/``_run_scope``.
Recorder = Callable[..., Awaitable[None]]

_PROMPT_REF_RE = re.compile(r"\{([^{}]+)\}")


def _interpolate_prompt(template: str, context: "dict[str, Any]") -> str:
    """Resolve every ``{ctx.dotted.path}`` / ``{pipe}`` reference in an ``AgentStep``
    prompt (R5 spec ``TPL``: "string with {item} {ctx.NAME.field} interpolation
    (values only)") against ``context`` (the SAME ``{"ctx": named_stores, "pipe":
    pipe_data}`` shape a ``transform``/``tool`` step resolves against). Each ``{...}``
    reference is evaluated as a bare R1 ``expr`` source via ``evaluate_expr`` — reusing
    its ``Path`` resolution for the dotted lookup rather than a second path-resolver —
    NOT a full expression (no operators/combinators inside the braces; that keeps this
    pure string interpolation, matching the spec's "values only"). A non-string value
    is stringified for splicing into the prompt text. A missing path or malformed
    reference raises :class:`PipelineExecutionError` naming the failing ``{...}``."""

    def _sub(match: "re.Match[str]") -> str:
        ref = match.group(1).strip()
        try:
            value = evaluate_expr(ref, context)
        except (ExprEvalError, ExprParseError) as exc:
            raise PipelineExecutionError(
                f"prompt template reference {{{ref}}} could not be resolved: {exc}"
            ) from exc
        return value if isinstance(value, str) else str(value)

    return _PROMPT_REF_RE.sub(_sub, template)


def _step_kind(step: Step) -> str:
    """The step's kind string (``transform`` / ``tool`` / ``agent`` / ``call``) for
    the IS-6 live-progress events — the same vocabulary the serde ``kind`` marker
    uses, derived from the dataclass type so it never drifts from the union."""
    return _STEP_KINDS.get(type(step), "unknown")


# ---------------------------------------------------------------------------
# Per-run collaborators + step-invocation bundle (dispatch-table plumbing)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RunDeps:
    """The read-only per-run collaborators, bundled once so a step runner has a
    single uniform argument surface instead of a 9-parameter signature. Threaded
    unchanged into every nested scope (a ``call``'s callee runs under the SAME
    deps — same tool dispatch, same registries)."""

    tool_dispatch: ToolDispatch
    state_log: "StateLog | None"
    run_id: str
    schema_registry: "SchemaRegistry | None"
    registry: "AgentRegistry | None"
    default_identity: "str | None"
    pipeline_registry: "PipelineRegistry | None"
    events: "EventLog | None"
    cancel_check: "Callable[[], bool] | None"


@dataclass(frozen=True)
class _StepInvocation:
    """One dispatch call's inputs. ``step_label`` is the step's DOTTED SCOPE PATH
    (``"3"`` at top level, ``"3.call.0"`` in a callee) — used both for error
    messages and (by a compositional runner) as the prefix for its sub-scope's
    dotted keys. ``record`` is the frozen R4 recorder a compositional runner uses
    for its sub-steps; leaf runners ignore it (their caller records)."""

    executor: "PipelineExecutor"
    step: Step
    context: "dict[str, Any]"
    step_label: str
    deps: _RunDeps
    completed_step_results: "dict[str, Any]"
    record: Recorder


async def _run_transform_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    step: TransformStep = inv.step  # type: ignore[assignment]
    try:
        result = evaluate_expr(step.value, inv.context)
    except ExprEvalError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (transform) failed: {exc}"
        ) from exc
    return result, False, inv.completed_step_results


async def _run_tool_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    step: ToolStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    resolved_args = {
        k: (evaluate_expr(v.src, inv.context) if isinstance(v, ExprRef) else v)
        for k, v in step.args.items()
    }
    raw = deps.tool_dispatch(step.name, resolved_args)
    result = await raw if inspect.isawaitable(raw) else raw
    if step.schema is not None:
        if deps.schema_registry is None:
            raise PipelineExecutionError(
                f"step {inv.step_label} (tool {step.name!r}) declares verify: schema "
                f"{step.schema!r} but no schema_registry was provided"
            )
        validation = validate(result, step.schema, deps.schema_registry)
        if not validation.conforming:
            raise PipelineExecutionError(
                f"step {inv.step_label} (tool {step.name!r}) output failed schema "
                f"{step.schema!r}: {validation.errors}"
            )
    return result, True, inv.completed_step_results


async def _run_agent_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    step: AgentStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    identity = step.identity or deps.default_identity
    if identity is None:
        raise PipelineExecutionError(
            f"step {inv.step_label} (agent) has no identity and no default_identity "
            "was given to run/resume — the design doc's 'identity "
            "defaults to invoker' requires the caller to supply one"
        )
    if deps.registry is None:
        raise PipelineExecutionError(
            f"step {inv.step_label} (agent) requires a registry (AgentRegistry) to "
            "spawn its session, but none was passed to run/resume"
        )
    prompt = _interpolate_prompt(step.prompt, inv.context)
    try:
        result = await run_agent_step(
            deps.registry,
            identity=identity,
            prompt=prompt,
            capabilities=step.capabilities,
            schema=step.schema,
            schema_registry=deps.schema_registry,
        )
    except AgentStepError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (agent) failed: {exc}"
        ) from exc
    return result, True, inv.completed_step_results


async def _run_call_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    """The first COMPOSITIONAL runner (R7): resolve the callee, build its isolated
    ``pass_`` context, and recurse into ``_run_scope`` under a ``f"{label}.call"``
    prefix. Returns the callee's FINAL output (N2), whether any executed sub-step
    was side-effecting (so the outer record durability is right), and the GROWN
    ``completed_step_results`` (with the callee's dotted keys)."""
    step: CallStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    if deps.pipeline_registry is None:
        raise PipelineExecutionError(
            f"step {inv.step_label} (call {step.pipeline!r}) requires a "
            "pipeline_registry to resolve its target, but none was passed to "
            "run/resume"
        )
    try:
        callee = deps.pipeline_registry.get(step.pipeline)
    except PipelineNotFoundError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (call) target pipeline {step.pipeline!r} is "
            "not registered"
        ) from exc

    outer_stores = inv.context["ctx"]
    sub_stores: "dict[str, Any]" = {}
    for name in step.pass_:
        if name not in outer_stores:
            raise PipelineExecutionError(
                f"step {inv.step_label} (call {step.pipeline!r}) pass: names "
                f"{name!r}, which is not in the caller's named stores"
            )
        sub_stores[name] = outer_stores[name]

    final_pipe, _final_stores, completed_step_results, any_durable = await (
        inv.executor._run_scope(
            callee.steps,
            scope_prefix=f"{inv.step_label}.call",
            seed_named_stores=sub_stores,
            # Hard rule 5: the callee's first step gets the caller's pipe-data.
            seed_pipe_data=inv.context["pipe"],
            completed_step_results=inv.completed_step_results,
            deps=deps,
            record=inv.record,
        )
    )
    return final_pipe, any_durable, completed_step_results


def _stringify_match_value(value: Any) -> str:
    """Coerce an evaluated ``on`` value to the string a case LABEL
    string-equality-compares against — same non-string stringification
    :func:`_interpolate_prompt` applies, so ``true``/``42``/etc. select a
    LABEL the same way they'd splice into a prompt."""
    return value if isinstance(value, str) else str(value)


async def _run_match_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    """``call``'s runtime-selected sibling (R7): evaluate ``on`` against the
    context, select the :class:`MatchCase` whose LABEL string-equals the
    result (falling back to ``default``, else failing the step), then run
    that ONE case's callee exactly like :func:`_run_call_step` — same
    ``pass_`` isolation, same ``f"{label}.match"`` dotted sub-scope, same
    Hard-rule-5 pipe-data-at-call-site, same N2 final-output threading."""
    step: MatchStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    try:
        raw_value = evaluate_expr(step.on, inv.context)
    except ExprEvalError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (match) failed evaluating 'on': {exc}"
        ) from exc
    label = _stringify_match_value(raw_value)

    case = step.cases.get(label, step.default)
    if case is None:
        raise PipelineExecutionError(
            f"step {inv.step_label} (match) value {raw_value!r} (label {label!r}) "
            f"matched no case in {sorted(step.cases)!r} and no 'default' was given"
        )

    if deps.pipeline_registry is None:
        raise PipelineExecutionError(
            f"step {inv.step_label} (match label {label!r}) requires a "
            "pipeline_registry to resolve its target, but none was passed to "
            "run/resume"
        )
    try:
        callee = deps.pipeline_registry.get(case.pipeline)
    except PipelineNotFoundError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (match label {label!r}) target pipeline "
            f"{case.pipeline!r} is not registered"
        ) from exc

    outer_stores = inv.context["ctx"]
    sub_stores: "dict[str, Any]" = {}
    for name in case.pass_:
        if name not in outer_stores:
            raise PipelineExecutionError(
                f"step {inv.step_label} (match label {label!r} -> {case.pipeline!r}) "
                f"pass: names {name!r}, which is not in the caller's named stores"
            )
        sub_stores[name] = outer_stores[name]

    final_pipe, _final_stores, completed_step_results, any_durable = await (
        inv.executor._run_scope(
            callee.steps,
            scope_prefix=f"{inv.step_label}.match",
            seed_named_stores=sub_stores,
            # Hard rule 5: the callee's first step gets the caller's pipe-data.
            seed_pipe_data=inv.context["pipe"],
            completed_step_results=inv.completed_step_results,
            deps=deps,
            record=inv.record,
        )
    )
    return final_pipe, any_durable, completed_step_results


# Dispatch table: step dataclass type -> its runner. A future primitive ADDS an
# entry here (+ serde/parser/analyzer) rather than editing a shared elif chain.
STEP_DISPATCH: "dict[type, Callable[[_StepInvocation], Awaitable[tuple[Any, bool, dict[str, Any]]]]]" = {
    TransformStep: _run_transform_step,
    ToolStep: _run_tool_step,
    AgentStep: _run_agent_step,
    CallStep: _run_call_step,
    MatchStep: _run_match_step,
}

# Type -> kind-string, the inverse vocabulary the serde ``kind`` marker uses.
_STEP_KINDS: "dict[type, str]" = {
    TransformStep: "transform",
    ToolStep: "tool",
    AgentStep: "agent",
    CallStep: "call",
    MatchStep: "match",
}


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class PipelineExecutor:
    """Runs a :class:`Pipeline`, threading pipe data + named stores (R3),
    recording a dotted-path step-boundary recovery generation after each step
    (R4/R7), and executing compositional steps (``call``) via a recursive
    sub-scope (:meth:`_run_scope`)."""

    async def run(
        self,
        pipeline: Pipeline,
        initial_context: "dict[str, Any] | None",
        *,
        tool_dispatch: ToolDispatch,
        state_log: "StateLog | None",
        run_id: str,
        schema_registry: "SchemaRegistry | None" = None,
        registry: "AgentRegistry | None" = None,
        default_identity: "str | None" = None,
        pipeline_registry: "PipelineRegistry | None" = None,
        events: "EventLog | None" = None,
        cancel_check: "Callable[[], bool] | None" = None,
    ) -> PipelineResult:
        """Run `pipeline` from the first step. `initial_context` seeds the named
        stores (``ctx.*``) available to the first step; there is no incoming pipe
        data (``pipe`` resolves to ``None`` until the first step produces one).
        `registry` / `default_identity` are required only if `pipeline` contains an
        `AgentStep`; `pipeline_registry` (a `PipelineRegistry`) is required only if
        it contains a `CallStep` — a pipeline with neither never touches either, so
        existing transform/tool-only callers are unaffected.

        `events` / `cancel_check` (IS-6) behave as documented on the module: an
        attached caller renders live progress from `events`, and a True
        `cancel_check` at a step boundary raises :class:`PipelineCancelled`."""
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
            registry=registry,
            default_identity=default_identity,
            pipeline_registry=pipeline_registry,
            events=events,
            cancel_check=cancel_check,
        )

    async def resume(
        self,
        run_id: str,
        *,
        pipeline: Pipeline,
        tool_dispatch: ToolDispatch,
        state_log: "StateLog",
        schema_registry: "SchemaRegistry | None" = None,
        registry: "AgentRegistry | None" = None,
        default_identity: "str | None" = None,
        pipeline_registry: "PipelineRegistry | None" = None,
        events: "EventLog | None" = None,
        cancel_check: "Callable[[], bool] | None" = None,
    ) -> PipelineResult:
        """Resume `run_id`: load the latest recorded generation (R4) and replay every
        step already in ``completed_step_results`` (no re-execution — exactly-once).
        For a mid-``call`` crash this means resuming at the ``call`` step's index and
        replaying the finished callee sub-steps from their dotted keys (the callee's
        side effects do not re-fire), executing only the remainder. With no snapshot
        at all, resume == run from scratch.

        `pipeline_registry` (R7) is required only if the resumed run re-enters a
        `CallStep` — the callee is re-resolved by name and re-walked to reconstruct
        its local state from the dotted keys. `events` / `cancel_check` behave as in
        :meth:`run`."""
        snapshot = latest_pipeline_state(run_id, state_log)
        if snapshot is None:
            return await self.run(
                pipeline,
                None,
                tool_dispatch=tool_dispatch,
                state_log=state_log,
                run_id=run_id,
                schema_registry=schema_registry,
                registry=registry,
                default_identity=default_identity,
                pipeline_registry=pipeline_registry,
                events=events,
                cancel_check=cancel_check,
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
            registry=registry,
            default_identity=default_identity,
            pipeline_registry=pipeline_registry,
            events=events,
            cancel_check=cancel_check,
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
        registry: "AgentRegistry | None" = None,
        default_identity: "str | None" = None,
        pipeline_registry: "PipelineRegistry | None" = None,
        events: "EventLog | None" = None,
        cancel_check: "Callable[[], bool] | None" = None,
    ) -> PipelineResult:
        deps = _RunDeps(
            tool_dispatch=tool_dispatch,
            state_log=state_log,
            run_id=run_id,
            schema_registry=schema_registry,
            registry=registry,
            default_identity=default_identity,
            pipeline_registry=pipeline_registry,
            events=events,
            cancel_check=cancel_check,
        )
        steps = pipeline.steps
        total_steps = len(steps)
        for i in range(start_index, total_steps):
            # IS-6 cancel checkpoint: poll at the TOP-LEVEL step BOUNDARY, before
            # this step starts. Every step < i is complete + snapshotted, so the
            # last record_pipeline_state is a consistent resume point.
            if cancel_check is not None and cancel_check():
                raise PipelineCancelled(run_id=run_id, step_index=i)
            step = steps[i]
            kind = _step_kind(step)
            if events is not None:
                events.emit(
                    "pipeline_step_started",
                    run_id=run_id, step_index=i, step_kind=kind, total_steps=total_steps,
                )
            context = {"ctx": named_stores, "pipe": pipe_data}

            # A frozen R4 recorder for any nested scope this step opens (a call):
            # every generation recorded WHILE inside the call carries THIS outer
            # frame (step_index=i, the pre-threading outer stores/pipe) with only
            # completed_step_results growing — the callee's local stores never
            # persist, giving pass:[...] isolation on the recovery axis. Leaf
            # steps never invoke it (their record happens below, post-threading).
            async def _record(
                *, completed_step_results: "dict[str, Any]", durable: bool,
                _i: int = i, _named: "dict[str, Any]" = named_stores, _pipe: Any = pipe_data,
            ) -> None:
                await record_pipeline_state(
                    state_log, run_id,
                    {
                        "run_id": run_id, "step_index": _i,
                        "named_stores": _named, "pipe_data": _pipe,
                        "completed_step_results": completed_step_results,
                    },
                    durable=durable,
                )

            runner = STEP_DISPATCH.get(type(step))
            if runner is None:  # pragma: no cover - Step is a closed union
                raise PipelineExecutionError(f"unknown step type: {step!r}")
            inv = _StepInvocation(
                executor=self, step=step, context=context, step_label=str(i),
                deps=deps, completed_step_results=completed_step_results, record=_record,
            )
            result, durable, completed_step_results = await runner(inv)

            pipe_data = result
            step_index = i + 1
            completed_step_results = {**completed_step_results, str(i): result}
            if step.output:
                named_stores = {**named_stores, step.output: result}

            await record_pipeline_state(
                state_log, run_id,
                {
                    "run_id": run_id, "step_index": step_index,
                    "named_stores": named_stores, "pipe_data": pipe_data,
                    "completed_step_results": completed_step_results,
                },
                durable=durable,
            )
            if events is not None:
                events.emit(
                    "pipeline_step_completed",
                    run_id=run_id, step_index=step_index, step_kind=kind,
                    total_steps=total_steps,
                )

        return PipelineResult(
            run_id=run_id,
            pipe_data=pipe_data,
            named_stores=named_stores,
            completed_step_results=completed_step_results,
            step_index=total_steps,
        )

    async def _run_scope(
        self,
        steps: "list[Step]",
        *,
        scope_prefix: str,
        seed_named_stores: "dict[str, Any]",
        seed_pipe_data: Any,
        completed_step_results: "dict[str, Any]",
        deps: _RunDeps,
        record: Recorder,
    ) -> "tuple[Any, dict[str, Any], dict[str, Any], bool]":
        """Run a SUB-scope's ``steps`` under ``scope_prefix`` (e.g. ``"3.call"``),
        threading its OWN local ``named_stores`` / ``pipe_data`` seeded from the
        call site. Each sub-step keys as ``f"{scope_prefix}.{j}"``: if that key is
        ALREADY in ``completed_step_results`` it REPLAYS (no execution, no record —
        the exactly-once contract for a mid-scope crash), else it executes via the
        same :data:`STEP_DISPATCH` and records through the frozen ``record`` closure.
        A nested ``call`` sub-step recurses here again (``"3.call.1.call"``), sharing
        the SAME ``record`` (the frozen outer frame propagates all the way down).
        Returns ``(final_pipe_data, final_local_named_stores, grown_completed, any_durable)``.

        Note: no ``cancel_check`` / ``events`` here — a ``call`` is ONE top-level
        boundary; its sub-steps neither emit ``i/N`` progress nor add cancel points,
        keeping the attached-caller view coherent. The callee runs to completion or
        failure once entered; cancel takes effect at the next TOP-LEVEL boundary."""
        named_stores = seed_named_stores
        pipe_data = seed_pipe_data
        any_durable = False
        for j, step in enumerate(steps):
            key = f"{scope_prefix}.{j}"
            if key in completed_step_results:
                # REPLAY exactly-once: the finished sub-step's result comes from the
                # snapshot; it is NOT re-executed (no side effect refires).
                result = completed_step_results[key]
            else:
                context = {"ctx": named_stores, "pipe": pipe_data}
                runner = STEP_DISPATCH.get(type(step))
                if runner is None:  # pragma: no cover - Step is a closed union
                    raise PipelineExecutionError(f"unknown step type: {step!r}")
                inv = _StepInvocation(
                    executor=self, step=step, context=context, step_label=key,
                    deps=deps, completed_step_results=completed_step_results,
                    record=record,
                )
                result, durable, completed_step_results = await runner(inv)
                any_durable = any_durable or durable
                completed_step_results = {**completed_step_results, key: result}
                await record(completed_step_results=completed_step_results, durable=durable)
            pipe_data = result
            if step.output:
                named_stores = {**named_stores, step.output: result}
        return pipe_data, named_stores, completed_step_results, any_durable


__all__ = [
    "ExprRef",
    "TransformStep",
    "ToolStep",
    "AgentStep",
    "CallStep",
    "MatchCase",
    "MatchStep",
    "Step",
    "Pipeline",
    "PipelineResult",
    "PipelineError",
    "PipelineExecutionError",
    "PipelineCancelled",
    "PipelineExecutor",
    "STEP_DISPATCH",
]
