"""Tier 2: a model class can state whether its calls stream, and reyn obeys it.

Whether a call streams was decided solely by litellm's catalog. That catalog is
a snapshot pinned by ``reyn/__init__.py`` (``LITELLM_LOCAL_MODEL_COST_MAP``), so
for any model newer than the installed litellm it says nothing — and reyn read
"says nothing" as "cannot stream". The operator, who chose the model and knows
the endpoint, had no way to say otherwise.

``stream:`` on a model class is that way. It is a REYN field, consumed by
``_streaming_enabled``, and deliberately NOT forwarded to litellm: passed
through as an ordinary kwarg it lands in the collect-whole branch and makes
litellm hand back a stream object that reyn reads as a finished reply (#3627).
Keeping it out of ``kwargs`` is what makes that impossible rather than merely
discouraged, so it is asserted here.
"""
from __future__ import annotations

from reyn.llm.llm import _streaming_capability, _streaming_enabled
from reyn.llm.model_resolver import ModelSpec

_UNKNOWN = "totally-unknown-model-xyz-3639"
_KNOWN_NON_STREAMING = "o1-pro"


def test_stream_is_a_reyn_field_not_a_litellm_kwarg() -> None:
    """Tier 2: ``stream`` reaches the spec and never the passthrough kwargs.

    The sibling keys are checked in the same call so a change that started
    forwarding everything verbatim would fail here rather than at a provider.
    """
    spec = ModelSpec.from_config(
        {"model": "some-model", "stream": True, "temperature": 0.0}
    )

    assert spec.stream is True
    assert spec.kwargs == {"temperature": 0.0}


def test_an_absent_stream_field_leaves_the_decision_to_the_catalog() -> None:
    """Tier 2: no operator opinion is distinct from an operator saying False."""
    spec = ModelSpec.from_config({"model": "some-model"})

    assert spec.stream is None
    assert _streaming_enabled(_UNKNOWN, has_tools=False, override=spec.stream) is True


def test_the_operator_can_turn_streaming_off_for_a_model_the_catalog_allows() -> None:
    """Tier 2: the override wins downward."""
    assert _streaming_capability("gpt-4o", has_tools=False) is True

    assert _streaming_enabled("gpt-4o", has_tools=False, override=False) is False


def test_the_operator_can_turn_streaming_on_for_a_model_the_catalog_denies() -> None:
    """Tier 2: the override wins upward too — the direction that matters.

    Downward-only would still leave an operator arguing with a snapshot they
    cannot edit, which is the situation that produced the defect: a model too
    new for the pinned table, with a working endpoint the operator had already
    configured.
    """
    assert _streaming_capability(_KNOWN_NON_STREAMING, has_tools=False) is False

    assert _streaming_enabled(_KNOWN_NON_STREAMING, has_tools=False, override=True) is True


def test_a_non_boolean_stream_is_rejected_at_load() -> None:
    """Tier 2: a typo fails where the operator can see it.

    Most model-class fields are forwarded to litellm unvalidated, so a typo
    surfaces as a provider error. This one is consumed by reyn, so nothing
    downstream would complain — ``"true"`` would simply be truthy forever.
    """
    import pytest

    with pytest.raises(ValueError, match="stream"):
        ModelSpec.from_config({"model": "some-model", "stream": "true"})
