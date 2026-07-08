"""Pipeline executor — R3 pipe-data threading + R4 recovery + the non-linear
compositional foundation (``_run_scope`` + dotted-path recovery + ``call`` +
``fold``).

The executor for ``docs/proposals/reyn-pipeline-v0.9-design-resolutions.md``: a
sequence of ``transform`` / ``tool`` (``shell`` is just a ``ToolStep(name="shell",
...)``) / ``agent`` (R5) steps, plus three COMPOSITIONAL primitives: ``call``
(R7, a STATIC callee), ``match`` (``call``'s runtime-selected sibling — the
``on`` VALUE picks a LABEL, never a target; see :class:`MatchStep`), and
``fold`` (the sequential accumulator, Appendix B), ``for_each`` (the
CONCURRENT fan-out — ``fold``'s parallel, isolated-sub-scope sibling; see
:class:`ForEachStep` and :func:`_run_for_each_step`), and ``parallel`` (the
LAST non-linear primitive — ``for_each``'s HETEROGENEOUS NAMED-branch
sibling, built additively on the SAME fan-out substrate; see
:class:`ParallelStep` and :func:`_run_parallel_step`). All Appendix-B
non-linear primitives are now implemented; ``refine`` (a pipeline-level
construct, not a step kind) stays out of scope.

**Fan-out recovery commit unit = the ITEM (READ THIS before touching
``for_each`` recovery).** ``for_each`` runs ``do`` over each list item as an
ISOLATED concurrent sub-scope (Hard rule 6: read-only ctx, no sibling comm), a
coordinator recording each item's result AS IT LANDS via ``asyncio.as_completed``
(never bare ``gather``). The recovery commit unit is the ITEM: a LANDED item is
exactly-once (its key is durable before the next boundary; resume replays it,
never re-runs it). A single-step ``do`` (the common case) therefore has NO
recovery gap — it is exactly-once identical to a durable step. The ONE gap: a
COMPOSITIONAL ``do`` (``call``/``match``/``fold``) that is IN-FLIGHT at a crash
re-runs ATOMICALLY on resume (its item key was never recorded), so its already-
completed INTERNAL side effects may re-fire — because item tasks record through a
NO-OP recorder (only the single coordinator coroutine calls the real ``record``,
serialized by construction, avoiding the read-modify-write race concurrent
per-task recording would cause). Per-internal-sub-step durability for
compositional fan-out items (a lock-guarded serialized-recorder path) is a
tracked follow-up, not a silent gap.

**Dispatch tables (R7 — the parallel-primitive seam).** Step execution is a
``dict``-dispatch keyed by the step's dataclass type
(:data:`STEP_DISPATCH`) rather than an ``isinstance`` chain — so a future
primitive ADDS one registry entry (here, plus a serde encoder/decoder, a parser,
and an analyzer facet) instead of editing a shared ``elif`` block. Every runner
has the uniform signature ``(_StepInvocation) -> (result, durable,
completed_step_results)``: leaf runners (``transform``/``tool``/``agent``) return
``completed_step_results`` unchanged and never record; a COMPOSITIONAL runner
(``call``, ``fold``) grows ``completed_step_results`` with its sub-scope's
dotted keys and records each sub-step through the frozen recovery closure it is
handed. ``call`` recurses into :meth:`PipelineExecutor._run_scope` (a LIST of
distinct sub-steps sharing one evolving local scope); ``fold`` instead loops
its OWN dispatch calls directly (see :func:`_run_fold_step`) because each
iteration re-invokes the SAME ``do`` step under a fresh ``{item, acc}``
binding — a shape ``_run_scope``'s "walk this fixed step list once" model does
not fit.

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
a pipeline with no ``call``/``fold`` records exactly as before), and a ``call``
at index ``i`` records its callee's sub-steps under ``f"{i}.call.{j}"`` while
the callee runs, then records its OWN N2 scalar result at ``str(i)`` once the
callee is done. A ``fold`` at index ``i`` records each iteration's ``do``
result under ``f"{i}.fold.{k}"`` (``k`` = iteration index, NOT a nested scope —
each key is ``do``'s own flat result, since every iteration runs the SAME
``do`` step once) as it walks the list in order, then records its own final
``acc`` at ``str(i)``. Nesting composes (``"3.call.1.call.0"``,
``"3.fold.2.call.0"`` for a ``call`` used as a fold's ``do``). The persisted
``named_stores`` /
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

import asyncio
import inspect
import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

from reyn.core.events.pipeline_recovery import latest_pipeline_state, record_pipeline_state
from reyn.core.offload.canonical import (
    canonical_to_ctx_fields,
    to_canonical,
    unwrap_dispatch_envelope,
)
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
    N2 return value (Appendix B, redesigned: ``call = {pipeline: LIT, pass:
    {NAME: EXPR}*, output: NAME}``).

    - ``pipeline`` is a STATIC literal name (Hard rule 2 — never a runtime
      expression), resolved through the run's ``PipelineRegistry`` at execution.
      An absent target fails the step (never a silent no-op).
    - ``pass_`` (wire/DSL key ``pass`` — the Python field can't be the ``pass``
      keyword) is the ONLY channel by which the caller's scope reaches the
      callee: it is a ``list[(callee_name, expr_source)]`` NAME -> R1-EXPRESSION
      mapping (normalized from the DSL's flat ``{NAME: EXPR}`` mapping at parse
      time — no bare-NAME shorthand, every entry states its own expression
      explicitly). Each ``expr_source`` is evaluated via :func:`evaluate_expr`
      against the CALLER's full current context — ``ctx``/``pipe``/``item``/
      ``acc``, whatever is in scope (the SAME context ``transform.value``
      evaluates against, so ``pass: {current: item}`` reaches a
      ``for_each``/``fold`` loop variable exactly like an ``agent`` step's
      ``{item}`` prompt already could) — and the result is bound to
      ``callee_name`` in the callee's FRESH, isolated ``ctx``: the callee
      structurally cannot see anything not listed here (Hard rule 8's
      ``{ctx.X}``-only-for-X-in-``pass`` isolation). A failing expression
      (missing path, wrong type, ...) fails the step, naming the failing
      entry in the error.
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
    pass_: "list[tuple[str, str]]" = field(default_factory=list)
    output: "str | None" = None


@dataclass(frozen=True)
class MatchCase:
    """One ``match`` case target: a REGISTERED sub-pipeline (``pipeline``, a
    STATIC literal — Hard rule 2, never a runtime expression) plus its own
    ``pass_`` NAME -> R1-EXPRESSION mapping, run exactly like a ``call`` step's
    callee (see :class:`CallStep`) once this case's LABEL is selected."""

    pipeline: str
    pass_: "list[tuple[str, str]]" = field(default_factory=list)


@dataclass(frozen=True)
class MatchStep:
    """A COMPOSITIONAL step (R7 — ``call``'s runtime-selected sibling):
    evaluate ``on`` (an R1 expression source, resolved exactly like
    ``TransformStep.value``) against the current context to get a VALUE, then
    select the :class:`MatchCase` whose LABEL string-equals that value —
    ``default`` runs when no case LABEL matches, and a step with no matching
    case and no ``default`` fails cleanly (Appendix B, redesigned: ``match =
    {on: PATH, cases: {LABEL: {pipeline: LIT, pass: {NAME: EXPR}*}}+,
    default?: {pipeline: LIT, pass: {NAME: EXPR}*}, output?: NAME}``).

    - Hard rule 2: every case/``default`` target is a STATIC literal pipeline
      name — the runtime VALUE only ever selects a LABEL, never a target.
    - Hard rule 7: ``on`` should reference a schema-declared field; the
      analyzer facet (P4) warns when it does not (see ``analyzer.py``).
    - the SELECTED case runs exactly like ``call``'s callee: its own
      ``pass_`` NAME -> R1-EXPRESSION mapping projects values evaluated
      against the caller's full context into an isolated sub-context, the
      callee's first step sees the caller's pipe-data at the match site
      (Hard rule 5), and the callee's FINAL step output is this ``match``
      step's N2 return value.
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


@dataclass(frozen=True)
class FoldStep:
    """The second COMPOSITIONAL primitive: a SEQUENTIAL walk over a list,
    threading an accumulator (Appendix B: ``fold = {over?:PATH | items?:[LIT*]
    init:EXPR do:Step output:NAME max_items?}``). Unlike ``call``, ``do`` is a
    single :data:`Step` re-invoked once PER ITEM, not a fixed list of distinct
    sub-steps — see :func:`_run_fold_step` for the loop.

    - List source (mutually exclusive; the parser rejects both together):
      ``over`` (an R1 expression source, evaluated against ``{ctx, pipe}``
      exactly like ``transform.value`` — e.g. ``"ctx.items"``) | ``items``
      (a static Python literal list, stored as-is — Appendix B's ``[LIT*]``)
      | neither, which falls back to the incoming PIPE DATA (the same
      "over (ctx path) | items (static) | pipe data" convention Appendix B
      states for ``for_each``).
    - ``init`` is an R1 expression evaluated ONCE, before the first iteration,
      against the fold step's own ``{ctx, pipe}`` — its result seeds ``acc``.
    - ``do`` runs once per list item, in order; item ``k``'s context is
      ``{"ctx": <a LOCAL named-store copy, seeded from the outer ctx and
      grown by ``do.output`` across iterations>, "pipe": <this fold step's
      OWN incoming pipe-data, held CONSTANT across every iteration — the
      per-iteration analog of Hard rule 5's "callee's first step sees the
      caller's pipe-data at the call site">, "item": <the k-th list element>,
      "acc": <the running accumulator>}``. ``item``/``acc`` are bare
      top-level context names, so a transform ``do`` reads them as
      ``acc + item`` and an agent ``do`` prompt interpolates them as
      ``{item}``/``{acc}`` — the SAME ``Path``/``{...}`` resolution
      ``ctx.NAME``/``pipe`` already use, just against two more top-level keys.
      A ``do: {call: ...}`` / ``do: {match: ...}`` reaches the SAME two
      bindings via an explicit ``pass:`` entry — e.g. ``pass: {current:
      item}`` / ``pass: {running: acc}`` — evaluated against this ``do``'s
      full context the same way ``transform.value`` would be, so ``item``/
      ``acc`` are forwardable into a sub-pipeline (under whatever name the
      entry chooses), not just readable from an ``agent`` prompt in the same
      ``do``.
    - ``do``'s RETURN VALUE becomes the next ``acc`` (never its ``pipe``/
      ``ctx`` output — those are local bookkeeping only); the FINAL ``acc``
      (after the last item, or ``init`` unchanged for an empty list) is this
      step's N2 return value and — per R3 — its named ``output``.
    - No ``collect`` (unlike ``for_each``): a fold's whole point is that each
      item depends on the accumulated result of the ones before it, so there
      is nothing to collect independently.
    - Item failure (``do`` raising, after any retry the leaf step itself
      implements) fails the WHOLE fold — the exception propagates unchanged,
      mirroring ``call``'s "callee failure fails this step".
    - ``max_items`` (Hard rule 9 — always finite): caps the walk to the first
      ``max_items`` elements of the resolved list; a longer source is
      silently truncated, never an error (the cap is a bound, not a
      length-equality assertion).

    Recovery: iteration ``k`` records at the FLAT dotted key
    ``f"{label}.fold.{k}"`` (not a nested scope prefix — every iteration runs
    the exact same ``do``, so there is no sub-scope of distinct steps to
    prefix, unlike ``call``). On resume, a key already present in
    ``completed_step_results`` REPLAYS: ``acc`` is read back from the stored
    result and ``do`` is NOT re-executed (its side effect does not re-fire) —
    walking the dotted keys in order rebuilds ``acc`` exactly as the live run
    computed it, then execution resumes at the first absent iteration."""

    init: str
    do: "Step"
    output: str
    over: "str | None" = None
    items: "list[Any] | None" = None
    max_items: "int | None" = None


@dataclass(frozen=True)
class ForEachStep:
    """The CONCURRENT fan-out primitive (Appendix B: ``for_each = {over?:PATH |
    items?:[LIT*] max_parallel? on_error:continue|abort|retry(n) do:Step
    collect:Step}``) — ``fold``'s parallel, isolated sibling. Where ``fold``
    walks a list SEQUENTIALLY threading an accumulator, ``for_each`` runs ``do``
    over each item as an ISOLATED concurrent sub-scope (Hard rule 6: each item
    gets its OWN copied ``ctx``, no sibling communication), then runs ``collect``
    ONCE over the ordered results list — ``collect``'s result is this step's N2
    return value / pipe-data.

    - List source (mutually exclusive; the parser rejects both together):
      ``over`` (an R1 expression source, evaluated against ``{ctx, pipe}`` like
      ``transform.value``) | ``items`` (a static Python literal list) | neither,
      falling back to the incoming PIPE DATA (the same convention ``fold`` uses).
    - ``do`` runs once per item; item ``item_idx``'s context is ``{"ctx": <a
      COPY of the outer named stores — isolation>, "pipe": <this for_each step's
      OWN incoming pipe-data, held constant across every item, the per-item analog
      of Hard rule 5's "callee's first step sees the caller's pipe-data at the
      call site">, "item": <the item_idx-th element>}``. There is NO ``acc``
      (that is ``fold``-only) and NO sibling visibility — an item cannot see any
      other item's result (writes happen only in ``collect``, Hard rule 6). A
      ``do: {call: ...}`` / ``do: {match: ...}`` reaches ``item`` via an
      explicit ``pass:`` entry, e.g. ``pass: {current: item}`` (evaluated
      against this ``do``'s full context the same way ``transform.value``
      would be), forwarding the loop item into a sub-pipeline the same way an
      ``agent`` step's ``{item}`` prompt already could.
    - ``max_parallel`` (S5 guard a — the Semaphore cap): live concurrency is
      gated to ``max_parallel`` items at once; omitted, it defaults to a
      conservative finite value (``min(len(items), _DEFAULT_MAX_PARALLEL)``) —
      NEVER unbounded-by-omission.
    - ``on_error`` is REQUIRED (Appendix B gives it no ``?`` — a fan-out author
      MUST state the completeness policy explicitly, unlike ``parallel`` whose
      ``on_error?`` defaults to ``abort``). One of: ``continue`` (a failed item
      is DROPPED from the results list — recorded with a kind-marker so resume
      does not re-run it, see :func:`_run_for_each_step`); ``abort`` (a failed
      item cancels the still-pending items and fails the whole step); or
      ``retry(n)`` (re-run the failed item's ``do`` up to ``n`` more times, then
      fall back to ``abort`` if still failing — only ``continue`` ever silently
      drops).
    - ``collect`` runs ONCE, sequentially, AFTER the fan-out, over the ordered
      list of surviving item results (dropped items filtered out via the ONE
      shared filter used identically live and on resume-replay).

    Recovery (see the module docstring's "commit unit = the ITEM"): item
    ``item_idx``'s ``do`` result records at the FLAT dotted key
    ``f"{label}.for_each.{item_idx}"`` (a compositional ``do`` adds its own
    deeper levels naturally, e.g. ``f"{label}.for_each.{item_idx}.call.{j}"`` —
    ``for_each`` invents no extra index level of its own, same as ``fold``).
    ``collect`` records at ``f"{label}.for_each.collect"``. A landed item is
    exactly-once; a dropped item stays dropped forever (its kind-marker key is
    present, so resume never re-runs it); resume re-runs ONLY the items whose key
    is ABSENT, then runs ``collect`` iff its key is absent."""

    do: "Step"
    collect: "Step"
    on_error: str
    over: "str | None" = None
    items: "list[Any] | None" = None
    max_parallel: "int | None" = None
    # R3's uniform output rule: ``collect``'s result is this step's N2 return /
    # pipe-data, and is ALSO written to a named store iff ``output`` is declared
    # (optional, like ``call``/``match`` — Appendix B's compact grammar omits it,
    # but the uniform rule applies to every step). The executor's outer loop reads
    # ``step.output`` on every Step, so this field must exist on the union member.
    output: "str | None" = None


@dataclass(frozen=True)
class ParallelStep:
    """The LAST non-linear primitive (Appendix B: ``parallel = {on_error?:abort|
    continue|retry(n), branches:{NAME:Step}+, collect:Step}``) — ``for_each``'s
    HETEROGENEOUS NAMED-branch sibling. Where ``for_each`` fans a SINGLE ``do``
    step out over a runtime-sized list of items, ``parallel`` fans a STATIC,
    FINITE dict of DISTINCT named branches out concurrently, then runs
    ``collect`` ONCE over the NAMED MAP ``{branch_name: result}`` (not an
    ordered list — Appendix B's compact grammar, N2).

    - ``branches`` is a non-empty ``{NAME: Step}`` mapping; each branch is its
      OWN heterogeneous Step (a different kind/config per NAME, unlike
      ``for_each``'s one ``do`` re-invoked per item). Every branch runs
      concurrently — the branch set is statically finite, so there is NO
      ``max_parallel`` field (unlike ``for_each``): the branch COUNT is the
      concurrency bound.
    - ``on_error`` is OPTIONAL (Appendix B: ``on_error?:`` — UNLIKE
      ``for_each`` where it is REQUIRED), defaulting to ``abort`` when
      omitted. Same three values, same semantics as ``for_each``: ``continue``
      (a failed branch is DROPPED — recorded with the shared
      ``__fan_out_dropped__`` kind-marker so resume never re-runs it —
      ``collect`` MUST handle the absent branch); ``abort`` (a branch failure
      cancels the still-pending branches and fails the whole step);
      ``retry(n)`` (re-run the failed branch's Step up to ``n`` more times,
      falling back to ``abort`` if still failing — only ``continue`` ever
      silently drops).
    - each branch's context is ``{"ctx": <a COPY of the outer named stores —
      isolation, Hard rule 6>, "pipe": <this parallel step's OWN incoming
      pipe-data, held constant across every branch — the per-branch analog of
      Hard rule 5>}`` — no ``item``/``acc`` (those are ``for_each``/
      ``fold``-only) and no sibling visibility (a branch cannot see any other
      branch's result; writes happen only in ``collect``).
    - ``collect`` runs ONCE, over the NAMED MAP of surviving branch results
      (dropped branches filtered out by the SAME shared
      :func:`_is_dropped_marker` filter ``for_each`` uses, adapted to a dict)
      — its result is this step's N2 return value / pipe-data.

    Recovery: reuses the EXACT fan-out substrate ``for_each`` established (see
    the module docstring's "commit unit = the ITEM") — the commit unit here is
    the BRANCH (keyed by NAME, not index): a branch's result records at the
    FLAT dotted key ``f"{label}.parallel.{branch_name}"``; ``collect`` records
    at ``f"{label}.parallel.collect"``. A landed branch is exactly-once; a
    dropped branch stays dropped forever (its marker key is present, so
    resume never re-runs it); resume re-runs ONLY the branches whose key is
    ABSENT, then runs ``collect`` iff its key is absent — see
    :func:`_run_parallel_step`."""

    branches: "dict[str, Step]"
    collect: "Step"
    on_error: str = "abort"
    output: "str | None" = None


Step = Union[
    TransformStep, ToolStep, AgentStep, CallStep, MatchStep, FoldStep, ForEachStep,
    ParallelStep,
]


@dataclass(frozen=True)
class Pipeline:
    """A sequence of steps.

    ``description`` (IS-5): optional human-readable summary surfaced to the
    LLM by the universal catalog's ``pipeline`` category enumerator
    (``tools/universal_catalog.py:_enumerate_category``), so an agent
    deciding whether to ``run_pipeline`` a registered pipeline sees what it
    does, not just its bare name. Empty string when the registrant omits
    it — the enumerator still lists the pipeline (name is enough to invoke
    it), just with no description text.

    ``name`` (#2575): the declared ``pipeline:`` name from the DSL document.
    The DSL parser populates it from the ``pipeline:`` key; a hand-built
    ``Pipeline`` (tests, inline construction) may leave it ``""``. It is the
    AUTHORITATIVE key the disk loader registers under and the identity a
    ``call``/``match`` step's ``pipeline: LIT`` resolves against. Additive
    (default ``""``) so it travels with the pipeline through work-order /
    invocation.json persistence + recovery for free; an on-disk
    invocation.json written before this field existed simply decodes to
    ``""`` (default-tolerant round-trip)."""

    steps: "list[Step]"
    description: str = ""
    name: str = ""


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

# S5 spawn-bound conservative defaults (used when run/resume is called without
# explicit operator caps — never unbounded-by-omission). ``0`` on the effective
# cap means "unlimited" (operator opt-out), mirroring ``SpawnConfig``'s convention.
_DEFAULT_MAX_PARALLEL = 8
_DEFAULT_MAX_FAN_OUT_DEPTH = 5
_DEFAULT_MAX_PIPELINE_SPAWNS = 100

# The kind-marker an ``on_error:continue`` DROPPED fan-out item records under its
# item key — NOT absent (resume would re-run an absent key) and NOT bare None
# (a legit ``do`` result). Recognised on read by EXACT key-set equality (see
# :func:`_is_dropped_marker`), the same collision-narrowing discipline
# ``serde``'s ``__exprref__`` marker uses.
_FAN_OUT_DROPPED_KEY = "__fan_out_dropped__"

_ON_ERROR_RETRY_RE = re.compile(r"^retry\((?P<n>\d+)\)$")


@dataclass(frozen=True)
class _OnError:
    """The normalized ``for_each.on_error`` policy. ``kind`` is ``"continue"`` |
    ``"abort"`` | ``"retry"``; ``retries`` is the extra-attempt count (>0 only
    for ``retry``). Parsed from the DSL string once (see :func:`_parse_on_error`)
    so the coordinator branches on an enum, not a re-parsed string."""

    kind: str
    retries: int = 0


def _parse_on_error(raw: str) -> "_OnError":
    """Normalize a ``for_each.on_error`` DSL string (``"continue"`` / ``"abort"``
    / ``"retry(N)"``) to an :class:`_OnError`. Raises :class:`PipelineExecutionError`
    on an unrecognized value (the parser validates this at parse time too — this
    is the runtime-side defensive parse for a hand-built / serde-round-tripped
    ``ForEachStep``)."""
    if raw in ("continue", "abort"):
        return _OnError(kind=raw)
    m = _ON_ERROR_RETRY_RE.match(raw)
    if m is not None:
        return _OnError(kind="retry", retries=int(m.group("n")))
    raise PipelineExecutionError(
        f"for_each on_error {raw!r} is not one of continue|abort|retry(n)"
    )


def _is_dropped_marker(value: Any) -> bool:
    """True iff ``value`` is a fan-out DROPPED kind-marker (exact 2-key set
    ``{_FAN_OUT_DROPPED_KEY, 'error'}``) — the ONE shared predicate the collect
    filter uses identically live AND on resume-replay so a dropped item is
    excluded from ``collect``'s input the same way both times."""
    return isinstance(value, dict) and set(value) == {_FAN_OUT_DROPPED_KEY, "error"}


class SpawnBudget:
    """S5 guard (c) — a per-RUN monotonic session-spawn counter. Constructed ONCE
    per top-level ``run``/``resume`` and shared BY REFERENCE across every nested
    scope (the one deliberate piece of run-scoped mutable state in this otherwise
    immutable-threading executor). Every ``AgentStep`` — top-level or reached from
    a ``call``/``match``/``fold``/``for_each`` — funnels through
    :func:`_run_agent_step`, which calls :meth:`consume` before spawning, so the
    cap is COMPLETE-BY-CONSTRUCTION across the whole surface, not just fan-out.

    This closes the CRITICAL GAP the fan-out design found: pipeline agent-steps
    reach ``spawn_ephemeral_session`` with NO parent/lineage, so the registry's
    ``SpawnConfig`` (max_depth/max_children over the spawn-lineage) does NOT cover
    them — this counter is the ONLY enforcement. ``cap == 0`` = unlimited
    (operator opt-out), mirroring ``SpawnConfig``. Monotonic counter vs a static
    operator-set finite cap: no LLM-writable path raises it."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._spent = 0

    def consume(self, *, label: str) -> None:
        """Charge one spawn; raise :class:`PipelineExecutionError` (fail the step,
        never silent) if the cap is already reached."""
        if self._cap and self._spent >= self._cap:
            raise PipelineExecutionError(
                f"step {label} (agent) would exceed the per-run pipeline spawn cap "
                f"({self._cap}): {self._spent} ephemeral session(s) already spawned "
                "this run — a for_each fanned out more agent-steps than the operator "
                "bound allows (S5 spawn guard)"
            )
        self._spent += 1

    @property
    def spent(self) -> int:
        """The number of spawns charged so far this run (test/observability read)."""
        return self._spent


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
    # S5 fan-out spawn bounds. ``fan_out_depth`` is a PER-BRANCH frozen value:
    # entering a ``for_each`` threads a ``replace(deps, fan_out_depth=+1)`` copy
    # into its item/collect sub-scopes, so a nested for_each sees a deeper value
    # (guard b). ``max_fan_out_depth`` is the run-constant cap (0 = unlimited).
    # ``spawn_budget`` is the per-RUN mutable counter (guard c), shared by
    # reference through every ``replace`` (so a bumped ``fan_out_depth`` copy
    # still charges the SAME budget). Defaults keep pre-for_each callers (and any
    # ``_RunDeps`` built without these) safe: depth 0, a fresh unlimited budget.
    fan_out_depth: int = 0
    max_fan_out_depth: int = _DEFAULT_MAX_FAN_OUT_DEPTH
    spawn_budget: SpawnBudget = field(default_factory=lambda: SpawnBudget(0))


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
    # #2425 PR-2: ctx exposes the same text/structured shape chat gets, uniformly across every op
    # kind — shape-only (to_canonical), NEVER offloaded/size-gated (owner ruling: ctx/pipe data
    # retains full values for downstream programmatic step processing). Schema validation above runs
    # against the RAW dispatch result, unchanged.
    if isinstance(result, dict):
        canonical = to_canonical(unwrap_dispatch_envelope(result))
        ctx_result: Any = canonical_to_ctx_fields(canonical)
    elif isinstance(result, str):
        ctx_result = {"text": result}
    else:
        # A non-dict, non-str result (int/float/bool/list/None/...) is not textual —
        # it is structured data, not a stringified lossy blob (full-value retention).
        ctx_result = {"text": "", "structured": result}
    return ctx_result, True, inv.completed_step_results


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
    # S5 guard (c): charge the per-run spawn budget BEFORE spawning the ephemeral
    # session. This is the ONLY spawn-count enforcement for pipeline agent-steps
    # (they reach spawn_ephemeral_session with no parent/lineage, so the
    # registry's SpawnConfig does not cover them). Every AgentStep — top-level or
    # fanned out inside a for_each — funnels here, so the cap is complete.
    deps.spawn_budget.consume(label=inv.step_label)
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

    sub_stores = _eval_pass_entries(
        step.pass_,
        inv.context,
        fail_where=f"step {inv.step_label} (call {step.pipeline!r})",
    )

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


def _eval_pass_entries(
    entries: "list[tuple[str, str]]",
    context: "dict[str, Any]",
    *,
    fail_where: str,
) -> "dict[str, Any]":
    """Evaluate a ``call``/``match`` step's ``pass_`` NAME -> R1-EXPRESSION
    entries against the caller's current context — the SAME
    :func:`evaluate_expr` call ``transform.value`` already makes, so
    ``ctx``/``pipe``/``item``/``acc`` (whatever the caller's scope carries)
    are all reachable from a ``pass:`` entry's expression, exactly like an
    ``agent`` step's prompt template already reaches them. Returns the
    callee's fresh, isolated ``ctx`` seed: ``{callee_name: evaluated_value,
    ...}``. A failing expression (missing path, wrong type, ...) fails the
    step, naming the failing entry."""
    resolved: "dict[str, Any]" = {}
    for callee_name, expr_source in entries:
        try:
            resolved[callee_name] = evaluate_expr(expr_source, context)
        except (ExprEvalError, ExprParseError) as exc:
            raise PipelineExecutionError(
                f"{fail_where} pass: {callee_name!r} = {expr_source!r} failed: {exc}"
            ) from exc
    return resolved


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

    sub_stores = _eval_pass_entries(
        case.pass_,
        inv.context,
        fail_where=f"step {inv.step_label} (match label {label!r} -> {case.pipeline!r})",
    )

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


async def _run_fold_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    """The second COMPOSITIONAL runner (the sequential-accumulator primitive):
    resolve the list source, evaluate ``init``, then walk the list IN ORDER
    running ``do`` once per item — see :class:`FoldStep` for the full context-
    injection + recovery contract. Returns the FINAL ``acc`` (N2), whether any
    executed iteration was side-effecting, and the GROWN
    ``completed_step_results`` (with this fold's ``f"{label}.fold.{k}"`` keys)."""
    step: FoldStep = inv.step  # type: ignore[assignment]
    context = inv.context

    if step.over is not None and step.items is not None:  # pragma: no cover - parser-enforced
        raise PipelineExecutionError(
            f"step {inv.step_label} (fold): 'over' and 'items' are mutually "
            "exclusive list sources"
        )
    if step.over is not None:
        try:
            source = evaluate_expr(step.over, context)
        except ExprEvalError as exc:
            raise PipelineExecutionError(
                f"step {inv.step_label} (fold) 'over' failed: {exc}"
            ) from exc
    elif step.items is not None:
        source = list(step.items)
    else:
        source = context["pipe"]
    if not isinstance(source, list):
        raise PipelineExecutionError(
            f"step {inv.step_label} (fold) list source resolved to a "
            f"{type(source).__name__}, not a list"
        )
    if step.max_items is not None:
        source = source[: step.max_items]

    try:
        acc = evaluate_expr(step.init, context)
    except ExprEvalError as exc:
        raise PipelineExecutionError(
            f"step {inv.step_label} (fold) 'init' failed: {exc}"
        ) from exc

    # Hard rule 5's per-iteration analog: `do` sees the fold step's OWN
    # incoming pipe-data, held constant across every iteration (never the
    # running acc/item — those get their own dedicated names).
    outer_pipe = context["pipe"]
    # A LOCAL named-store copy `do.output` (if set) grows across iterations;
    # it never writes back to the outer scope (only `fold.output` does, via
    # the caller's normal step.output handling — same as `call`).
    local_stores: "dict[str, Any]" = dict(context["ctx"])
    completed_step_results = inv.completed_step_results
    any_durable = False

    for k, item in enumerate(source):
        key = f"{inv.step_label}.fold.{k}"
        if key in completed_step_results:
            # REPLAY exactly-once: rebuild `acc` from the recorded result; `do`
            # does NOT re-execute (its side effect must not re-fire).
            acc = completed_step_results[key]
            continue
        do_context = {"ctx": local_stores, "pipe": outer_pipe, "item": item, "acc": acc}
        runner = STEP_DISPATCH.get(type(step.do))
        if runner is None:  # pragma: no cover - Step is a closed union
            raise PipelineExecutionError(f"unknown step type: {step.do!r}")
        do_inv = _StepInvocation(
            executor=inv.executor, step=step.do, context=do_context, step_label=key,
            deps=inv.deps, completed_step_results=completed_step_results,
            record=inv.record,
        )
        result, durable, completed_step_results = await runner(do_inv)
        any_durable = any_durable or durable
        acc = result
        completed_step_results = {**completed_step_results, key: result}
        if step.do.output:
            local_stores = {**local_stores, step.do.output: result}
        await inv.record(completed_step_results=completed_step_results, durable=durable)

    return acc, any_durable, completed_step_results


def _resolve_list_source(
    step: "ForEachStep | FoldStep", context: "dict[str, Any]", label: str, kind: str,
) -> "list[Any]":
    """Resolve a ``for_each``/``fold`` list source (``over`` R1 expr | ``items``
    static literal | fallback to incoming pipe-data), validating it IS a list.
    Shared shape both primitives use (see :class:`FoldStep`/:class:`ForEachStep`)."""
    if step.over is not None and step.items is not None:  # pragma: no cover - parser-enforced
        raise PipelineExecutionError(
            f"step {label} ({kind}): 'over' and 'items' are mutually exclusive list sources"
        )
    if step.over is not None:
        try:
            source = evaluate_expr(step.over, context)
        except ExprEvalError as exc:
            raise PipelineExecutionError(
                f"step {label} ({kind}) 'over' failed: {exc}"
            ) from exc
    elif step.items is not None:
        source = list(step.items)
    else:
        source = context["pipe"]
    if not isinstance(source, list):
        raise PipelineExecutionError(
            f"step {label} ({kind}) list source resolved to a "
            f"{type(source).__name__}, not a list"
        )
    return source


def _for_each_results(
    completed_step_results: "dict[str, Any]", label: str, n: int,
) -> "list[Any]":
    """Build ``collect``'s ORDERED input list from the recorded per-item results,
    FILTERING OUT dropped markers (:func:`_is_dropped_marker`). The ONE shared
    filter used identically live (all items just landed) AND on resume-replay
    (all item keys read back from a snapshot) — so a dropped item is excluded
    from ``collect``'s input exactly the same way both times. Every index in
    ``range(n)`` MUST have a key by the time this runs (the coordinator only
    reaches ``collect`` once all items are present)."""
    out: "list[Any]" = []
    for item_idx in range(n):
        value = completed_step_results[f"{label}.for_each.{item_idx}"]
        if _is_dropped_marker(value):
            continue
        out.append(value)
    return out


async def _run_for_each_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    """The CONCURRENT fan-out runner (S5-bounded) — ``fold``'s parallel sibling.

    Resolve the list source, then a SINGLE coordinator coroutine fans ``do`` out
    over the items via ``asyncio.as_completed`` gated by a ``Semaphore`` (S5 guard
    a), recording each item's result AS IT LANDS (only the coordinator ever calls
    the real ``record`` — item tasks use a NO-OP recorder, so there is no
    concurrent read-modify-write race on ``completed_step_results``; see the
    module docstring's "commit unit = the ITEM"). ``on_error`` decides an item
    failure: ``continue`` records a DROPPED kind-marker (so resume never re-runs
    it); ``retry(n)`` re-runs the item up to ``n`` more times then falls back to
    ``abort``; ``abort`` cancels the still-pending items and fails the step (the
    already-landed items stay durably recorded, so a resume after an abort-crash
    skips them). After every item index has a key, ``collect`` runs ONCE over the
    filtered ordered results and its result is this step's N2 return value.

    S5 guards: (a) the ``Semaphore`` caps live concurrency; (b) ``fan_out_depth``
    is +1'd through ``_RunDeps`` into every item/collect sub-scope, failing the
    step if it would exceed ``max_fan_out_depth``; (c) each ``AgentStep`` reached
    below charges the shared per-run :class:`SpawnBudget`."""
    step: ForEachStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    context = inv.context
    label = inv.step_label
    completed = inv.completed_step_results

    # Full-completion replay: if collect already ran (its key is present) the whole
    # for_each is done — replay its N2 result, execute nothing.
    collect_key = f"{label}.for_each.collect"
    if collect_key in completed:
        return completed[collect_key], False, completed

    source = _resolve_list_source(step, context, label, "for_each")
    n = len(source)
    on_err = _parse_on_error(step.on_error)

    # S5 guard (b): entering this for_each deepens the fan-out nesting by one.
    next_depth = deps.fan_out_depth + 1
    if deps.max_fan_out_depth and next_depth > deps.max_fan_out_depth:
        raise PipelineExecutionError(
            f"step {label} (for_each) fan-out depth {next_depth} exceeds the "
            f"operator cap {deps.max_fan_out_depth} (S5 depth guard) — a for_each "
            "nested deeper than allowed fails the step rather than spawning"
        )
    # Same spawn_budget by reference (guard c is per-run, not per-branch); only the
    # per-branch fan_out_depth is bumped. Threaded into every item task AND collect.
    child_deps = replace(deps, fan_out_depth=next_depth)

    outer_stores = context["ctx"]
    outer_pipe = context["pipe"]

    async def _noop_record(**_kw: Any) -> None:
        # Item tasks NEVER record: only the single coordinator does (serialized),
        # so concurrent items cannot race the read-modify-write of the snapshot.
        return None

    async def _run_item(item_idx: int, item: Any) -> "tuple[int, Any, bool, Exception | None]":
        # Retry(n) re-runs INSIDE the same task (same semaphore slot — a retry
        # does not raise live concurrency). Any Exception is an item failure
        # (CancelledError is BaseException, so it is NOT swallowed here — an
        # abort-cancel propagates out and the task is gathered in the finally).
        attempts = 1 + (on_err.retries if on_err.kind == "retry" else 0)
        last_exc: "Exception | None" = None
        for _attempt in range(attempts):
            item_ctx = {"ctx": dict(outer_stores), "pipe": outer_pipe, "item": item}
            runner = STEP_DISPATCH.get(type(step.do))
            if runner is None:  # pragma: no cover - Step is a closed union
                raise PipelineExecutionError(f"unknown step type: {step.do!r}")
            do_inv = _StepInvocation(
                executor=inv.executor, step=step.do, context=item_ctx,
                step_label=f"{label}.for_each.{item_idx}", deps=child_deps,
                completed_step_results=completed, record=_noop_record,
            )
            try:
                result, durable, _ = await runner(do_inv)
                return item_idx, result, durable, None
            except Exception as exc:  # noqa: BLE001 - item-failure boundary (on_error policy)
                last_exc = exc
        return item_idx, None, False, last_exc

    # Only items whose key is ABSENT run (resume re-runs exactly the absent ones;
    # a present success key replays, a present dropped-marker stays dropped).
    pending = [i for i in range(n) if f"{label}.for_each.{i}" not in completed]
    any_durable = False
    if pending:
        effective_max_parallel = step.max_parallel or min(len(pending), _DEFAULT_MAX_PARALLEL)
        sem = asyncio.Semaphore(effective_max_parallel)

        async def _gated(item_idx: int, item: Any) -> "tuple[int, Any, bool, Exception | None]":
            async with sem:
                return await _run_item(item_idx, item)

        tasks = [asyncio.create_task(_gated(i, source[i])) for i in pending]
        try:
            for fut in asyncio.as_completed(tasks):
                item_idx, result, durable, exc = await fut
                key = f"{label}.for_each.{item_idx}"
                if exc is None:
                    completed = {**completed, key: result}
                    any_durable = any_durable or durable
                    await inv.record(completed_step_results=completed, durable=durable)
                elif on_err.kind == "continue":
                    # DROP the failed item: record a kind-marker (NOT absent — else
                    # resume re-runs it forever; NOT bare None — a legit result).
                    completed = {
                        **completed,
                        key: {_FAN_OUT_DROPPED_KEY: True, "error": str(exc)},
                    }
                    any_durable = True  # a drop MUST survive to resume (awaited-durable)
                    await inv.record(completed_step_results=completed, durable=True)
                else:
                    # abort (incl. retry(n) exhausted): the landed items above are
                    # already durably recorded; fail the step now.
                    raise PipelineExecutionError(
                        f"step {label} (for_each) item {item_idx} failed "
                        f"(on_error={step.on_error!r}): {exc}"
                    ) from exc
        finally:
            # Never leave orphaned live fan-out tasks (an abort-raise cancels the
            # still-pending items; a clean finish finds them all already done).
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # collect runs ONCE over the ordered surviving results (dropped filtered out).
    results_list = _for_each_results(completed, label, n)
    collect_ctx = {"ctx": outer_stores, "pipe": results_list}
    collect_runner = STEP_DISPATCH.get(type(step.collect))
    if collect_runner is None:  # pragma: no cover - Step is a closed union
        raise PipelineExecutionError(f"unknown step type: {step.collect!r}")
    collect_inv = _StepInvocation(
        executor=inv.executor, step=step.collect, context=collect_ctx,
        step_label=collect_key, deps=child_deps,
        completed_step_results=completed, record=inv.record,
    )
    collect_result, collect_durable, completed = await collect_runner(collect_inv)
    completed = {**completed, collect_key: collect_result}
    any_durable = any_durable or collect_durable
    await inv.record(completed_step_results=completed, durable=collect_durable)
    return collect_result, any_durable, completed


def _parallel_results(
    completed_step_results: "dict[str, Any]", label: str, names: "list[str]",
) -> "dict[str, Any]":
    """Build ``collect``'s NAMED-MAP input ``{branch_name: result}`` from the
    recorded per-branch results, FILTERING OUT dropped markers
    (:func:`_is_dropped_marker`) — ``for_each``'s :func:`_for_each_results`,
    adapted from an ordered list to a dict keyed by branch NAME. The same one
    shared filter used identically live (all branches just landed) AND on
    resume-replay (every branch key read back from a snapshot). Every name in
    ``names`` MUST have a key by the time this runs (the coordinator only
    reaches ``collect`` once every branch is present)."""
    out: "dict[str, Any]" = {}
    for name in names:
        value = completed_step_results[f"{label}.parallel.{name}"]
        if _is_dropped_marker(value):
            continue
        out[name] = value
    return out


async def _run_parallel_step(inv: "_StepInvocation") -> "tuple[Any, bool, dict[str, Any]]":
    """The heterogeneous NAMED-branch fan-out runner (S5-bounded) — reuses the
    EXACT fan-out substrate :func:`_run_for_each_step` established (concurrent
    recovery via a single serializing coordinator + a NO-OP recorder inside
    each branch task, the ``__fan_out_dropped__`` kind-marker, the
    fan_out_depth/SpawnBudget S5 guards). The one structural difference: the
    source is a STATIC FINITE dict of DISTINCT named branches (each its own
    heterogeneous Step) rather than one ``do`` re-invoked per list item, so
    every branch runs concurrently with NO Semaphore (the branch count IS the
    bound — there is no ``max_parallel`` field), and ``collect`` runs ONCE
    over the NAMED MAP of surviving branch results (:func:`_parallel_results`),
    not an ordered list.

    ``on_error`` (optional, default ``abort`` — normalized the same way
    ``for_each.on_error`` is, via :func:`_parse_on_error`) decides a branch
    failure: ``continue`` records a DROPPED kind-marker (so resume never
    re-runs it); ``retry(n)`` re-runs the branch's Step up to ``n`` more times
    then falls back to ``abort``; ``abort`` cancels the still-pending branches
    and fails the step (the already-landed branches stay durably recorded, so
    a resume after an abort-crash skips them).

    S5 guards: (b) ``fan_out_depth`` is +1'd through ``_RunDeps`` into every
    branch/collect sub-scope, failing the step if it would exceed
    ``max_fan_out_depth``; (c) each ``AgentStep`` reached below charges the
    shared per-run :class:`SpawnBudget`."""
    step: ParallelStep = inv.step  # type: ignore[assignment]
    deps = inv.deps
    context = inv.context
    label = inv.step_label
    completed = inv.completed_step_results

    # Full-completion replay: if collect already ran (its key is present) the
    # whole parallel is done — replay its N2 result, execute nothing.
    collect_key = f"{label}.parallel.collect"
    if collect_key in completed:
        return completed[collect_key], False, completed

    names = list(step.branches)
    on_err = _parse_on_error(step.on_error)

    # S5 guard (b): entering this parallel deepens the fan-out nesting by one
    # (same guard, same semantics as for_each — a parallel scope counts as a
    # fan-out level too).
    next_depth = deps.fan_out_depth + 1
    if deps.max_fan_out_depth and next_depth > deps.max_fan_out_depth:
        raise PipelineExecutionError(
            f"step {label} (parallel) fan-out depth {next_depth} exceeds the "
            f"operator cap {deps.max_fan_out_depth} (S5 depth guard) — a "
            "parallel nested deeper than allowed fails the step rather than "
            "spawning"
        )
    child_deps = replace(deps, fan_out_depth=next_depth)

    outer_stores = context["ctx"]
    outer_pipe = context["pipe"]

    async def _noop_record(**_kw: Any) -> None:
        # Branch tasks NEVER record: only the single coordinator does
        # (serialized), so concurrent branches cannot race the
        # read-modify-write of the snapshot (identical to for_each's item
        # tasks).
        return None

    async def _run_branch(name: str) -> "tuple[str, Any, bool, Exception | None]":
        branch_step = step.branches[name]
        # Retry(n) re-runs INSIDE the same task. Any Exception is a branch
        # failure (CancelledError is BaseException, so an abort-cancel
        # propagates out and the task is gathered in the finally).
        attempts = 1 + (on_err.retries if on_err.kind == "retry" else 0)
        last_exc: "Exception | None" = None
        for _attempt in range(attempts):
            branch_ctx = {"ctx": dict(outer_stores), "pipe": outer_pipe}
            runner = STEP_DISPATCH.get(type(branch_step))
            if runner is None:  # pragma: no cover - Step is a closed union
                raise PipelineExecutionError(f"unknown step type: {branch_step!r}")
            branch_inv = _StepInvocation(
                executor=inv.executor, step=branch_step, context=branch_ctx,
                step_label=f"{label}.parallel.{name}", deps=child_deps,
                completed_step_results=completed, record=_noop_record,
            )
            try:
                result, durable, _ = await runner(branch_inv)
                return name, result, durable, None
            except Exception as exc:  # noqa: BLE001 - branch-failure boundary (on_error policy)
                last_exc = exc
        return name, None, False, last_exc

    # Only branches whose key is ABSENT run (resume re-runs exactly the absent
    # ones; a present success key replays, a present dropped-marker stays
    # dropped).
    pending = [n for n in names if f"{label}.parallel.{n}" not in completed]
    any_durable = False
    if pending:
        # No Semaphore: the branch set is statically finite (there is no
        # max_parallel field) — every pending branch runs concurrently.
        tasks = [asyncio.create_task(_run_branch(n)) for n in pending]
        try:
            for fut in asyncio.as_completed(tasks):
                name, result, durable, exc = await fut
                key = f"{label}.parallel.{name}"
                if exc is None:
                    completed = {**completed, key: result}
                    any_durable = any_durable or durable
                    await inv.record(completed_step_results=completed, durable=durable)
                elif on_err.kind == "continue":
                    # DROP the failed branch: record a kind-marker (NOT absent
                    # — else resume re-runs it forever; NOT bare None — a
                    # legit branch result).
                    completed = {
                        **completed,
                        key: {_FAN_OUT_DROPPED_KEY: True, "error": str(exc)},
                    }
                    any_durable = True  # a drop MUST survive to resume (awaited-durable)
                    await inv.record(completed_step_results=completed, durable=True)
                else:
                    # abort (incl. retry(n) exhausted): the landed branches
                    # above are already durably recorded; fail the step now.
                    raise PipelineExecutionError(
                        f"step {label} (parallel) branch {name!r} failed "
                        f"(on_error={step.on_error!r}): {exc}"
                    ) from exc
        finally:
            # Never leave orphaned live fan-out tasks (an abort-raise cancels
            # the still-pending branches; a clean finish finds them all
            # already done).
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # collect runs ONCE over the NAMED MAP of surviving results (dropped
    # filtered out).
    results_map = _parallel_results(completed, label, names)
    collect_ctx = {"ctx": outer_stores, "pipe": results_map}
    collect_runner = STEP_DISPATCH.get(type(step.collect))
    if collect_runner is None:  # pragma: no cover - Step is a closed union
        raise PipelineExecutionError(f"unknown step type: {step.collect!r}")
    collect_inv = _StepInvocation(
        executor=inv.executor, step=step.collect, context=collect_ctx,
        step_label=collect_key, deps=child_deps,
        completed_step_results=completed, record=inv.record,
    )
    collect_result, collect_durable, completed = await collect_runner(collect_inv)
    completed = {**completed, collect_key: collect_result}
    any_durable = any_durable or collect_durable
    await inv.record(completed_step_results=completed, durable=collect_durable)
    return collect_result, any_durable, completed


# Dispatch table: step dataclass type -> its runner. A future primitive ADDS an
# entry here (+ serde/parser/analyzer) rather than editing a shared elif chain.
STEP_DISPATCH: "dict[type, Callable[[_StepInvocation], Awaitable[tuple[Any, bool, dict[str, Any]]]]]" = {
    TransformStep: _run_transform_step,
    ToolStep: _run_tool_step,
    AgentStep: _run_agent_step,
    CallStep: _run_call_step,
    MatchStep: _run_match_step,
    FoldStep: _run_fold_step,
    ForEachStep: _run_for_each_step,
    ParallelStep: _run_parallel_step,
}

# Type -> kind-string, the inverse vocabulary the serde ``kind`` marker uses.
_STEP_KINDS: "dict[type, str]" = {
    TransformStep: "transform",
    ToolStep: "tool",
    AgentStep: "agent",
    CallStep: "call",
    MatchStep: "match",
    FoldStep: "fold",
    ForEachStep: "for_each",
    ParallelStep: "parallel",
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
        max_fan_out_depth: "int | None" = None,
        max_pipeline_spawns: "int | None" = None,
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
        `cancel_check` at a step boundary raises :class:`PipelineCancelled`.

        `max_fan_out_depth` / `max_pipeline_spawns` are the S5 fan-out spawn caps
        (guards b/c). Both default to conservative finite module constants when
        omitted (NEVER unbounded-by-omission); the operator wires reyn.yaml's
        ``safety.spawn.*`` values here via the driver. ``0`` = unlimited (opt-out).
        Deliberately decoupled from `registry`: a `for_each` needs its caps even
        with no `AgentStep` (so no `registry`) in the pipeline."""
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
            max_fan_out_depth=max_fan_out_depth,
            max_pipeline_spawns=max_pipeline_spawns,
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
        max_fan_out_depth: "int | None" = None,
        max_pipeline_spawns: "int | None" = None,
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
                max_fan_out_depth=max_fan_out_depth,
                max_pipeline_spawns=max_pipeline_spawns,
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
            max_fan_out_depth=max_fan_out_depth,
            max_pipeline_spawns=max_pipeline_spawns,
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
        max_fan_out_depth: "int | None" = None,
        max_pipeline_spawns: "int | None" = None,
    ) -> PipelineResult:
        # S5 caps: default to conservative finite constants when the caller omits
        # them (never unbounded-by-omission). ``0`` (an explicit operator opt-out)
        # is preserved as unlimited by the guards. The SpawnBudget is constructed
        # ONCE here — this is the single funnel both run() and resume() reach — and
        # shared by reference through every nested scope's _RunDeps (guard c).
        eff_max_depth = (
            _DEFAULT_MAX_FAN_OUT_DEPTH if max_fan_out_depth is None else max_fan_out_depth
        )
        eff_max_spawns = (
            _DEFAULT_MAX_PIPELINE_SPAWNS if max_pipeline_spawns is None else max_pipeline_spawns
        )
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
            fan_out_depth=0,
            max_fan_out_depth=eff_max_depth,
            spawn_budget=SpawnBudget(eff_max_spawns),
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
    "FoldStep",
    "ForEachStep",
    "ParallelStep",
    "SpawnBudget",
    "Step",
    "Pipeline",
    "PipelineResult",
    "PipelineError",
    "PipelineExecutionError",
    "PipelineCancelled",
    "PipelineExecutor",
    "STEP_DISPATCH",
]
