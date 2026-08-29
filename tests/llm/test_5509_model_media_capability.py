"""Tier 2: model_media_capability.get_media_capability invariants (#5509).

Real litellm throughout — no mocks; the whole point is a real catalog query.
Each test uses a UNIQUE model string (matching test_model_budget.py's own
convention) so the process-shared warn-once state and override registry
don't leak between tests run in the same process.
"""
from __future__ import annotations

import logging

import pytest

from reyn.llm.litellm_bootstrap import ensure_litellm_ready
from reyn.llm.model_media_capability import (
    MediaCapability,
    MediaCapabilityConflictError,
    get_media_capability,
    register_media_capability_overrides,
)

# Force litellm's blocking, deterministic warm-up before any test runs —
# get_media_capability's own NOT_READY handling (module docstring) is
# real and correct, but this file's tests are about the CATALOG answer,
# not the async-warming race; without this, running this file in
# isolation (no earlier test having already touched litellm) hits the
# real "background thread hasn't finished importing yet" window and
# every catalog lookup below resolves UNKNOWN regardless of the model.
ensure_litellm_ready()


def test_a_cataloged_model_with_the_capability_resolves_supported() -> None:
    """Tier 2: gpt-4o is real-cataloged with supports_vision=True."""
    assert get_media_capability("gpt-4o", "supports_vision") is MediaCapability.SUPPORTED


def test_an_uncataloged_model_resolves_unknown_not_supported() -> None:
    """Tier 2: the load-bearing case (lead-coder finding, #5509, real
    measurement) — litellm RAISES for a model string it doesn't recognize
    (not just returns an empty dict), and this is the ORDINARY case for a
    proxy-routed deployment (owner's own standing setup: API-key handling
    is the proxy's job), not a rare edge case. Must resolve UNKNOWN, never
    SUPPORTED — an uncataloged model is never silently treated as capable."""
    result = get_media_capability(
        "openai/reyn-test-5509-uncataloged-proxy-model", "supports_vision",
    )
    assert result is MediaCapability.UNKNOWN


def test_an_uncataloged_model_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 2: lead-coder's acceptance requirement — an uncataloged model
    with no operator declaration must not fail SILENTLY; a warning names
    the exact config key to declare. Fires once per (model,
    capability_field), not on every call (same "warn once" discipline
    model_budget.py's own _warned_models uses)."""
    model = "openai/reyn-test-5509-warn-once-model"
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_media_capability"):
        get_media_capability(model, "supports_vision")
        get_media_capability(model, "supports_vision")
    (warning,) = [r for r in caplog.records if model in r.getMessage()]
    assert "model_capability_overrides" in warning.getMessage()


def test_a_cataloged_model_never_warns(caplog: pytest.LogCaptureFixture) -> None:
    """Tier 2: accept-side / noise guard — a model litellm genuinely knows
    must not trigger the uncataloged-model warning at all."""
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_media_capability"):
        get_media_capability("gpt-4o", "supports_vision")
    assert not any("gpt-4o" in r.getMessage() for r in caplog.records)


def test_an_operator_override_wins_over_an_uncataloged_result() -> None:
    """Tier 2: the escape hatch — a declared override resolves SUPPORTED
    even though litellm's own catalog has no entry for this model at all."""
    model = "custom/reyn-test-5509-declared-vision-model"
    register_media_capability_overrides({model: {"supports_vision": True}})
    assert get_media_capability(model, "supports_vision") is MediaCapability.SUPPORTED


def test_an_operator_override_wins_over_a_cataloged_result() -> None:
    """Tier 2: #4689-style priority (mirrors model_budget's own ruling) —
    the override wins UNCONDITIONALLY, not only when the catalog lookup
    fails. gpt-4o genuinely supports vision; the override forces False
    anyway, proving the override is checked FIRST, not as a fallback."""
    model = "gpt-4o"  # real catalog entry, supports_vision=True
    override_field = "reyn_test_5509_synthetic_field"
    register_media_capability_overrides({model: {override_field: False}})
    assert get_media_capability(model, override_field) is MediaCapability.UNSUPPORTED


def test_conflicting_overrides_for_the_same_model_and_field_raise() -> None:
    """Tier 2: a second, DIFFERENT declaration for the same (model,
    capability_field) raises rather than silently overwriting — mirrors
    model_budget.MaxInputTokensConflictError's own reasoning."""
    model = "custom/reyn-test-5509-conflict-model"
    register_media_capability_overrides({model: {"supports_vision": True}})
    with pytest.raises(MediaCapabilityConflictError):
        register_media_capability_overrides({model: {"supports_vision": False}})


def test_identical_reregistration_is_idempotent() -> None:
    """Tier 2: accept side of the conflict check — registering the SAME
    value twice (e.g. two Sessions sharing one project's config) must not
    raise."""
    model = "custom/reyn-test-5509-idempotent-model"
    register_media_capability_overrides({model: {"supports_vision": True}})
    register_media_capability_overrides({model: {"supports_vision": True}})
    assert get_media_capability(model, "supports_vision") is MediaCapability.SUPPORTED


def test_provider_prefix_strip_retry_resolves_under_the_bare_name() -> None:
    """Tier 2: #1162-style retry — a provider-prefixed model missing from
    the catalog under its prefix resolves under the bare suffix. gpt-4o is
    real-cataloged; "some-custom-proxy/gpt-4o" is not, under that exact
    prefixed string, but the bare "gpt-4o" suffix is."""
    result = get_media_capability("some-custom-proxy/gpt-4o", "supports_vision")
    assert result is MediaCapability.SUPPORTED
