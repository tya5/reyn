"""Tier 1/2: #2425 PR-2 — pipeline `tool:` step results expose the same
`text`/`structured`/`meta` ctx shape chat gets, uniformly across op kinds,
and NEVER go through the chat-side offload/size-gate machinery.

Covers:
  1. Tier 1: `canonical_to_ctx_fields` reduces `CanonicalToolResult.attachments`
     to `structured` (absent / single item / list) exactly like `seam.py`'s own
     reduction, minus any size gating.
  1b. Tier 1 (#2966): the same reducer exposes the producer's SIGNAL channel as
     `meta`, absent-when-empty on the same convention `structured` uses. Chat
     always saw `meta` (`seam.py` reads it); the pipeline did not, so a `tool:`
     step could read a producer's body but never its signal — which forced
     FP-0063's ingest pipeline to stamp each chunk's `embedding_model` from its
     own INPUT (a model-CLASS alias) instead of from the model that actually
     produced the vectors, i.e. the "column becomes a lie" failure FP-0057's C1
     gate exists to prevent.
  2. Tier 2: a real `PipelineExecutor.run` through `_run_tool_step` exposes
     `ctx.<name>.text`/`.structured` uniformly for TWO distinct real op kinds
     (`mcp`, `sandboxed_exec`) via the REAL R1 expression evaluator.
  3. Tier 2: a dispatch-envelope-wrapped result (the `run_pipeline`-style
     `{"status": "ok", "data": {"kind": ..., ...}}` shape a tool-registry
     handler can itself return) unwraps correctly before canonicalization.
  4. Tier 2 falsify (owner hard rule): a structured payload far larger than
     `seam.STRUCTURED_INLINE_MAX_CHARS` still lands FULLY INLINE in
     `ctx.<name>.structured` — pipeline ctx is never offloaded/size-gated,
     unlike the chat-side `build_offload_body` path.

No mocks — real `PipelineExecutor`, real `to_canonical`, real R1 evaluator.
"""
from __future__ import annotations

import pytest

from reyn.core.offload.canonical import canonical_to_ctx_fields
from reyn.core.offload.seam import STRUCTURED_INLINE_MAX_CHARS
from reyn.core.pipeline.executor import (
    ExprRef,
    Pipeline,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)


def test_canonical_to_ctx_fields_structured_absent_when_no_attachments():
    """Tier 1: no structured attachments → the ctx dict has no 'structured' key at all."""
    fields = canonical_to_ctx_fields({"text": "hi", "attachments": [], "source_ref": None, "meta": {}})
    assert fields == {"text": "hi"}
    assert "structured" not in fields


def test_canonical_to_ctx_fields_single_structured_attachment_unwraps():
    """Tier 1: exactly one structured attachment → 'structured' is the bare value, not a
    single-item list."""
    canonical = {
        "text": "",
        "attachments": [{"kind": "structured", "data": {"a": 1}}],
        "source_ref": None,
        "meta": {},
    }
    assert canonical_to_ctx_fields(canonical) == {"text": "", "structured": {"a": 1}}


def test_canonical_to_ctx_fields_multiple_structured_attachments_become_a_list():
    """Tier 1: 2+ structured attachments → 'structured' is the list of their data,
    mirroring seam.py's own reduction convention."""
    canonical = {
        "text": "",
        "attachments": [
            {"kind": "structured", "data": {"a": 1}},
            {"kind": "structured", "data": {"b": 2}},
        ],
        "source_ref": None,
        "meta": {},
    }
    assert canonical_to_ctx_fields(canonical) == {
        "text": "", "structured": [{"a": 1}, {"b": 2}],
    }


def test_canonical_to_ctx_fields_ignores_media_attachments():
    """Tier 1: a media attachment (no chat-side rendering target in pipeline ctx)
    never appears in 'structured' — only 'kind': 'structured' items are collected."""
    canonical = {
        "text": "body",
        "attachments": [{"kind": "media", "block": {"type": "image"}}],
        "source_ref": None,
        "meta": {},
    }
    assert canonical_to_ctx_fields(canonical) == {"text": "body"}


def test_canonical_to_ctx_fields_exposes_meta_signal_fields():
    """Tier 1: #2966 — a producer's signal meta reaches pipeline ctx as 'meta',
    so a step can read what a result COST / HOW it was produced — not just its
    body. Pinned with embed's own field set, the motivating producer."""
    canonical = {
        "text": "",
        "attachments": [{"kind": "structured", "data": [[0.1, 0.2]]}],
        "source_ref": None,
        "meta": {"model": "text-embedding-3-small", "total_tokens": 42, "cost_usd": 0.001},
    }
    assert canonical_to_ctx_fields(canonical) == {
        "text": "",
        "structured": [[0.1, 0.2]],
        "meta": {"model": "text-embedding-3-small", "total_tokens": 42, "cost_usd": 0.001},
    }


def test_canonical_to_ctx_fields_meta_absent_when_producer_emits_no_signal():
    """Tier 1: #2966 — no signal meta → no 'meta' key at all (the same
    absent-when-empty convention 'structured' uses). Load-bearing: a step
    reading ctx.<name>.meta.<field> on a signal-less producer must RAISE
    rather than silently read {} and default — see pipeline-dsl.md."""
    assert canonical_to_ctx_fields(
        {"text": "hi", "attachments": [], "source_ref": None, "meta": {}}
    ) == {"text": "hi"}


@pytest.mark.asyncio
async def test_tool_step_ctx_uniform_text_structured_across_two_distinct_op_kinds():
    """Tier 2: two REAL, distinct op-kind result shapes (mcp / sandboxed_exec) both
    resolve via the SAME `ctx.<name>.text` / `ctx.<name>.structured` R1 paths —
    proving the ctx shape is uniform across op kinds, not per-kind bespoke."""

    def _dispatch(name: str, args: dict) -> dict:
        if name == "mcp_call":
            return {
                "kind": "mcp",
                "content": "mcp said hi",
                "structured": {"echo": args.get("msg")},
                "_canonical_source": "mcp",
            }
        if name == "run_shell":
            return {"kind": "sandboxed_exec", "stdout": "shell said hi", "returncode": 0,
                    "_canonical_source": "sandboxed_exec"}
        raise AssertionError(f"unexpected tool {name!r}")

    pipeline = Pipeline(steps=[
        ToolStep(name="mcp_call", args={"msg": "hi"}, output="mcp_result"),
        ToolStep(name="run_shell", args={}, output="shell_result"),
        TransformStep(
            value=(
                "ctx.mcp_result.text + '|' + ctx.mcp_result.structured.echo"
                " + '|' + ctx.shell_result.text"
            ),
            output="combined",
        ),
    ])

    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-2425-uniform-shape",
    )

    assert result.named_stores["combined"] == "mcp said hi|hi|shell said hi"
    # sandboxed_exec's zero returncode carries no signal → dropped, so
    # structured is absent entirely for that step's ctx value.
    assert "structured" not in result.named_stores["shell_result"]


@pytest.mark.asyncio
async def test_tool_step_unwraps_dispatch_envelope_before_canonicalizing():
    """Tier 2: a tool-registry handler's own `{"status": "ok", "data": {"kind": ...}}`
    envelope (the `run_pipeline` return shape) unwraps to its inner `kind`-bearing
    dict BEFORE `to_canonical` dispatches — otherwise it would fall through to the
    unregistered-kind whole-dict fallback instead of the real mapper."""

    def _dispatch(name: str, args: dict) -> dict:
        return {"status": "ok", "data": {"kind": "sandboxed_exec", "stdout": "enveloped", "returncode": 0},
                "_canonical_source": "sandboxed_exec"}

    pipeline = Pipeline(steps=[
        ToolStep(name="run_shell", args={}, output="r"),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-2425-envelope-unwrap",
    )
    # Unwrapped + mapped via the REAL sandboxed_exec mapper (text == stdout) — NOT
    # the unregistered-kind fallback (which would have wrapped the whole envelope
    # dict, including "status"/"data", as a structured attachment instead).
    assert result.named_stores["r"] == {"text": "enveloped"}


@pytest.mark.asyncio
async def test_tool_step_ctx_never_offloads_oversized_structured_data():
    """Tier 2: FALSIFY (owner hard rule) — a structured payload far exceeding
    seam.py's STRUCTURED_INLINE_MAX_CHARS still lands FULLY INLINE in
    ctx.<name>.structured — pipeline `_run_tool_step` must call bare
    `to_canonical` (shape only), never `seam.build_offload_body` (which would
    replace an oversized value with a file-ref dict here)."""
    big_items = list(range(STRUCTURED_INLINE_MAX_CHARS))  # str() of this vastly exceeds the cap

    def _dispatch(name: str, args: dict) -> dict:
        return {"kind": "mcp", "content": "", "structured": {"items": big_items},
                "_canonical_source": "mcp"}

    pipeline = Pipeline(steps=[ToolStep(name="big_call", args={}, output="r")])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-2425-no-offload",
    )

    structured = result.named_stores["r"]["structured"]
    # The full list survives verbatim — no truncation, no offload-ref substitution
    # (an offloaded value would be a small dict with a path/ref marker, not this list).
    assert structured == {"items": big_items}
    assert len(structured["items"]) == STRUCTURED_INLINE_MAX_CHARS
