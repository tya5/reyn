"""Tier 2: #4689 (owner instruction) — llm.models.<tier>.max_input_tokens
takes priority over the LiteLLM catalog unconditionally.

Real instances only: ModelSpec/ModelResolver are constructed for real
(no mocks); model_budget's process-shared registry is exercised directly,
each test using a UNIQUE model string to avoid cross-test collision (the
same discipline test_model_budget.py's own fallback tests already use for
the SAME reason — this module's state is process-shared and not reset
between tests).
"""
from __future__ import annotations

import pytest

from reyn.llm.model_budget import (
    MaxInputTokensConflictError,
    get_max_input_tokens,
    register_max_input_overrides,
)
from reyn.llm.model_resolver import ModelResolver, ModelSpec

# ── ModelSpec.max_input_tokens field ─────────────────────────────────────


def test_from_config_extracts_max_input_tokens_as_its_own_field():
    """Tier 2: max_input_tokens is a real ModelSpec field, not left in
    kwargs (which would silently pass through to litellm untouched — the
    #4655 B-3 shape this field exists to avoid)."""
    spec = ModelSpec.from_config({
        "model": "openai/gpt-4o", "max_input_tokens": 500_000,
    })
    assert spec.max_input_tokens == 500_000
    assert "max_input_tokens" not in spec.kwargs


def test_from_config_defaults_max_input_tokens_to_none():
    """Tier 2: no operator opinion -> None, byte-identical to before this
    field existed."""
    spec = ModelSpec.from_config({"model": "openai/gpt-4o"})
    assert spec.max_input_tokens is None


def test_from_config_rejects_a_non_positive_max_input_tokens():
    """Tier 2: 0 or negative is a config-authoring mistake, rejected at
    load time rather than silently accepted as a real ceiling."""
    with pytest.raises(ValueError, match="positive"):
        ModelSpec.from_config({"model": "openai/gpt-4o", "max_input_tokens": 0})


def test_from_config_rejects_a_non_int_max_input_tokens():
    """Tier 2: a string/float value fails loudly (a real config-authoring
    mistake, not silently coerced)."""
    with pytest.raises(ValueError, match="int"):
        ModelSpec.from_config({"model": "openai/gpt-4o", "max_input_tokens": "500000"})


def test_from_config_rejects_a_bool_max_input_tokens():
    """Tier 2: (accept-side gap check) bool is an int SUBCLASS in Python —
    `isinstance(True, int)` is True — so this must be checked explicitly,
    not just `isinstance(x, int)`."""
    with pytest.raises(ValueError, match="int"):
        ModelSpec.from_config({"model": "openai/gpt-4o", "max_input_tokens": True})


# ── extends path — the pre-existing gap this PR also closes ─────────────


def test_extends_path_propagates_max_input_tokens_from_the_base_class():
    """Tier 2: a class the operator extends carries its max_input_tokens
    forward when the override doesn't redeclare it."""
    resolver = ModelResolver({
        "base": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
        "child": {"extends": "base", "temperature": 0.1},
    })
    assert resolver.resolve("child").max_input_tokens == 500_000


def test_extends_path_lets_the_override_replace_the_base_value():
    """Tier 2: (accept-side) an override that DOES redeclare
    max_input_tokens replaces the base's value, not merges with it."""
    resolver = ModelResolver({
        "base": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
        "child": {"extends": "base", "max_input_tokens": 200_000},
    })
    assert resolver.resolve("child").max_input_tokens == 200_000


def test_extends_path_propagates_stream_from_the_base_class():
    """Tier 2: the pre-existing gap this PR's from_config-routing fix also
    closes — before, a base class's stream/api_base/provider never
    reached an extends-ing override at all (silently dropped, left in
    kwargs where stream's own __post_init__ guard would reject it as an
    unknown litellm passthrough kwarg — or worse, silently pass through)."""
    resolver = ModelResolver({
        "base": {"model": "openai/gpt-4o", "stream": True},
        "child": {"extends": "base", "temperature": 0.1},
    })
    assert resolver.resolve("child").stream is True
    assert "stream" not in resolver.resolve("child").kwargs


# ── ModelResolver.max_input_token_overrides ──────────────────────────────


def test_max_input_token_overrides_maps_resolved_model_string_to_value():
    """Tier 2: the class -> resolved-model-string -> declared value map,
    the exact shape register_max_input_overrides expects."""
    resolver = ModelResolver({
        "standard": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
        "light": "openai/gpt-4o-mini",
    })
    assert resolver.max_input_token_overrides() == {"openai/gpt-4o": 500_000}


def test_max_input_token_overrides_is_empty_when_no_class_declares_one():
    """Tier 2: (accept-side) no class in the namespace set max_input_tokens
    — an empty map, not an error."""
    resolver = ModelResolver({"standard": "openai/gpt-4o"})
    assert resolver.max_input_token_overrides() == {}


def test_max_input_token_overrides_two_classes_agreeing_on_the_same_model_is_fine():
    """Tier 2: (accept-side) two classes resolving to the SAME model
    string with the SAME declared value is not a conflict — only a
    DIFFERENT value is."""
    resolver = ModelResolver({
        "a": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
        "b": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
    })
    assert resolver.max_input_token_overrides() == {"openai/gpt-4o": 500_000}


def test_max_input_token_overrides_raises_on_conflicting_values_within_one_resolver():
    """Tier 2: two classes in the SAME resolver resolving to the same
    model string with DIFFERENT declared values is ambiguous — raised
    before it ever reaches the process-shared registration step."""
    resolver = ModelResolver({
        "a": {"model": "openai/gpt-4o", "max_input_tokens": 500_000},
        "b": {"model": "openai/gpt-4o", "max_input_tokens": 200_000},
    })
    with pytest.raises(ValueError, match="conflicting max_input_tokens"):
        resolver.max_input_token_overrides()


# ── model_budget.register_max_input_overrides ────────────────────────────


def test_register_max_input_overrides_wins_over_the_catalog_unconditionally():
    """Tier 2: THE #4689 witness — a config override wins even for a
    model that WOULD resolve from the real litellm catalog (priority is
    config > catalog unconditionally, not just when the catalog misses,
    per the owner's own explicit instruction)."""
    model = "gemini/gemini-2.5-flash-lite-4689-test-a"
    register_max_input_overrides({model: 999_999})
    assert get_max_input_tokens(model) == 999_999


def test_register_max_input_overrides_is_idempotent_for_the_same_value():
    """Tier 2: (accept-side) re-registering the SAME model with the SAME
    value (e.g. two Sessions sharing one project's config) does not
    raise."""
    model = "gemini/gemini-2.5-flash-lite-4689-test-b"
    register_max_input_overrides({model: 111_111})
    register_max_input_overrides({model: 111_111})  # must not raise
    assert get_max_input_tokens(model) == 111_111


def test_register_max_input_overrides_raises_on_a_conflicting_re_registration():
    """Tier 2: lead-coder's explicit condition — the module-level registry
    is process-shared (reyn holds multiple sessions per process), so a
    SECOND, DIFFERENT registration for the same model string must raise
    rather than silently overwrite the first (the same "config wins here,
    silently doesn't win there" confusion #4680 already caused, this time
    from cross-session sharing)."""
    model = "gemini/gemini-2.5-flash-lite-4689-test-c"
    register_max_input_overrides({model: 111_111})
    with pytest.raises(MaxInputTokensConflictError, match="conflicting"):
        register_max_input_overrides({model: 222_222})


def test_a_model_with_no_registered_override_still_uses_the_catalog():
    """Tier 2: (accept-side) the registration is per-model-string — an
    unrelated model never registered still resolves through the normal
    catalog/fallback path, unaffected."""
    result = get_max_input_tokens("unknown/garbage-model-4689-unregistered")
    from reyn.llm.model_budget import _STARTUP_FALLBACK_MAX_INPUT_TOKENS
    assert result == _STARTUP_FALLBACK_MAX_INPUT_TOKENS
