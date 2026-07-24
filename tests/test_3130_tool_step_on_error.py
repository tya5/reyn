"""Tier 2: OS invariant — a top-level ``tool:`` step's ``on_error`` (#3130).

Closes the "last piece" gap #3096/#3105 left open (per the architect, the
original designer of the 6-defects-ONE-gap framing): the typed canonical-error
seam (``meta.isError`` via ``reyn.core.offload.canonical``) was already
consumed by the FAN-OUT steps (``for_each``/``parallel``'s ``on_error``), but
a single top-level ``tool:`` step could only abort on a RAISED exception — a
canonical-error RESULT (e.g. an MCP tool's ``isError`` branch) passed through
silently, requiring a ``schema: PreflightCheck`` crutch to react to it.

This adds ``ToolStep.on_error`` (``continue``/``abort``/``retry(n)``),
reusing the exact for_each/parallel parse shape (``_ON_ERROR_RE``) and the
SAME ``_parse_on_error``/retry mechanism at the executor layer:

  1. ``abort`` (the *implicit* default — ``on_error`` OMITTED) is
     byte-identical to pre-#3130 behavior: a RAISED exception still aborts
     the pipeline (unchanged), and this test also covers the now-available
     EXPLICIT ``on_error: abort``, which additionally aborts on a canonical
     ``meta.isError`` RESULT (the new, opt-in behavior this issue asks for).
  2. ``continue`` binds a typed error-envelope to the step's ``output`` (the
     SAME ``{text, structured, meta: {isError: true}}`` shape a canonical
     tool error already takes, or its exception-derived equivalent via
     ``error_to_canonical``) — NOT a for_each-style dropped/null binding,
     since a single step's ``output`` is a NAMED binding downstream steps
     reference, unlike a fan-out COLLECTION item. The pipeline proceeds and
     a downstream step can discriminate the envelope via ``meta.isError``.
  3. ``retry(n)``: a flaky tool that fails then succeeds within ``n``
     retries ends up succeeding (bound normally, no error envelope); a tool
     that fails ALL ``n`` retries falls through to abort (mirroring
     ``for_each``'s hard-coded "retry exhausted -> abort" fallback — tested
     against BOTH an ``on_error: retry(n)`` config, which aborts on
     exhaustion, and an ``on_error: continue`` config with no retries, which
     never aborts at all).

Real ``PipelineExecutor`` + a real (non-mocked) ``tool_dispatch`` callable
throughout — no mocks; ``state_log=None``/``run_id`` mirrors
``test_pipeline_for_each_primitive.py``'s own on_error harness exactly.
"""
from __future__ import annotations

from typing import Any

import pytest

from reyn.core.pipeline.executor import (
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)

# ── abort (implicit default: on_error omitted) — byte-identical ─────────────


@pytest.mark.asyncio
async def test_on_error_unset_a_raised_exception_still_aborts():
    """Tier 2: ``on_error`` OMITTED (the field's default, ``None``) — a tool
    that RAISES still aborts the pipeline exactly like before #3130 (no
    behavior change for the byte-identical "unset" case)."""
    def _dispatch(name: str, args: dict) -> Any:
        raise RuntimeError("boom")

    pipeline = Pipeline(steps=[ToolStep(name="work", args={})])
    # today's exact behavior: the raw exception propagates UNWRAPPED (never a
    # PipelineExecutionError) — nothing in the dispatch path catches it when
    # on_error is unset.
    with pytest.raises(RuntimeError, match="boom"):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-tool-unset-raise",
        )


@pytest.mark.asyncio
async def test_on_error_unset_a_canonical_error_result_passes_through_unchecked():
    """Tier 2: ``on_error`` OMITTED — a tool that returns NORMALLY with a
    canonical-error result (``meta.isError``) does NOT abort — this is the
    exact pre-#3130 behavior the schema-crutch pattern relies on, preserved
    byte-identical when the field is unset."""
    def _dispatch(name: str, args: dict) -> Any:
        return {"content": [{"type": "text", "text": "tool failed"}], "isError": True}

    pipeline = Pipeline(steps=[
        ToolStep(name="work", args={}, output="result"),
        TransformStep(value="ctx.result.meta.isError", output="flag"),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-tool-unset-canonical",
    )
    # the pipeline ran to completion (no abort) and the canonical error is
    # inspectable downstream exactly like today's schema-crutch pattern needs.
    assert result.pipe_data is True


@pytest.mark.asyncio
async def test_on_error_explicit_abort_also_fails_on_canonical_error_result():
    """Tier 2: an EXPLICIT ``on_error: abort`` is the NEW opt-in behavior this
    issue asks for — it aborts on a canonical-error RESULT too (not just a
    raised exception), closing the schema-crutch gap natively."""
    def _dispatch(name: str, args: dict) -> Any:
        return {"error": "tool failed", "isError": True}

    pipeline = Pipeline(steps=[ToolStep(name="work", args={}, on_error="abort")])
    with pytest.raises(PipelineExecutionError) as exc_info:
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-tool-explicit-abort",
        )
    assert "tool failed" in str(exc_info.value)


# ── continue: typed error-envelope binding ───────────────────────────────────


@pytest.mark.asyncio
async def test_on_error_continue_binds_error_envelope_from_canonical_result():
    """Tier 2: ``on_error: continue`` on a canonical-error RESULT binds that
    SAME error-envelope shape to ``output`` (not null, not dropped) — the
    pipeline proceeds and a downstream step can discriminate it via
    ``meta.isError``, symmetric with a success result's shape."""
    def _dispatch(name: str, args: dict) -> Any:
        return {"error": "tool failed", "isError": True}

    pipeline = Pipeline(steps=[
        ToolStep(name="work", args={}, on_error="continue", output="result"),
        TransformStep(
            value="get(ctx.result, 'meta.isError', false)",
            output="discriminated",
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-tool-continue-canonical",
    )
    assert result.named_stores["result"]["meta"]["isError"] is True
    assert "tool failed" in result.named_stores["result"]["text"]
    # downstream step DISCRIMINATES the bound envelope from a success result.
    assert result.pipe_data is True


@pytest.mark.asyncio
async def test_on_error_continue_binds_error_envelope_from_raised_exception():
    """Tier 2: ``on_error: continue`` on a RAISED exception (no dict result to
    canonicalize) synthesizes the SAME typed error-envelope shape via
    ``error_to_canonical`` — not a second, invented shape — so downstream code
    discriminates a raised-exception failure identically to a canonical-error
    RESULT failure."""
    def _dispatch(name: str, args: dict) -> Any:
        raise RuntimeError("boom")

    pipeline = Pipeline(steps=[
        ToolStep(name="work", args={}, on_error="continue", output="result"),
        TransformStep(value="ctx.result.meta.isError", output="flag"),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-tool-continue-exception",
    )
    assert result.named_stores["result"]["meta"]["isError"] is True
    assert "boom" in result.named_stores["result"]["text"]
    assert result.pipe_data is True


# ── retry(n) ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_on_error_retry_reruns_flaky_step_until_success():
    """Tier 2: ``on_error: retry(2)`` on a step that fails twice then succeeds
    on the 3rd attempt ends up succeeding — bound normally (no error
    envelope), reusing the SAME retry-safety model ``for_each.on_error:
    retry(n)`` already relies on (a retry re-invokes the tool body)."""
    attempts = {"n": 0}

    def _dispatch(name: str, args: dict) -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return "recovered"

    pipeline = Pipeline(steps=[ToolStep(name="work", args={}, on_error="retry(2)")])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-tool-retry-ok",
    )
    assert result.pipe_data == {"text": "recovered"}
    assert attempts["n"] == 3  # 1 initial + 2 retries


@pytest.mark.asyncio
async def test_on_error_retry_exhausted_falls_back_to_abort():
    """Tier 2: ``on_error: retry(1)`` on an ALWAYS-failing step exhausts its
    retries and falls back to ABORT (mirroring ``for_each``'s hard-coded
    "retry exhausted -> abort" — there is no combined "retry then continue"
    DSL value)."""
    calls = {"n": 0}

    def _dispatch(name: str, args: dict) -> Any:
        calls["n"] += 1
        raise RuntimeError("permanent")

    pipeline = Pipeline(steps=[ToolStep(name="work", args={}, on_error="retry(1)")])
    with pytest.raises(PipelineExecutionError):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-tool-retry-exhaust",
        )
    assert calls["n"] == 2  # 1 initial + 1 retry, then abort


@pytest.mark.asyncio
async def test_on_error_continue_never_aborts_even_without_retries():
    """Tier 2: contrast with the retry-exhausted-abort case above — an
    always-failing step configured with ``on_error: continue`` (no retries at
    all) never aborts; it binds the error-envelope on the FIRST failure. Two
    on_error configs over the same always-failing tool land on opposite
    outcomes (abort vs continue), exactly per each config's own semantics."""
    def _dispatch(name: str, args: dict) -> Any:
        raise RuntimeError("permanent")

    pipeline = Pipeline(steps=[
        ToolStep(name="work", args={}, on_error="continue", output="result"),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-tool-continue-no-retry",
    )
    assert result.named_stores["result"]["meta"]["isError"] is True
