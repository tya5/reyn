"""Tier 2: #5509 — ``_materialise_media_part``'s capability gate.

Before this, an image was embedded unconditionally — no ``get_model_info``
call anywhere in the path — so a model that genuinely cannot take images
(or one nobody has confirmed can) still received one, discovering the
mismatch only via a live provider error mid-turn. Real litellm throughout
(no mocks) — ``ensure_litellm_ready()`` forces the deterministic blocking
warm-up first, same reasoning as ``test_5509_model_media_capability.py``'s
own module-level call.
"""
from __future__ import annotations

import logging

import pytest

from reyn.llm.litellm_bootstrap import ensure_litellm_ready
from reyn.llm.model_media_capability import register_media_capability_overrides
from reyn.runtime.router_loop import (
    MediaMaterialiseFailure,
    _build_media_followup_message,
    _materialise_media_part,
)

ensure_litellm_ready()

_IMAGE_BLOCK = {"type": "image", "mime_type": "image/png", "data": "AAAA"}


def test_supported_model_still_materialises_inline() -> None:
    """Tier 2: accept side — gpt-4o (real supports_vision=True) embeds the
    block exactly as before #5509 (no behavior change for a known-capable
    model)."""
    part = _materialise_media_part(_IMAGE_BLOCK, None, model="gpt-4o")
    assert part == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_uncataloged_model_degrades_to_capability_unavailable() -> None:
    """Tier 2: the load-bearing case (lead-coder's own acceptance
    requirement, #5509) — a proxy-routed model litellm's catalog doesn't
    know, with NO operator declaration, degrades to a NAMED failure
    reason rather than either (a) silently embedding anyway or (b)
    raising. This is the PINNED behavior for "catalog miss, no config" —
    see this test's own name; any future change to this behavior must
    change this test's name and docstring, not just its body."""
    result = _materialise_media_part(
        _IMAGE_BLOCK, None, model="openai/reyn-test-5509-gate-uncataloged",
    )
    assert result is MediaMaterialiseFailure.CAPABILITY_UNAVAILABLE


def test_a_declared_override_restores_inline_materialisation() -> None:
    """Tier 2: the escape hatch closes the gap the test above pins — once
    the operator declares the model DOES support vision, the SAME
    uncataloged model materialises inline again."""
    model = "custom/reyn-test-5509-gate-declared-model"
    register_media_capability_overrides({model: {"supports_vision": True}})
    part = _materialise_media_part(_IMAGE_BLOCK, None, model=model)
    assert part == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_a_declared_unsupported_override_also_degrades() -> None:
    """Tier 2: the override can say no too — a model explicitly declared
    NOT to support vision degrades the same way an uncataloged one does."""
    model = "custom/reyn-test-5509-gate-declared-unsupported"
    register_media_capability_overrides({model: {"supports_vision": False}})
    result = _materialise_media_part(_IMAGE_BLOCK, None, model=model)
    assert result is MediaMaterialiseFailure.CAPABILITY_UNAVAILABLE


def test_model_none_skips_the_gate_entirely() -> None:
    """Tier 2: accept side — a partial/test host with no resolvable model
    string (``model=None``) keeps the pre-#5509 unconditional-embed
    behavior — the gate is additive, not a new failure mode for callers
    that never had a model to check against."""
    part = _materialise_media_part(_IMAGE_BLOCK, None, model=None)
    assert part == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }


def test_non_image_mime_always_degrades_regardless_of_capability() -> None:
    """Tier 2: architect's ruling, applied — a modality with no
    established per-item token bound (anything other than image/*) always
    routes to the ref stage, even for a model that DOES support it,
    because "can this model take it" is only question ① of architect's
    two-question rule; question ② (can the cost be bounded) has no answer
    yet for these modalities. Uses gpt-4o (real supports_pdf_input=True)
    to prove this is NOT a capability-lookup gap — the gate deliberately
    never reaches the capability check for a non-image mime at all.

    #5509 architect review (BLOCKING): this is a DIFFERENT failure reason
    than an actual capability check failing — NO_TOKEN_BOUND (question ②),
    not CAPABILITY_UNAVAILABLE (question ①). Conflating them into one
    member pointed an operator at a useless remedy (declaring a
    capability override cannot fix "this modality has no per-item token
    bound yet")."""
    pdf_block = {"type": "file", "mime_type": "application/pdf", "data": "AAAA"}
    result = _materialise_media_part(pdf_block, None, model="gpt-4o")
    assert result is MediaMaterialiseFailure.NO_TOKEN_BOUND


def test_the_caller_reads_the_failure_reason_unbounded_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: #5509 architect review (BLOCKING) — every
    ``MediaMaterialiseFailure`` member's own docstring promises a caller
    reads it; before this fix, ``_build_media_followup_message`` silently
    dropped the reason (``if not isinstance(part, MediaMaterialiseFailure)``
    with no ``else``). This is the ONE place that read now happens:
    dropping a block in the unbounded path (no ref fallback there) logs
    the reason name, not just "something failed"."""
    model = "openai/reyn-test-5509-caller-reads-reason-unbounded"
    block = {"type": "image", "mime_type": "image/png", "data": "AAAA"}
    with caplog.at_level(logging.DEBUG, logger="reyn.runtime.router_loop"):
        result = _build_media_followup_message(
            tool_name="read_file", media_blocks=[block], model=model,
        )
    assert result is None  # the only image dropped -> no follow-up at all
    assert any(
        "capability_unavailable" in r.getMessage() and "read_file" in r.getMessage()
        for r in caplog.records
    )


def test_the_caller_reads_the_failure_reason_bounded_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 2: same witness as above, for the BOUNDED path — the block
    still degrades to a ref (real fallback exists here), but the reason
    is logged before the ref-degrade, not silently."""
    model = "openai/reyn-test-5509-caller-reads-reason-bounded"
    block = {"type": "image", "mime_type": "image/png", "data": "AAAA"}
    with caplog.at_level(logging.DEBUG, logger="reyn.runtime.router_loop"):
        result = _build_media_followup_message(
            tool_name="read_file", media_blocks=[block], model=model,
            budget_tokens=10_000,
        )
    assert result is not None  # ref fallback still produced a follow-up
    assert any(
        "capability_unavailable" in r.getMessage() and "read_file" in r.getMessage()
        for r in caplog.records
    )


def test_strip_falsify_the_gate_by_hand() -> None:
    """Tier 2: STRIP-FALSIFY, performed programmatically in-process (not a
    hand-edit, since the assertion itself needs to observe both states) —
    monkeypatching the gate's own capability check to always return
    SUPPORTED makes the uncataloged-model case (which correctly degrades
    above) instead embed inline, proving the gate in
    ``test_uncataloged_model_degrades_to_capability_unavailable`` is
    genuinely load-bearing on the capability check running, not a
    tautology."""
    import reyn.llm.model_media_capability as capability_mod
    from reyn.llm.model_media_capability import MediaCapability

    original = capability_mod.get_media_capability
    capability_mod.get_media_capability = lambda model, field: MediaCapability.SUPPORTED
    try:
        part = _materialise_media_part(
            _IMAGE_BLOCK, None, model="openai/reyn-test-5509-strip-falsify-model",
        )
    finally:
        capability_mod.get_media_capability = original
    assert part == {
        "type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"},
    }
