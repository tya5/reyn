"""Tier 1/2: #3104 — ``_fallback_structured`` stamps ``meta.isError`` (+ non-empty ``text``) on an
op-level ERROR result, closing the census gap #3105 (corrective (a)) flagged during co-vet.

Background (#3099 → #3105 → #3104): #3099's corrective (a) taught ``for_each``/``parallel``
``on_error`` to consume the ALREADY-COMPUTED canonical error signal (``meta.isError`` on a
``tool:`` step's converted ``ctx_result`` — ``reyn.core.pipeline.executor._tool_step_canonical_error``).
That signal comes from ``reyn.core.offload.canonical.to_canonical``, whose dispatch (:func:`to_canonical`
in ``canonical.py``) EXITS via ``_fallback_structured`` for a declared ``STRUCTURED_PASSTHROUGH``
producer or a genuinely-unregistered ``source`` BEFORE ``is_error_result``/``error_to_canonical`` ever
runs — so an op-level failure on one of the 9 ``STRUCTURED_PASSTHROUGH`` admin/install verbs
(``mcp_drop_server``/``mcp_install``/``mcp_subscribe_resource``/``mcp_unsubscribe_resource``/
``pipeline_install``/``plugin_install``/``plugin_uninstall``/``presentation_install``/``skill_install``)
— or ``None``/unregistered, ``CANONICAL_TODO``, or a mapper's discriminator-miss fallback — never got
``meta.isError`` stamped, so a declared ``on_error`` fan-out over one of these ops silently never
triggered (the same class of gap #3099 closed for MAPPED producers, left open here — #3105's co-vet
flagged it as REQUIRED, user-pipeline reachable via ``on_error`` fan-out over these verbs).

The fix (architect design, #3104 issue comment) is a CHOKEPOINT stamp, not a dispatch reorder (the
higher-risk FP-0056 dispatch-reorder alternative was explicitly rejected): ``_fallback_structured``
itself now stamps ``meta.isError`` + the extracted error message on its ERROR branch only (``_is_error``
— the SAME predicate ``error_to_canonical`` uses), leaving the SUCCESS branch (``text=""``, ``meta={}``)
byte-for-byte unchanged. Because every one of the 9 ops + ``None``/unregistered + ``CANONICAL_TODO`` +
discriminator-miss ALL funnel through this one function (``to_canonical``'s three call sites at
:1680/:1684/:1693 in ``canonical.py``), the fix covers the whole registry from one seam — no per-op
patch, no dispatch-order change.

Real registrations only (no mocks): ``import reyn.core.op_runtime`` registers all 9 real
``STRUCTURED_PASSTHROUGH`` declarations at import time (the same registration every real op call goes
through in production) — the registry-derived list below is read back from the SAME
``reyn.core.offload.canonical._DECLARATIONS`` registry the coverage-gate test
(``test_fp0056_m3_fail_visible.py``) already reads, not a hand-typed guess.
"""
from __future__ import annotations

import pytest

# Registers every real op handler's canonical declaration (the 9 STRUCTURED_PASSTHROUGH admin/install
# verbs among them) — the same import path every real op dispatch goes through in production.
import reyn.core.op_runtime as _op_runtime  # noqa: F401
from reyn.core.offload.canonical import (
    _DECLARATIONS,
    STRUCTURED_PASSTHROUGH,
    _fallback_structured,
    to_canonical,
)
from reyn.core.pipeline.executor import (
    ExprRef,
    ForEachStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)

# Registry-derived (not hand-typed): every source_id the real registrations declared
# STRUCTURED_PASSTHROUGH, read back from the SAME registry the coverage-gate test consults.
_STRUCTURED_PASSTHROUGH_SOURCES = sorted(
    k for k, v in _DECLARATIONS.items() if v is STRUCTURED_PASSTHROUGH
)


def test_registry_actually_declares_the_nine_admin_install_verbs() -> None:
    """Tier 1: vacuity guard — the registry-derived list is non-trivial and names exactly the 9
    admin/install verbs the #3104 fix design enumerates (fails loudly if a future PR adds/removes a
    STRUCTURED_PASSTHROUGH declaration without updating this expectation)."""
    assert _STRUCTURED_PASSTHROUGH_SOURCES == [
        "mcp_drop_server",
        "mcp_install",
        "mcp_subscribe_resource",
        "mcp_unsubscribe_resource",
        "pipeline_install",
        "plugin_install",
        "plugin_uninstall",
        "presentation_install",
        "skill_install",
    ]


@pytest.mark.parametrize("source", _STRUCTURED_PASSTHROUGH_SOURCES)
def test_structured_passthrough_op_error_result_stamps_meta_iserror(source: str) -> None:
    """Tier 1: every registered STRUCTURED_PASSTHROUGH op's op-level FAILURE (the real
    ``execute_op``-degrade shape: ``{status: "error", error: <message>}``) canonicalizes with
    ``meta.isError`` set and a non-empty ``text`` carrying the message — closing the #3105-flagged gap
    for the full 9-op registry from the ``_fallback_structured`` chokepoint."""
    result = {"kind": source, "status": "error", "error": f"{source} failed: quota exceeded"}
    canonical = to_canonical(result, source=source)
    assert canonical["meta"] == {"isError": True}
    assert canonical["text"] == f"{source} failed: quota exceeded"
    # Lossless: the whole dict still rides the structured attachment (chokepoint stamp, not a
    # dispatch reorder — error_to_canonical's mapper path is untouched).
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]


@pytest.mark.parametrize("source", _STRUCTURED_PASSTHROUGH_SOURCES)
def test_structured_passthrough_op_success_result_meta_stays_empty(source: str) -> None:
    """Tier 1: negative/pin — a SUCCESS result on the same 9 ops is byte-for-byte unaffected —
    ``meta == {}`` and ``text == ""`` still, exactly as before #3104. The fix stamps the ERROR branch
    only; it must never mislabel a success passthrough as an error."""
    result = {"kind": source, "status": "ok", "installed": True}
    canonical = to_canonical(result, source=source)
    assert canonical["meta"] == {}
    assert canonical["text"] == ""
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]


def test_unregistered_source_error_result_also_stamps_meta_iserror() -> None:
    """Tier 1: a genuinely-unregistered/``None`` ``source`` (the other ``_fallback_structured`` entry
    besides STRUCTURED_PASSTHROUGH) gets the same error stamp — the chokepoint covers this path too,
    not just the 9 named ops."""
    result = {"kind": "totally_unknown_producer", "status": "error", "error": "boom"}
    canonical = to_canonical(result, source="totally_unknown_producer")
    assert canonical["meta"] == {"isError": True}
    assert canonical["text"] == "boom"
    canonical_none_source = to_canonical(result, source=None)
    assert canonical_none_source["meta"] == {"isError": True}


def test_discriminator_miss_error_result_stamps_meta_iserror_too() -> None:
    """Tier 1: the discriminator-miss fallback (piece #3, M3) is the SAME ``_fallback_structured``
    function — its ``discriminator_miss=True`` marker keeps working alongside the new error stamp
    (the two concerns are orthogonal: one flags audit-visibility, the other flags on_error)."""
    result = {"status": "error", "error": "inner dispatch missed"}
    canonical = _fallback_structured(result, discriminator_miss=True)
    assert canonical["meta"] == {"isError": True}
    assert canonical["text"] == "inner dispatch missed"
    assert canonical.get("_discriminator_miss") is True


# ── end-to-end: on_error fan-out over a STRUCTURED_PASSTHROUGH op now fires (#3099 x #3104) ────────


@pytest.mark.asyncio
async def test_for_each_on_error_abort_fires_on_structured_passthrough_op_canonical_error():
    """Tier 2: the #3105 on_error-consumes-canonical-error wiring (``_tool_step_canonical_error``)
    NOW fires for a STRUCTURED_PASSTHROUGH op too (before #3104 it silently never did — the exact gap
    #3105's co-vet flagged as required follow-up). A ``for_each`` over ``plugin_install`` calls with a
    declared ``on_error: abort`` aborts the pipeline the same way it already does for a mapped op
    (mirrors ``tests/test_3099_on_error_canonical_seam.py``'s ``file`` case, but for a passthrough
    op — end-to-end through the real ``plugin_install`` STRUCTURED_PASSTHROUGH registration)."""
    def _dispatch(name: str, args: dict) -> dict:
        assert name == "plugin_install"
        plugin = args["plugin"]
        if plugin == "bad-plugin":
            return {
                "_canonical_source": "plugin_install",
                "kind": "plugin_install",
                "status": "error",
                "error": f"install failed: {plugin} not found",
            }
        return {"_canonical_source": "plugin_install", "kind": "plugin_install", "status": "ok"}

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["good-plugin", "bad-plugin"],
            on_error="abort",
            do=ToolStep(name="plugin_install", args={"plugin": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    with pytest.raises(PipelineExecutionError, match="canonical error"):
        await PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None, run_id="run-3104-passthrough-abort",
        )


@pytest.mark.asyncio
async def test_for_each_on_error_continue_survives_good_items_over_structured_passthrough_op():
    """Tier 2: ``on_error: continue`` over the same STRUCTURED_PASSTHROUGH op drops only the failing
    item — the collected pipe carries the survivor, proving the trigger is real (not a false
    positive on the success item too)."""
    def _dispatch(name: str, args: dict) -> dict:
        plugin = args["plugin"]
        if plugin == "bad-plugin":
            return {
                "_canonical_source": "plugin_install",
                "kind": "plugin_install",
                "status": "error",
                "error": "install failed",
            }
        return {"_canonical_source": "plugin_install", "kind": "plugin_install", "status": "ok"}

    pipeline = Pipeline(steps=[
        ForEachStep(
            items=["good-plugin", "bad-plugin"],
            on_error="continue",
            do=ToolStep(name="plugin_install", args={"plugin": ExprRef("item")}),
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = await PipelineExecutor().run(
        pipeline, None,
        tool_dispatch=_dispatch, state_log=None, run_id="run-3104-passthrough-continue",
    )
    assert result.pipe_data == [
        {"text": "", "structured": {"kind": "plugin_install", "status": "ok"}},
    ]
    dropped = result.completed_step_results["0.for_each.1"]
    assert dropped["__fan_out_dropped__"] is True
