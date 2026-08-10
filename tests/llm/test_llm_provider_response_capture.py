"""Tier 2: trace dump captures provider-specific response fields.

The narrow ``content / tool_calls / finish_reason / usage`` payload
the OS used to write was insufficient for empty-stop diagnosis — an
operator looking at a recorded empty-stop couldn't tell whether the
provider blocked the response on a safety filter, refused, or simply
output zero tokens (= the actual case for gemini-2.5-flash-lite on
some tool_use → narration transitions).

These tests pin the contract that
``_extract_provider_response_fields`` surfaces the right metadata
when the provider emits it, and stays silent when it doesn't (=
backward-compat with non-Vertex providers).
"""
from __future__ import annotations

from types import SimpleNamespace

from reyn.llm.llm import _extract_provider_response_fields


def _stub_response(*, choices: list, **top_level) -> SimpleNamespace:
    """Build a SimpleNamespace mimicking a litellm ModelResponse shape."""
    return SimpleNamespace(choices=choices, **top_level)


def _stub_choice(message: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(message=message, finish_reason="stop", index=0)


# ── Vertex AI (Gemini) shape ───────────────────────────────────────────────


def test_vertex_safety_and_refusal_captured():
    """Tier 2: Gemini's safety_results and provider_specific_fields.refusal
    are pulled into the trace dump payload.
    """
    msg = SimpleNamespace(
        content=None,
        provider_specific_fields={"refusal": "Cannot fulfill"},
    )
    response = _stub_response(
        choices=[_stub_choice(msg)],
        vertex_ai_safety_results=[
            {"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "HIGH"}
        ],
        vertex_ai_grounding_metadata=[],
        vertex_ai_citation_metadata=[],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=0,
            completion_tokens_details=None,
        ),
    )

    out = _extract_provider_response_fields(response)
    assert out["provider_specific_fields"] == {"refusal": "Cannot fulfill"}
    assert out["vertex_ai_safety_results"] == [
        {"category": "HARM_CATEGORY_HATE_SPEECH", "probability": "HIGH"}
    ]
    # Empty lists are dropped.
    assert "vertex_ai_grounding_metadata" not in out
    assert "vertex_ai_citation_metadata" not in out


def test_empty_provider_fields_skipped():
    """Tier 2: when provider_specific_fields is empty / refusal is None,
    no spurious entry leaks into the trace.
    """
    msg = SimpleNamespace(content="Hello!", provider_specific_fields={})
    response = _stub_response(
        choices=[_stub_choice(msg)],
        vertex_ai_safety_results=[],
        usage=SimpleNamespace(
            prompt_tokens=50,
            completion_tokens=10,
            completion_tokens_details=None,
        ),
    )
    out = _extract_provider_response_fields(response)
    assert "provider_specific_fields" not in out
    assert "vertex_ai_safety_results" not in out


# ── OpenAI shape ───────────────────────────────────────────────────────────


def test_openai_system_fingerprint_captured():
    """Tier 2: OpenAI's system_fingerprint is captured for replay /
    attractor-only-on-this-build investigations.
    """
    msg = SimpleNamespace(content="OK", provider_specific_fields=None)
    response = _stub_response(
        choices=[_stub_choice(msg)],
        system_fingerprint="fp_abc123",
        service_tier="default",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=2,
            completion_tokens_details=None,
        ),
    )
    out = _extract_provider_response_fields(response)
    assert out["system_fingerprint"] == "fp_abc123"
    assert out["service_tier"] == "default"


# ── Reasoning models (= o1, claude thinking) ───────────────────────────────


def test_completion_tokens_details_captured():
    """Tier 2: thinking-mode models expose reasoning vs. text token
    splits in completion_tokens_details. Captured for cost / latency
    debugging.
    """
    ctd = SimpleNamespace(
        reasoning_tokens=400,
        text_tokens=80,
        model_dump=lambda: {"reasoning_tokens": 400, "text_tokens": 80},
    )
    msg = SimpleNamespace(content="answer", provider_specific_fields=None)
    response = _stub_response(
        choices=[_stub_choice(msg)],
        usage=SimpleNamespace(
            prompt_tokens=200,
            completion_tokens=480,
            completion_tokens_details=ctd,
        ),
    )
    out = _extract_provider_response_fields(response)
    assert out["completion_tokens_details"] == {
        "reasoning_tokens": 400, "text_tokens": 80,
    }


# ── Robustness ─────────────────────────────────────────────────────────────


def test_malformed_response_returns_empty():
    """Tier 2: when the response object is malformed (= no choices /
    no message), the extractor returns an empty dict instead of raising.
    Trace-dump infra MUST NOT mask LLM errors with its own exceptions.
    """
    response = SimpleNamespace()  # no choices attribute
    out = _extract_provider_response_fields(response)
    assert out == {}


def test_choices_without_message_doesnt_crash():
    """Tier 2: response.choices[0] without a message attribute — falls
    through cleanly to top-level field extraction.
    """
    response = _stub_response(
        choices=[SimpleNamespace(finish_reason="stop", index=0)],
        system_fingerprint="fp_x",
    )
    out = _extract_provider_response_fields(response)
    assert out == {"system_fingerprint": "fp_x"}
