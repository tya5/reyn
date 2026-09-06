"""Tier 2: OS invariant — #5865 folds the pipeline `tool:` step's dispatch
(``tools/pipeline_verbs._make_tool_dispatch``) onto ``dispatch_tool``
(``core/dispatch/dispatcher.py``), so the call-time TOOL-axis restrict
predicate (#5841/#5854's 2b) has exactly ONE call site left, and a pipeline
step gains the same ``tool_called``/``tool_returned``/``tool_failed`` audit
trail every other ``dispatch_tool`` caller already had.

Pre-#5865, ``_make_tool_dispatch``'s own closure called ``target.handler``
directly, with no audit events at all, and read ``tool_contextually_denied``
at its own separate site (#3546). #3546's own regression suite
(``tests/runtime/test_3546_pipeline_driver_narrowing_inheritance.py``) still
covers "a narrowing-denied tool's real side effect does not happen" through
the real driver/session machinery — this file is scoped to the NEW
properties the fold adds (audit events, ``caller_kind="pipeline"``) and to
the raise/return contract the fold has to preserve exactly (see
``_DISPATCH_TOOL_RAISING_KINDS``'s own docstring in ``pipeline_verbs.py``),
using the lightweight ``_make_tool_dispatch`` surface directly rather than a
full pipeline run.
"""
from __future__ import annotations

import pytest

import reyn.tools as tools_pkg
from reyn.core.events.events import EventLog
from reyn.core.pipeline.executor import PipelineExecutionError
from reyn.security.permissions.effective import ContextualPermission
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.types import ToolContext, ToolDefinition, ToolGates

_TOOL_NAME = "p5865_test_tool"


def _install_tool(monkeypatch, handler) -> None:
    """A REAL ``ToolDefinition`` registered on a real (monkeypatched-fresh)
    registry — the same idiom
    ``test_3546_pipeline_driver_narrowing_inheritance.py``'s own
    ``_install_side_effect_tool`` uses, generalized to take any handler so
    each test below can install the exact behaviour (success / self-declared
    error / raise) it needs to observe through the fold."""
    tool = ToolDefinition(
        name=_TOOL_NAME,
        description="#5865 test tool.",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"),
        handler=handler,
        category="io",
        purity="side_effect",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _tool_ctx(events: EventLog) -> ToolContext:
    return ToolContext(
        events=events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        agent_name="worker",
    )


def _events_by_type(sink: "list") -> "dict[str, list[dict]]":
    """Group the captured events' ``data`` payloads by ``type``."""
    grouped: "dict[str, list[dict]]" = {}
    for event in sink:
        grouped.setdefault(event.type, []).append(event.data)
    return grouped


@pytest.mark.asyncio
async def test_a_pipeline_tool_step_now_emits_the_dispatch_tool_audit_trail(
    monkeypatch,
) -> None:
    """Tier 2: a successful pipeline tool-step dispatch emits `tool_called` then
    `tool_returned`, carrying `caller_kind="pipeline"` and no `chain_id`
    (there is no litellm tool_calls round behind a pipeline step) — the
    trail `_make_tool_dispatch` never produced before #5865."""

    async def _handler(args, ctx):
        return {"echoed": args.get("tag")}

    _install_tool(monkeypatch, _handler)
    sink: "list" = []
    events = EventLog(subscribers=[sink.append])
    dispatch = _make_tool_dispatch(_tool_ctx(events))

    result = await dispatch(_TOOL_NAME, {"tag": "hello"})
    await events.drain()

    assert result["echoed"] == "hello"
    grouped = _events_by_type(sink)
    assert len(grouped.get("tool_called", [])) == 1
    assert len(grouped.get("tool_returned", [])) == 1
    assert "tool_failed" not in grouped
    called = grouped["tool_called"][0]
    assert called["caller_kind"] == "pipeline"
    assert called["tool"] == _TOOL_NAME
    assert called["chain_id"] is None


@pytest.mark.asyncio
async def test_contextual_deny_still_raises_and_now_also_emits_tool_failed(
    monkeypatch,
) -> None:
    """Tier 2: the TOOL-axis restrict is still enforced for a pipeline step post-fold
    (a real `ContextualPermission` denying `_TOOL_NAME`, driven through
    `_make_tool_dispatch`'s own `contextual_permission=` parameter exactly
    as `PipelineExecutorDriver._make_dispatch` passes it) — pre-#5865 this
    raised with no audit trail at all; it must still raise (the pre-#5865
    contract this closure's callers depend on), and now additionally leaves
    a `tool_failed` record naming the real reason."""

    async def _handler(args, ctx):
        raise AssertionError("denied tool's handler must never run")

    _install_tool(monkeypatch, _handler)
    sink: "list" = []
    events = EventLog(subscribers=[sink.append])
    denied = ContextualPermission(tool_deny=frozenset({_TOOL_NAME}))
    dispatch = _make_tool_dispatch(_tool_ctx(events), contextual_permission=denied)

    with pytest.raises(PipelineExecutionError):
        await dispatch(_TOOL_NAME, {})
    await events.drain()

    grouped = _events_by_type(sink)
    assert "tool_called" not in grouped, (
        "a denied call must not be recorded as having been called at all — "
        "dispatch_tool's own 2b check runs BEFORE the tool_called emit"
    )
    (failed,) = grouped["tool_failed"]
    assert failed["error_kind"] == "tool_excluded"


@pytest.mark.asyncio
async def test_an_unnarrowed_pipeline_step_is_unaffected_by_the_contextual_gate(
    monkeypatch,
) -> None:
    """Tier 2: positive control for the deny test above: `contextual_permission=None`
    (what the `reyn pipe` CLI passes — an operator-direct run with no
    session envelope) must leave the SAME tool callable — the fold's own
    documented "byte-identical when unnarrowed" guarantee."""

    async def _handler(args, ctx):
        return {"ran": True}

    _install_tool(monkeypatch, _handler)
    events = EventLog()
    dispatch = _make_tool_dispatch(_tool_ctx(events), contextual_permission=None)

    result = await dispatch(_TOOL_NAME, {})

    assert result["ran"] is True


@pytest.mark.asyncio
async def test_a_raised_handler_exception_still_propagates_not_a_silent_result(
    monkeypatch,
) -> None:
    """Tier 2: pre-#5865, a raised handler exception propagated straight out of
    `_dispatch` (uncaught by anything in `pipeline_verbs.py`). `dispatch_tool`
    itself CATCHES that raise into a returned `{"status": "error", ...}`
    envelope — folding onto it naively would silently turn every pipeline
    tool-handler crash into a normal step result, making an `on_error: None`
    (the default, unset) step swallow a crash instead of aborting the run.
    The fold must re-raise (see `_DISPATCH_TOOL_RAISING_KINDS`), now
    uniformly as `PipelineExecutionError` — a disclosed narrowing of the
    exception TYPE, never of whether it raises at all."""

    async def _handler(args, ctx):
        raise ValueError("boom")

    _install_tool(monkeypatch, _handler)
    sink: "list" = []
    events = EventLog(subscribers=[sink.append])
    dispatch = _make_tool_dispatch(_tool_ctx(events))

    with pytest.raises(PipelineExecutionError, match="boom"):
        await dispatch(_TOOL_NAME, {})
    await events.drain()

    (failed,) = _events_by_type(sink)["tool_failed"]
    assert failed["error_kind"] == "exception"


@pytest.mark.asyncio
async def test_a_handler_declared_error_still_returns_normally_not_raising(
    monkeypatch,
) -> None:
    """Tier 2: the one case that must NOT raise: a handler that returns NORMALLY
    with its own self-declared error envelope (the exact shape
    `run_pipeline`'s own handler uses, per `_handler_declared_error`'s
    docstring in `dispatcher.py`) — pre-#5865 this was returned as-is,
    letting `_run_tool_step`'s own canonicalization apply `on_error` policy,
    and an UNSET `on_error` (the default) returns such a result UNCHECKED
    rather than aborting the pipeline. `dispatch_tool`'s own 5b promotes
    this to its outer error envelope; folding that straight through
    unflattened would raise (wrong) or render a Python dict repr as the
    tool's text (also wrong) — this test pins the flattened, non-raising
    shape `_dispatch` reconstructs instead."""

    async def _handler(args, ctx):
        return {"status": "error", "error": {"kind": "custom_kind", "message": "nope"}}

    _install_tool(monkeypatch, _handler)
    sink: "list" = []
    events = EventLog(subscribers=[sink.append])
    dispatch = _make_tool_dispatch(_tool_ctx(events))

    result = await dispatch(_TOOL_NAME, {})  # must not raise
    await events.drain()

    assert result["status"] == "error"
    assert result["error"] == "nope"
    assert result["error_kind"] == "custom_kind"
    assert result["_canonical_source"] == _TOOL_NAME
    (failed,) = _events_by_type(sink)["tool_failed"]
    assert failed["error_kind"] == "custom_kind"


@pytest.mark.asyncio
async def test_the_structural_nesting_deny_is_unaffected_by_the_fold(
    monkeypatch,
) -> None:
    """Tier 2: `_PIPELINE_STEP_DENY_TOOLS` (R6 S3 — a step must not launch a nested
    pipeline) is checked BEFORE dispatch_tool is ever reached, unchanged by
    this fold: it must still raise, and must emit NO dispatch_tool audit
    event at all (there was never a real dispatch attempt)."""
    sink: "list" = []
    events = EventLog(subscribers=[sink.append])
    dispatch = _make_tool_dispatch(_tool_ctx(events))

    with pytest.raises(PipelineExecutionError, match="structurally denied"):
        await dispatch("run_pipeline", {"name": "whatever"})
    await events.drain()

    assert sink == []
