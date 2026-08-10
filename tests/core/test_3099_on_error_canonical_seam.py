"""Tier 2: OS invariant — #3099 corrective (a): a `for_each`/`parallel`
`on_error` trigger fires on a `tool:` step's CANONICAL error result
(`meta.isError`), not just a raised exception.

Background (#3096/#3098/#3099): `execute_op` (op_runtime's shared chokepoint)
NEVER raises for an op-level failure by design — a `PermissionError` degrades
to `{status:"denied", error:...}`, any other exception to
`{status:"error", error:...}` (`reyn/core/op_runtime/__init__.py`). Before
this fix, `_run_item`/`_run_branch` (the `for_each`/`parallel` fan-out
coordinators in `reyn/core/pipeline/executor.py`) only tripped `on_error` on a
RAISED exception — a `tool:` step that returned one of these degraded results
NORMALLY (no exception) sailed through as an ordinary "success", so a
declared `on_error: abort`/`continue`/`retry(n)` never engaged (#3095's root:
`rag_ingest.yaml`'s `glob_files` for_each flowed a permission-denied item into
a `fold` that assumed every item's `.structured` was a list, and broke with an
opaque `list + dict` TypeError several steps downstream of the real cause).
#3098 landed an interim per-call-site `schema: PreflightCheck` gate to force a
raise; this corrective closes it at the SOURCE instead — the executor now ALSO
consumes the tool result's own canonical error signal.

That signal is NOT new: FP-0056 v2's shared error seam
(`reyn.core.offload.canonical.is_error_result`/`_is_error`) already computes
it for every `tool:` step's raw dispatch result inside `_run_tool_step`
(`to_canonical`), and surfaces it as `meta.isError` on the `ctx_result` a
fan-out coordinator receives (`_run_tool_step`'s return value is the
CONVERTED ctx-shaped dict, not the raw envelope — `status`/`isError` no
longer exist at the top level by the time `_run_item`/`_run_branch` see it;
`meta.isError` is what survives the conversion). This is the SAME field a
handle-inline pipeline step already branches on manually
(`rag_ingest.yaml`: `get(ctx.converted, 'meta.isError', false)`) — the fix
makes a DECLARED `on_error` consume it automatically too, without inventing
any new field (`reyn.core.pipeline.executor._tool_step_canonical_error`).

Real production `to_canonical`/`error_to_canonical`/`file_to_canonical`
throughout (no mocks) — a dispatch result carrying a genuine
`_canonical_source: "file"` tag with a `status: "denied"` + `error` shape
(the EXACT shape `execute_op`'s own `PermissionError` handler produces, and
the EXACT shape #3098's real end-to-end `glob_files`-against-an-unpermitted-
folder test (`tests/test_fp0063_p3_rag_pipelines.py::
test_ingest_file_discovery_aborts_clean_on_unreadable_input_path`) exercises
through the real op_runtime chokepoint) drives every test here — no invented
result shape.
"""
from __future__ import annotations

from typing import Any

import pytest

# Import the real op_runtime `file` module so its canonical declaration
# (`file_to_canonical`) is registered — the same registration every real
# `glob_files`/`read_file`/... call goes through in production.
import reyn.core.op_runtime.file  # noqa: F401
from reyn.core.pipeline.executor import (
    ExprRef,
    ForEachStep,
    ParallelStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)


def _denied_file_result(pattern: str) -> dict:
    """The REAL shape `execute_op`'s `PermissionError` handler produces for a
    genuinely-registered `file`-kind op (`status: "denied"`, NOT `"error"` —
    the exact shape a real `glob_files` permission-denial takes, per
    `reyn/core/op_runtime/__init__.py`'s `except PermissionError` branch)."""
    return {
        "_canonical_source": "file",
        "kind": "file",
        "op": "glob",
        "status": "denied",
        "error": f"not permitted: {pattern}",
    }


# ── for_each: on_error now fires on a canonical-error NON-raising result ────


@pytest.mark.asyncio
async def test_for_each_on_error_abort_fires_on_canonical_error_without_raising():
    """Tier 2: a `tool:` step that returns NORMALLY (no exception) but carries
    a canonical error result (`meta.isError` — the real `file`-op permission-
    denied shape) trips `on_error: abort` exactly like a raised exception
    would."""
    def _dispatch(name: str, args: dict) -> Any:
        assert name == "glob_files"
        pattern = args["pattern"]
        if pattern == "bad/**":
            return _denied_file_result(pattern)
        return {"ok": True}

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["good/**", "bad/**"],
            on_error="abort",
            do=ToolStep(name="glob_files", args={"pattern": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError, match="canonical error"):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-3099-fe-abort",
        )


@pytest.mark.asyncio
async def test_for_each_on_error_continue_drops_canonical_error_item():
    """Tier 2: `on_error: continue` DROPS a canonical-error item the same way
    it drops a raised-exception item (a kind-marker, collect sees survivors
    only) — the trigger widening does not change the on_error POLICY
    semantics, only what counts as a failure."""
    def _dispatch(name: str, args: dict) -> Any:
        pattern = args["pattern"]
        if pattern == "bad/**":
            return _denied_file_result(pattern)
        return {"ok": True}

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["good/**", "bad/**"],
            on_error="continue",
            do=ToolStep(name="glob_files", args={"pattern": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-3099-fe-continue",
    )
    assert result.pipe_data == [{"text": "", "structured": {"ok": True}}]
    dropped = result.completed_step_results["0.for_each.1"]
    assert dropped["__fan_out_dropped__"] is True
    assert "not permitted" in dropped["error"]


# ── parallel: same trigger widening over NAMED branches ─────────────────────


@pytest.mark.asyncio
async def test_parallel_on_error_abort_fires_on_canonical_error_without_raising():
    """Tier 2: the `parallel` fan-out coordinator (`_run_branch`) gets the
    SAME canonical-error trigger as `for_each`'s `_run_item` — a branch that
    returns a canonical-error result normally trips `on_error: abort`."""
    def _dispatch(name: str, args: dict) -> Any:
        if args["pattern"] == "bad/**":
            return _denied_file_result(args["pattern"])
        return {"ok": True}

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="abort",
            branches={
                "good": ToolStep(name="glob_files", args={"pattern": "good/**"}),
                "bad": ToolStep(name="glob_files", args={"pattern": "bad/**"}),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError, match="canonical error"):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-3099-par-abort",
        )


# ── handle-inline preserved: a step WITHOUT on_error is unaffected ──────────


@pytest.mark.asyncio
async def test_plain_tool_step_without_on_error_still_surfaces_meta_iserror_inline():
    """Tier 2: a step that does NOT sit inside a `for_each`/`parallel`
    `on_error` (i.e. an ordinary top-level `tool:` step, the handle-inline
    pattern rag_ingest.yaml's `mcp_convert`/`meta.isError` branch relies on)
    is COMPLETELY UNAFFECTED by this fix — it still returns its
    `meta.isError`-flagged ctx dict NORMALLY, with no raise/abort, so a later
    step can branch on it itself. #3099 corrective (a) only widens the
    trigger INSIDE a DECLARED for_each/parallel on_error; it must not turn a
    plain tool step into an auto-aborting one."""
    def _dispatch(name: str, args: dict) -> Any:
        return _denied_file_result(args["pattern"])

    pipeline = Pipeline(steps=[
        ToolStep(name="glob_files", args={"pattern": "bad/**"}),
        # A later step reads the flag inline — proves it survived, unraised.
        TransformStep(value="get(pipe, 'meta.isError', false)"),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-3099-inline",
    )
    assert result.pipe_data is True


@pytest.mark.asyncio
async def test_non_tool_step_inside_for_each_on_error_is_unaffected():
    """Tier 2: the canonical-error check is scoped to `ToolStep` ONLY — a
    `transform`-do inside a `for_each` (which never returns a
    canonical-envelope shape) is unaffected by the new check; its own
    existing exception-only on_error behavior is preserved byte-for-byte."""
    pipeline = Pipeline(steps=[
        ForEachStep(
            items=[1, 2],
            on_error="abort",
            # A dict result that HAPPENS to carry a `meta` key with `isError`
            # would previously (and still) never trip on_error for a
            # transform-do — the check only ever looks at ToolStep results.
            do=TransformStep(value="{meta: {isError: true}, value: item}"),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=lambda *_a, **_k: None,
        state_log=None, run_id="run-3099-transform-unaffected",
    )
    assert result.pipe_data == [
        {"meta": {"isError": True}, "value": 1},
        {"meta": {"isError": True}, "value": 2},
    ]
