"""Pipeline DSL parser (IS-3): compact YAML text -> :class:`Pipeline` dataclasses.

Implements the surface grammar in Appendix B of
``docs/proposals/reyn-pipeline-spec-v0.8.md`` (lines 706-835), narrowed to
exactly the subset :class:`reyn.core.pipeline.executor.PipelineExecutor` can
run today: a **linear** sequence of ``transform`` / ``tool`` / ``shell`` /
``agent`` steps plus three COMPOSITIONAL primitives — ``call`` (R7 — runs a
STATIC registered sub-pipeline synchronously), ``match`` (``call``'s
runtime-selected sibling: a runtime VALUE picks a case LABEL, whose target
stays a static literal), and ``fold`` (sequential accumulator — runs a nested
``do`` step once per list item) — plus the pipeline-level ``description``.
``for_each`` / ``parallel`` / ``refine``, and the pipeline-level ``input`` /
``defaults`` blocks are all part of the full v0.8 grammar but have no runtime
yet — this parser refuses to accept them rather than silently dropping them
into a pipeline that "parses" but then either crashes at run time in a
confusing place or (worse) quietly loses the author's intent. Every construct
not yet executable raises
:class:`PipelineParseError` naming it explicitly, so authoring a
not-yet-supported pipeline fails at parse time, at the DSL text, not deep in
an executor stack trace.

**Tool/shell ``args`` — the load-bearing divergence from Appendix B.** The
compact spec writes ``tool = {... args?:{KEY:TPL} ...}`` (a template string
per arg, interpolated the same way an ``agent.prompt`` is). The executor does
**not** do that: :class:`~reyn.core.pipeline.executor.ToolStep` resolves an
``args`` value against the step context only when it is wrapped in
:class:`~reyn.core.pipeline.executor.ExprRef`; every other value (including a
bare string) passes through as a Python literal, untouched. Mixing those two
models — DSL author writes ``{ctx.x}`` expecting interpolation, executor
treats it as a literal string containing four literal characters — is
exactly the "parses fine, resolves wrong at runtime" drift this parser must
not produce. So this parser defines its own explicit surface rule instead of
mirroring Appendix B literally:

    A tool/shell ``args``/``command`` value is a **literal** UNLESS it is
    tagged with the YAML tag ``!expr``, e.g. ``query: !expr ctx.brief`` or
    ``limit: !expr "ctx.n + 1"`` — in which case it becomes an
    :class:`ExprRef` wrapping the scalar's text as an R1 expression source
    (validated by ``expr.parse`` at parse time, so a malformed expression is
    a DSL parse error, not a runtime surprise). A plain untagged value —
    string, number, list, mapping — is stored as-is and passed straight
    through to ``tool_dispatch``. ``!expr`` is only honored as the WHOLE
    value of an args entry; one hiding inside a nested list/mapping is a
    parse error (it would otherwise silently reach ``tool_dispatch`` as an
    inert tag object instead of the resolved value the author intended).

``agent.prompt`` keeps the Appendix B ``TPL`` semantics verbatim — it is
stored as the literal template string and interpolated at run time by the
executor's own ``_interpolate_prompt`` (``{ctx.NAME.field}`` / ``{pipe}``).
This parser does not touch it beyond storing it.

``transform.value`` (and nothing else) is an R1 expression SOURCE per
Appendix B's ``EXPR`` — stored verbatim as a string (never evaluated here,
the executor evaluates it against the live context), but run through
``expr.parse`` at parse time so a malformed expression fails immediately.

Standalone ``Schema`` documents (Appendix B: ``Schema = name:NAME
fields:{...}``) register into a caller-supplied
:class:`~reyn.core.pipeline.schema.SchemaRegistry` — the same registry the
caller later hands to ``PipelineExecutor.run``/``resume`` so a step's
``schema: REF`` resolves. This module never constructs its own registry
implicitly: the caller owns the registry's lifetime (mirrors how
``AgentRegistry``/``StateLog`` are threaded explicitly elsewhere in this
package), so schemas parsed from one DSL file are visible to pipelines
parsed from another as long as the same registry is passed to both.

A DSL text is one or more YAML documents (``---``-separated). Each document
is classified as a ``schema:`` document or a ``pipeline:`` document by its
top-level key; a text may mix both (schemas typically precede the pipeline
that references them). Exactly one ``pipeline:`` document must be present in
a call to :func:`parse_pipeline_dsl` — a file with zero or more than one is a
parse error (unambiguous "what did I just parse" contract for the registry
caller populating a :class:`~reyn.core.pipeline.registry.PipelineRegistry`
from one file per pipeline).
"""
from __future__ import annotations

import re
from typing import Any

import yaml

from reyn.core.pipeline.executor import (
    AgentStep,
    CallStep,
    ExprRef,
    FoldStep,
    ForEachStep,
    MatchCase,
    MatchStep,
    Pipeline,
    Step,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.expr import ExprParseError
from reyn.core.pipeline.expr import parse as parse_expr
from reyn.core.pipeline.schema import SchemaRegistry

__all__ = ["PipelineParseError", "parse_pipeline_dsl"]


class PipelineParseError(ValueError):
    """Raised for any DSL text this parser cannot turn into a `Pipeline` the
    current linear executor can run — malformed YAML, a malformed R1
    expression, or (most commonly) a construct that is valid Appendix-B
    grammar but not yet supported by `PipelineExecutor` (`for_each`,
    `parallel`, `refine`, pipeline-level `input`/
    `defaults`, or a per-step field the executor does not consume)."""


# ---------------------------------------------------------------------------
# `!expr` YAML tag — the tool/shell-arg expression marker (see module
# docstring). A dedicated tag rather than reusing agent-prompt `{...}`
# brace syntax so there is no ambiguity between "a literal string that
# happens to contain braces" and "an expression to resolve".
# ---------------------------------------------------------------------------


class _ExprTag:
    """Marks a YAML scalar tagged ``!expr`` — carries the raw (untouched)
    scalar text as the candidate R1 expression source."""

    __slots__ = ("src",)

    def __init__(self, src: str) -> None:
        self.src = src


class _PipelineLoader(yaml.SafeLoader):
    """`yaml.SafeLoader` plus the `!expr` tag constructor, minus YAML 1.1's
    `on`/`off`/`yes`/`no` implicit-bool resolution ("the Norway problem"). A
    dedicated subclass (rather than mutating `SafeLoader` globally) keeps
    this parser's tag vocabulary / resolver narrowing from leaking into
    unrelated YAML loads elsewhere in the process.

    The bool-resolver narrowing matters because Appendix B's `match` step
    names its selector field literally `on` (`match = {on: PATH, cases:
    ...}`) — under `SafeLoader`'s stock YAML 1.1 resolver, an unquoted `on:`
    key (or `off`/`yes`/`no`) resolves to the Python `bool` `True`/`False`
    instead of the string key/value the DSL author wrote, silently breaking
    every `match` step (and any tool/shell arg literally named or valued
    `on`/`off`/`yes`/`no`) unless every author remembers to quote it. This
    loader instead resolves `bool` only for YAML 1.2 core-schema spellings
    (`true`/`True`/`TRUE`/`false`/`False`/`FALSE`) — `on`/`off`/`yes`/`no`
    pass through as plain strings, matching how most modern YAML tooling
    (e.g. `ruamel.yaml`'s default) already resolved this ambiguity."""


_BOOL_TAG = "tag:yaml.org,2002:bool"
# `SafeLoader` registers ONE shared bool regex (matching
# yes/Yes/YES/no/No/NO/true/True/TRUE/false/False/FALSE/on/On/ON/off/Off/OFF)
# under EVERY first-character bucket it could start with. Dropping the entry
# from just the `on`/`off`/`yes`/`no` buckets (o/O/y/Y/n/N) — while leaving
# the t/T/f/F buckets (`true`/`false`) untouched — is enough: resolution
# looks up by the scalar's OWN first character, so `"true"` still hits the
# t-bucket entry while `on`/`off`/`yes`/`no` no longer match anything and
# fall through to plain strings.
_PipelineLoader.yaml_implicit_resolvers = {
    first_char: [
        (tag, regexp)
        for tag, regexp in resolvers
        if not (tag == _BOOL_TAG and first_char in "oOyYnN")
    ]
    for first_char, resolvers in _PipelineLoader.yaml_implicit_resolvers.items()
}


def _construct_expr_tag(loader: yaml.SafeLoader, node: yaml.Node) -> _ExprTag:
    return _ExprTag(loader.construct_scalar(node))


_PipelineLoader.add_constructor("!expr", _construct_expr_tag)

# Constructs the union grammar's non-linear step kinds accept as *keys* so a
# document using them gets a clear "not yet supported" error instead of a
# generic "unknown step type". ``call`` (R7), ``match`` (runtime-selected
# sibling), ``fold`` (sequential accumulator), and ``for_each`` (concurrent
# fan-out) are now SUPPORTED — they moved out of this set into ``_STEP_PARSERS``
# (``fold``/``for_each`` are dispatched specially in ``_parse_step`` since their
# ``do``/``collect`` sub-steps recurse). ``parallel`` is the last primitive.
_UNSUPPORTED_STEP_KINDS = ("parallel",)
_LINEAR_STEP_KINDS = ("transform", "tool", "shell", "agent")
# ``call``/``match``/``fold``/``for_each`` are compositional, not linear, but ARE
# executable — listed separately so error text distinguishes "the linear
# kinds" from the full supported set.
_SUPPORTED_STEP_KINDS = _LINEAR_STEP_KINDS + ("call", "match", "fold", "for_each")
_ALL_STEP_KINDS = _SUPPORTED_STEP_KINDS + _UNSUPPORTED_STEP_KINDS

# Pipeline-level fields the full grammar allows but the linear executor has
# no runtime concept of at all (R4 recovery / R3 threading only understand
# `steps`; `description` is IS-5 surfacing metadata, also consumed).
_UNSUPPORTED_PIPELINE_KEYS = ("input", "defaults", "refine")
_SUPPORTED_PIPELINE_KEYS = frozenset({"pipeline", "description", "steps"})


def _fail(message: str) -> "None":
    raise PipelineParseError(message)


def _validate_expr_source(src: Any, *, where: str) -> str:
    if not isinstance(src, str):
        _fail(f"{where}: expected an expression string, got {type(src).__name__}")
    try:
        parse_expr(src)
    except ExprParseError as exc:
        raise PipelineParseError(f"{where}: malformed expression {src!r}: {exc}") from exc
    return src


def _reject_nested_expr_tag(value: Any, *, where: str) -> None:
    """`!expr` is only meaningful as the WHOLE value of an args entry (see
    module docstring). One buried inside a list/mapping would otherwise
    reach `tool_dispatch` as an inert `_ExprTag` object instead of either a
    literal or a resolved value — a silent-wrong-result trap — so it is a
    parse error instead."""
    if isinstance(value, _ExprTag):
        _fail(
            f"{where}: '!expr' is only supported as the entire value of an "
            "args entry, not nested inside a list/mapping"
        )
    if isinstance(value, dict):
        for k, v in value.items():
            _reject_nested_expr_tag(v, where=f"{where}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _reject_nested_expr_tag(v, where=f"{where}[{i}]")


def _resolve_arg_value(value: Any, *, where: str) -> Any:
    if isinstance(value, _ExprTag):
        _validate_expr_source(value.src, where=where)
        return ExprRef(value.src)
    _reject_nested_expr_tag(value, where=where)
    return value


def _reject_unknown_keys(
    doc: "dict[str, Any]", allowed: "frozenset[str] | tuple[str, ...]", *, where: str
) -> None:
    allowed_set = frozenset(allowed)
    extra = sorted(set(doc) - allowed_set)
    if extra:
        _fail(
            f"{where}: field(s) {extra!r} are not supported by the linear "
            "executor (later slice — see PipelineExecutor's module docstring "
            "for current scope)"
        )


# ---------------------------------------------------------------------------
# Step parsing
# ---------------------------------------------------------------------------

_TRANSFORM_KEYS = frozenset({"value", "output"})
_TOOL_KEYS = frozenset({"name", "args", "schema", "output"})
_SHELL_KEYS = frozenset({"command", "schema", "output"})
_AGENT_KEYS = frozenset({"prompt", "identity", "capabilities", "schema", "output"})
_CALL_KEYS = frozenset({"pipeline", "pass", "output"})
_MATCH_KEYS = frozenset({"on", "cases", "default", "output"})
_MATCH_CASE_KEYS = frozenset({"pipeline", "pass"})


def _parse_transform_step(body: "dict[str, Any]") -> TransformStep:
    _reject_unknown_keys(body, _TRANSFORM_KEYS, where="transform step")
    if "value" not in body:
        _fail("transform step: missing required field 'value'")
    value = _validate_expr_source(body["value"], where="transform step 'value'")
    return TransformStep(value=value, output=body.get("output"))


def _parse_tool_step(body: "dict[str, Any]") -> ToolStep:
    _reject_unknown_keys(body, _TOOL_KEYS, where="tool step")
    if "name" not in body:
        _fail("tool step: missing required field 'name'")
    name = body["name"]
    if not isinstance(name, str):
        _fail(f"tool step 'name': expected a literal string, got {type(name).__name__}")
    raw_args = body.get("args") or {}
    if not isinstance(raw_args, dict):
        _fail(f"tool step 'args': expected a mapping, got {type(raw_args).__name__}")
    args = {
        k: _resolve_arg_value(v, where=f"tool {name!r} arg {k!r}")
        for k, v in raw_args.items()
    }
    schema = body.get("schema")
    if schema is not None and not isinstance(schema, str):
        _fail(f"tool step 'schema': expected a schema-name string, got {type(schema).__name__}")
    return ToolStep(name=name, args=args, output=body.get("output"), schema=schema)


def _parse_shell_step(body: "dict[str, Any]") -> ToolStep:
    _reject_unknown_keys(body, _SHELL_KEYS, where="shell step")
    if "command" not in body:
        _fail("shell step: missing required field 'command'")
    command = _resolve_arg_value(body["command"], where="shell step 'command'")
    schema = body.get("schema")
    if schema is not None and not isinstance(schema, str):
        _fail(f"shell step 'schema': expected a schema-name string, got {type(schema).__name__}")
    return ToolStep(
        name="shell", args={"command": command}, output=body.get("output"), schema=schema,
    )


def _parse_agent_step(body: "dict[str, Any]") -> AgentStep:
    _reject_unknown_keys(body, _AGENT_KEYS, where="agent step")
    if "prompt" not in body:
        _fail("agent step: missing required field 'prompt'")
    prompt = body["prompt"]
    if not isinstance(prompt, str):
        _fail(f"agent step 'prompt': expected a TPL string, got {type(prompt).__name__}")
    identity = body.get("identity")
    if identity is not None and not isinstance(identity, str):
        _fail(f"agent step 'identity': expected a literal string, got {type(identity).__name__}")
    capabilities = None
    raw_caps = body.get("capabilities")
    if raw_caps is not None:
        if not isinstance(raw_caps, dict) or "tools" not in raw_caps:
            _fail(
                "agent step 'capabilities': expected {tools: [NAME*]} per Appendix B, "
                f"got {raw_caps!r}"
            )
        tools = raw_caps["tools"]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            _fail("agent step 'capabilities.tools': expected a list of literal strings")
        capabilities = list(tools)
    schema = body.get("schema")
    if schema is not None and not isinstance(schema, str):
        _fail(f"agent step 'schema': expected a schema-name string, got {type(schema).__name__}")
    return AgentStep(
        prompt=prompt, identity=identity, capabilities=capabilities, schema=schema,
        output=body.get("output"),
    )


def _parse_call_step(body: "dict[str, Any]") -> CallStep:
    """``call = {pipeline: LIT, pass: [NAME*], output: NAME}`` (Appendix B, R7).
    ``pipeline`` is a STATIC literal name (Hard rule 2 — never an expression);
    ``pass`` is the caller-store projection the callee may reference; ``output``
    binds the callee's final result to a caller named store."""
    _reject_unknown_keys(body, _CALL_KEYS, where="call step")
    if "pipeline" not in body:
        _fail("call step: missing required field 'pipeline'")
    name = body["pipeline"]
    if not isinstance(name, str) or not name:
        _fail(
            f"call step 'pipeline': expected a non-empty literal pipeline name, "
            f"got {name!r}"
        )
    raw_pass = body.get("pass") or []
    if not isinstance(raw_pass, list) or not all(isinstance(n, str) for n in raw_pass):
        _fail(f"call step 'pass': expected a list of store-name strings, got {raw_pass!r}")
    output = body.get("output")
    if output is not None and not isinstance(output, str):
        _fail(f"call step 'output': expected a store-name string, got {type(output).__name__}")
    return CallStep(pipeline=name, pass_=list(raw_pass), output=output)


def _parse_match_case(body: Any, *, where: str) -> MatchCase:
    """A ``match`` case/``default`` body: ``{pipeline: LIT, pass: [NAME*]}`` —
    the SAME shape (and Hard-rule-2 static-literal-target rule) as a ``call``
    step's own ``{pipeline, pass}``, just nested under a case LABEL / the
    ``default`` key instead of being the step body itself."""
    if not isinstance(body, dict):
        _fail(f"{where}: expected a mapping {{pipeline, pass?}}, got {type(body).__name__}")
    _reject_unknown_keys(body, _MATCH_CASE_KEYS, where=where)
    if "pipeline" not in body:
        _fail(f"{where}: missing required field 'pipeline'")
    name = body["pipeline"]
    if not isinstance(name, str) or not name:
        _fail(f"{where} 'pipeline': expected a non-empty literal pipeline name, got {name!r}")
    raw_pass = body.get("pass") or []
    if not isinstance(raw_pass, list) or not all(isinstance(n, str) for n in raw_pass):
        _fail(f"{where} 'pass': expected a list of store-name strings, got {raw_pass!r}")
    return MatchCase(pipeline=name, pass_=list(raw_pass))


def _parse_match_step(body: "dict[str, Any]") -> MatchStep:
    """``match = {on: PATH, cases: {LABEL: {pipeline: LIT, pass: [NAME*]}}+,
    default?: {pipeline: LIT, pass: [NAME*]}, output?: NAME}`` (Appendix B).
    ``on`` is an R1 expression source (same as ``transform.value``) whose
    RUNTIME VALUE selects a case LABEL by string equality — every case/
    ``default`` TARGET stays a static literal (Hard rule 2)."""
    _reject_unknown_keys(body, _MATCH_KEYS, where="match step")
    if "on" not in body:
        _fail("match step: missing required field 'on'")
    on = _validate_expr_source(body["on"], where="match step 'on'")
    raw_cases = body.get("cases")
    if not isinstance(raw_cases, dict) or not raw_cases:
        _fail("match step: 'cases' must be a non-empty mapping of LABEL -> {pipeline, pass?}")
    cases = {
        label: _parse_match_case(case_body, where=f"match step case {label!r}")
        for label, case_body in raw_cases.items()
    }
    raw_default = body.get("default")
    default = (
        _parse_match_case(raw_default, where="match step 'default'")
        if raw_default is not None
        else None
    )
    output = body.get("output")
    if output is not None and not isinstance(output, str):
        _fail(f"match step 'output': expected a store-name string, got {type(output).__name__}")
    return MatchStep(on=on, cases=cases, default=default, output=output)


_FOLD_KEYS = frozenset({"over", "items", "init", "do", "output", "max_items"})


def _parse_fold_step(body: "dict[str, Any]", *, index: "int | str") -> FoldStep:
    """``fold = {over?:PATH | items?:[LIT*] init:EXPR do:Step output:NAME
    max_items?}`` (Appendix B) — the sequential-accumulator primitive.
    ``over``/``items`` are mutually exclusive (both together is an ambiguous
    list source, never silently resolved one way); neither given falls back to
    the incoming pipe data at run time (the executor's own fallback — see
    ``FoldStep``'s docstring), so the parser does not require either. ``do`` is
    itself a full nested Step definition, parsed the same way a pipeline's
    top-level steps are (so a fold's ``do`` may be any supported step kind,
    including a nested ``call``/``fold``). ``output`` (unlike ``call``'s,
    which is optional) is REQUIRED — Appendix B gives it no ``?``, since a
    fold's whole point is producing a named accumulator result."""
    _reject_unknown_keys(body, _FOLD_KEYS, where="fold step")
    if "over" in body and "items" in body:
        _fail("fold step: 'over' and 'items' are mutually exclusive list sources")
    over = body.get("over")
    if over is not None:
        over = _validate_expr_source(over, where="fold step 'over'")
    raw_items = body.get("items")
    items: "list[Any] | None" = None
    if raw_items is not None:
        if not isinstance(raw_items, list):
            _fail(f"fold step 'items': expected a list, got {type(raw_items).__name__}")
        _reject_nested_expr_tag(raw_items, where="fold step 'items'")
        items = list(raw_items)
    if "init" not in body:
        _fail("fold step: missing required field 'init'")
    init = _validate_expr_source(body["init"], where="fold step 'init'")
    if "do" not in body:
        _fail("fold step: missing required field 'do'")
    do = _parse_step(body["do"], index=f"{index} (fold do)")
    output = body.get("output")
    if not isinstance(output, str) or not output:
        _fail(f"fold step 'output': expected a non-empty store-name string, got {output!r}")
    max_items = body.get("max_items")
    if max_items is not None and (
        isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0
    ):
        _fail(f"fold step 'max_items': expected a positive integer, got {max_items!r}")
    return FoldStep(
        init=init, do=do, output=output, over=over, items=items, max_items=max_items,
    )


_FOR_EACH_KEYS = frozenset(
    {"over", "items", "max_parallel", "on_error", "do", "collect", "output"}
)
_ON_ERROR_RE = re.compile(r"^retry\(\d+\)$")


def _parse_for_each_step(body: "dict[str, Any]", *, index: "int | str") -> ForEachStep:
    """``for_each = {over?:PATH | items?:[LIT*] max_parallel? on_error:continue|
    abort|retry(n) do:Step collect:Step}`` (Appendix B) — the concurrent fan-out
    primitive. ``over``/``items`` are mutually exclusive (both together is an
    ambiguous list source); neither falls back to the incoming pipe data at run
    time (like ``fold``). ``on_error`` is REQUIRED (Appendix B gives it no ``?`` —
    a fan-out author MUST state the completeness policy; unlike ``parallel`` whose
    ``on_error?`` defaults to ``abort``) and must be ``continue``/``abort``/
    ``retry(n)``. ``do`` AND ``collect`` are each a full nested Step (parsed the
    same way top-level steps are, so either may be a nested ``call``/``fold``/
    ``for_each``); ``collect`` is REQUIRED (it produces the primitive's N2
    result). ``max_parallel`` (S5 Semaphore cap) is an optional positive int."""
    _reject_unknown_keys(body, _FOR_EACH_KEYS, where="for_each step")
    if "over" in body and "items" in body:
        _fail("for_each step: 'over' and 'items' are mutually exclusive list sources")
    over = body.get("over")
    if over is not None:
        over = _validate_expr_source(over, where="for_each step 'over'")
    raw_items = body.get("items")
    items: "list[Any] | None" = None
    if raw_items is not None:
        if not isinstance(raw_items, list):
            _fail(f"for_each step 'items': expected a list, got {type(raw_items).__name__}")
        _reject_nested_expr_tag(raw_items, where="for_each step 'items'")
        items = list(raw_items)
    on_error = body.get("on_error")
    if not isinstance(on_error, str) or (
        on_error not in ("continue", "abort") and _ON_ERROR_RE.match(on_error) is None
    ):
        _fail(
            "for_each step 'on_error': REQUIRED — must be 'continue', 'abort', or "
            f"'retry(n)' (a positive integer n), got {on_error!r}"
        )
    if "do" not in body:
        _fail("for_each step: missing required field 'do'")
    do = _parse_step(body["do"], index=f"{index} (for_each do)")
    if "collect" not in body:
        _fail("for_each step: missing required field 'collect'")
    collect = _parse_step(body["collect"], index=f"{index} (for_each collect)")
    max_parallel = body.get("max_parallel")
    if max_parallel is not None and (
        isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or max_parallel <= 0
    ):
        _fail(f"for_each step 'max_parallel': expected a positive integer, got {max_parallel!r}")
    output = body.get("output")
    if output is not None and not isinstance(output, str):
        _fail(f"for_each step 'output': expected a store-name string, got {type(output).__name__}")
    return ForEachStep(
        do=do, collect=collect, on_error=on_error, over=over, items=items,
        max_parallel=max_parallel, output=output,
    )


_STEP_PARSERS = {
    "transform": _parse_transform_step,
    "tool": _parse_tool_step,
    "shell": _parse_shell_step,
    "agent": _parse_agent_step,
    "call": _parse_call_step,
    "match": _parse_match_step,
}


def _parse_step(raw_step: Any, *, index: "int | str") -> Step:
    if not isinstance(raw_step, dict) or len(raw_step) != 1:
        _fail(
            f"step {index}: expected a single-key mapping naming its step kind "
            f"(one of {_ALL_STEP_KINDS!r}), got {raw_step!r}"
        )
    (kind, body), = raw_step.items()
    if kind in _UNSUPPORTED_STEP_KINDS:
        _fail(
            f"step {index}: step kind {kind!r} is not yet supported (later slice) — "
            f"the executor runs {_SUPPORTED_STEP_KINDS!r}"
        )
    if kind == "fold":
        if not isinstance(body, dict):
            _fail(f"step {index} (fold): expected a mapping body, got {type(body).__name__}")
        return _parse_fold_step(body, index=index)
    if kind == "for_each":
        # Special-dispatched like ``fold``: its ``do``/``collect`` sub-steps recurse
        # through ``_parse_step``, so it needs ``index`` for nested error context.
        if not isinstance(body, dict):
            _fail(f"step {index} (for_each): expected a mapping body, got {type(body).__name__}")
        return _parse_for_each_step(body, index=index)
    if kind not in _STEP_PARSERS:
        _fail(
            f"step {index}: unknown step kind {kind!r} (expected one of "
            f"{_ALL_STEP_KINDS!r})"
        )
    if not isinstance(body, dict):
        _fail(f"step {index} ({kind}): expected a mapping body, got {type(body).__name__}")
    return _STEP_PARSERS[kind](body)


# ---------------------------------------------------------------------------
# Pipeline-document / schema-document parsing
# ---------------------------------------------------------------------------


def _parse_pipeline_doc(doc: "dict[str, Any]") -> Pipeline:
    _reject_unknown_keys(doc, _SUPPORTED_PIPELINE_KEYS, where="pipeline")
    name = doc.get("pipeline")
    if not isinstance(name, str) or not name:
        _fail("pipeline document: 'pipeline' must be a non-empty name string")
    description = doc.get("description", "")
    if not isinstance(description, str):
        _fail(f"pipeline {name!r}: 'description' must be a string, got {type(description).__name__}")
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        _fail(f"pipeline {name!r}: 'steps' must be a non-empty list")
    steps = [_parse_step(s, index=i) for i, s in enumerate(raw_steps)]
    # #2575: carry the declared ``pipeline:`` name on the Pipeline so the disk
    # loader can register under it (authoritative for ``call``/``match``
    # resolution) and it persists through work-order/recovery.
    return Pipeline(steps=steps, description=description, name=name)


def _parse_schema_doc(doc: "dict[str, Any]", schema_registry: SchemaRegistry) -> None:
    name = doc.get("schema")
    if not isinstance(name, str) or not name:
        _fail("schema document: 'schema' must be a non-empty name string")
    _reject_unknown_keys(doc, frozenset({"schema", "fields"}), where=f"schema {name!r}")
    fields = doc.get("fields")
    if not isinstance(fields, dict) or not fields:
        _fail(f"schema {name!r}: 'fields' must be a non-empty mapping")
    schema_registry.register(name, {"fields": fields})


def parse_pipeline_dsl(text: str, schema_registry: SchemaRegistry) -> Pipeline:
    """Parse `text` (one or more `---`-separated YAML documents) into exactly
    one `Pipeline`. Any `schema:`-keyed document found is registered into
    `schema_registry` (mutated in place — the caller passes the SAME
    registry to `PipelineExecutor.run`/`resume` so `schema: REF` references
    resolve). Raises `PipelineParseError` for malformed YAML, a malformed R1
    expression, any construct outside the linear executor's current scope
    (see module docstring), or a text with zero or more than one `pipeline:`
    document."""
    try:
        raw_docs = list(yaml.load_all(text, Loader=_PipelineLoader))
    except yaml.YAMLError as exc:
        raise PipelineParseError(f"invalid YAML: {exc}") from exc

    pipeline_docs: "list[dict[str, Any]]" = []
    for doc in raw_docs:
        if doc is None:
            continue
        if not isinstance(doc, dict):
            _fail(f"top-level document must be a mapping, got {type(doc).__name__}")
        if "schema" in doc:
            _parse_schema_doc(doc, schema_registry)
        elif "pipeline" in doc:
            pipeline_docs.append(doc)
        else:
            for bad_key in _UNSUPPORTED_PIPELINE_KEYS:
                if bad_key in doc:
                    _fail(
                        f"top-level document has {bad_key!r} but no 'pipeline' name — "
                        f"{bad_key!r} is also not yet supported (later slice) even "
                        "when a 'pipeline' key is present"
                    )
            _fail(
                "top-level document must have either a 'schema' or a 'pipeline' "
                f"key, got keys {sorted(doc)!r}"
            )

    if len(pipeline_docs) != 1:
        _fail(
            f"expected exactly one 'pipeline:' document, found {len(pipeline_docs)}"
        )
    return _parse_pipeline_doc(pipeline_docs[0])
