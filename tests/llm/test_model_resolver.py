"""Tier 2: ModelResolver OS invariant tests (PR-MODEL-SPEC + PR-MODEL-SPEC-EXTENDS).

Pinned invariants:
  - resolve(name) returns ModelSpec for known classes (API change pin)
  - str-form and dict-form values both produce correct ModelSpec
  - backward compat: str form -> ModelSpec(kwargs={})
  - dict form: extra_body / temperature / etc. carried in ModelSpec.kwargs
  - is_known_class behaves identically for str-form and dict-form values
  - unknown name passthrough: ModelSpec(model=name, kwargs={})
  - ReynConfig.models accepts dict-form values (config layer check)
  - [EXTENDS] built-in pre-load: claude-sonnet-thinking resolvable with empty user mapping
  - [EXTENDS] user override: user-declared entry wins over same-named built-in
  - [EXTENDS] backward compat: existing ``/``-containing str form unchanged

Reference: PR-MODEL-SPEC Task 2 (Tier 2) + PR-MODEL-SPEC-EXTENDS Task 3 (Tier 2).
"""
from __future__ import annotations

import logging

import pytest

from reyn.llm.builtin_models import BUILTIN_TIER_ALIASES
from reyn.llm.model_resolver import STANDARD_CLASSES, ModelResolver, ModelSpec

# ---------------------------------------------------------------------------
# resolve() returns ModelSpec — API change pin
# ---------------------------------------------------------------------------


def test_resolve_known_class_returns_model_spec():
    """Tier 2: resolve(known_class) returns ModelSpec instance."""
    r = ModelResolver({"light": "openai/model-a"})
    spec = r.resolve("light")
    assert isinstance(spec, ModelSpec)


def test_resolve_unknown_name_returns_model_spec_passthrough():
    """Tier 2: resolve(unknown) returns ModelSpec with model=name, kwargs={}."""
    r = ModelResolver({"standard": "openai/model-b"})
    spec = r.resolve("openai/gpt-4o")
    assert isinstance(spec, ModelSpec)
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs == {}


def test_resolve_unknown_name_logs_warning(caplog):
    """Tier 2: resolve(unknown) logs a warning naming the unresolved value (#3368).

    Found via bug-mining (2026-07-26): an unregistered/mistyped model CLASS
    name (e.g. a config load failure dropping `models: {standard: ...}`)
    falls into the same silent passthrough as a legitimate raw LiteLLM model
    string — the two are indistinguishable until litellm itself rejects the
    unresolved name much later. Falsification: pre-fix, no log record was
    emitted here. Passthrough behavior itself (assertions above) is
    unchanged by this test — only that it is no longer fully silent.
    """
    import logging

    r = ModelResolver({"standard": "openai/model-b"})
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        r.resolve("stnadard")
    assert any("stnadard" in rec.message for rec in caplog.records)


def test_resolve_unknown_name_warns_once_per_distinct_name(caplog):
    """Tier 2: repeated resolve() calls for the same unresolved name warn once (#3368).

    resolve() is on the per-LLM-call hot path (called once per request for
    the session's configured model class) — an unresolved name persists for
    the session's lifetime, so an unconditional per-call warning would log
    on every single turn. Falsification: pre-dedup, N calls with the same
    unresolved name produced N warning records; this asserts exactly 1.
    A distinct name still gets its own warning (no cross-name suppression).
    """
    import logging

    r = ModelResolver({"standard": "openai/model-b"})

    def _matching(caplog, needle):
        return [rec for rec in caplog.records if needle in rec.message]

    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        r.resolve("stnadard")
        count_after_first_call = len(_matching(caplog, "stnadard"))
        r.resolve("stnadard")
        r.resolve("stnadard")
        count_after_repeat_calls = len(_matching(caplog, "stnadard"))
        r.resolve("another_typo")
        another_typo_hits = _matching(caplog, "another_typo")

    # Repeating the SAME unresolved name must not grow the warning count
    # beyond what the first call already produced.
    assert count_after_repeat_calls == count_after_first_call
    # A genuinely NEW unresolved name is not suppressed by the first name's
    # dedup — each distinct name gets its own warning.
    assert another_typo_hits


# ---------------------------------------------------------------------------
# Backward compat: str form
# ---------------------------------------------------------------------------


def test_resolve_str_form_model_string():
    """Tier 2: str-form mapping -> ModelSpec.model matches configured string."""
    r = ModelResolver({"standard": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("standard")
    assert spec.model == "openai/gemini-2.5-flash-lite"
    assert spec.kwargs == {}


def test_resolve_str_form_empty_kwargs():
    """Tier 2: str-form mapping -> ModelSpec.kwargs is empty (no extra params)."""
    r = ModelResolver({"light": "openai/gemini-2.5-flash-lite"})
    spec = r.resolve("light")
    assert spec.kwargs == {}


def test_resolve_str_form_multiple_classes():
    """Tier 2: multiple str-form classes resolve independently."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "standard": "openai/gpt-4o",
        "strong": "anthropic/claude-3-5-sonnet",
    })
    assert r.resolve("light").model == "openai/gemini-2.5-flash-lite"
    assert r.resolve("standard").model == "openai/gpt-4o"
    assert r.resolve("strong").model == "anthropic/claude-3-5-sonnet"


# ---------------------------------------------------------------------------
# dict form: extra_body / temperature / arbitrary kwargs carried
# ---------------------------------------------------------------------------


def test_resolve_dict_form_temperature_carried():
    """Tier 2: dict-form with temperature -> ModelSpec.kwargs has temperature."""
    r = ModelResolver({"standard": {"model": "openai/gpt-4o", "temperature": 0.7}})
    spec = r.resolve("standard")
    assert spec.model == "openai/gpt-4o"
    assert spec.kwargs["temperature"] == 0.7


def test_resolve_dict_form_extra_body_carried():
    """Tier 2: dict-form with extra_body -> ModelSpec.kwargs has extra_body."""
    thinking = {"type": "enabled", "budget_tokens": 16000}
    r = ModelResolver({
        "strong": {
            "model": "anthropic/claude-3-7-sonnet",
            "extra_body": {"thinking": thinking},
            "max_tokens": 16000,
            "temperature": 0.0,
        }
    })
    spec = r.resolve("strong")
    assert spec.model == "anthropic/claude-3-7-sonnet"
    assert spec.kwargs["extra_body"] == {"thinking": thinking}
    assert spec.kwargs["max_tokens"] == 16000
    assert spec.kwargs["temperature"] == 0.0
    assert "model" not in spec.kwargs


def test_resolve_dict_form_model_key_not_in_kwargs():
    """Tier 2: 'model' key from dict is not duplicated in kwargs."""
    r = ModelResolver({"light": {"model": "openai/model-a", "top_p": 0.9}})
    spec = r.resolve("light")
    assert "model" not in spec.kwargs
    assert spec.kwargs == {"top_p": 0.9}


# ---------------------------------------------------------------------------
# is_known_class identical for str-form and dict-form
# ---------------------------------------------------------------------------


def test_is_known_class_str_form():
    """Tier 2: is_known_class True for str-form class, False for unknown."""
    r = ModelResolver({"light": "openai/model-a"})
    assert r.is_known_class("light") is True
    # #3368: light/standard/strong are now built-in aliases (resolvable with
    # no user mapping at all), so "strong" is a poor unknown-class example —
    # use a name that is neither user-declared nor a built-in.
    assert r.is_known_class("totally-unknown-class") is False


def test_is_known_class_dict_form_same_as_str_form():
    """Tier 2: is_known_class behaves identically for dict-form values."""
    r_str = ModelResolver({"light": "openai/model-a", "standard": "openai/model-b"})
    r_dict = ModelResolver({
        "light": {"model": "openai/model-a"},
        "standard": {"model": "openai/model-b"},
    })
    for name in ("light", "standard", "strong", "gpt-4o"):
        assert r_str.is_known_class(name) == r_dict.is_known_class(name), (
            f"is_known_class({name!r}) differs between str-form and dict-form mapping"
        )


def test_is_known_class_mixed_mapping():
    """Tier 2: mapping can mix str-form and dict-form values."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "strong": {"model": "anthropic/claude-3-7-sonnet", "temperature": 0.0},
    })
    assert r.is_known_class("light") is True
    assert r.is_known_class("strong") is True
    # #3368: "standard" is now a built-in alias, so it is known even though
    # this mapping doesn't declare it — use a genuinely unrelated name.
    assert r.is_known_class("totally-unknown-class") is False


# ---------------------------------------------------------------------------
# Config layer: ReynConfig.models accepts dict-form values
# ---------------------------------------------------------------------------


def test_reyn_config_models_accepts_dict_form():
    """Tier 2: ReynConfig.llm.models field allows dict-form values through
    config layer (#4174 T3: moved from top-level `.models`)."""
    import dataclasses

    from reyn.config import LLMConfig, ReynConfig
    cfg = dataclasses.replace(ReynConfig(), llm=LLMConfig(models={
        "light": "openai/gemini-2.5-flash-lite",
        "strong": {
            "model": "anthropic/claude-3-7-sonnet",
            "temperature": 0.0,
            "extra_body": {"thinking": {"type": "enabled", "budget_tokens": 8000}},
        },
    }))
    r = ModelResolver(cfg.llm.models)
    light_spec = r.resolve("light")
    assert light_spec.model == "openai/gemini-2.5-flash-lite"
    assert light_spec.kwargs == {}

    strong_spec = r.resolve("strong")
    assert strong_spec.model == "anthropic/claude-3-7-sonnet"
    assert strong_spec.kwargs["temperature"] == 0.0
    assert strong_spec.kwargs["extra_body"]["thinking"]["type"] == "enabled"


# ---------------------------------------------------------------------------
# PR-MODEL-SPEC-EXTENDS: built-in pre-load + user override (Tier 2)
# ---------------------------------------------------------------------------


def test_extends_builtin_preload_empty_user_mapping():
    """Tier 2: [EXTENDS] empty user mapping -> built-in claude-sonnet-thinking resolvable."""
    r = ModelResolver({})
    spec = r.resolve("claude-sonnet-thinking")
    assert isinstance(spec, ModelSpec)
    assert "anthropic" in spec.model
    assert spec.kwargs.get("extra_body", {}).get("thinking", {}).get("type") == "enabled"


def test_extends_user_override_wins_over_builtin():
    """Tier 2: [EXTENDS] user-declared entry with same name as built-in takes precedence."""
    r = ModelResolver({"claude-sonnet": {"model": "openai/gpt-4o"}})
    spec = r.resolve("claude-sonnet")
    assert spec.model == "openai/gpt-4o"


def test_extends_backward_compat_slash_str_with_builtin_loaded():
    """Tier 2: [EXTENDS] existing '/' str form resolves as literal even with built-ins loaded."""
    r = ModelResolver({
        "light": "openai/gemini-2.5-flash-lite",
        "standard": "openai/gpt-4o",
    })
    assert r.resolve("light").model == "openai/gemini-2.5-flash-lite"
    assert r.resolve("light").kwargs == {}
    assert r.resolve("standard").model == "openai/gpt-4o"


def test_light_standard_strong_resolve_with_no_reyn_yaml_at_all():
    """Tier 2: light/standard/strong resolve to real models with an empty user mapping (#3368).

    Found via bug-mining (2026-07-28): `ReynConfig.model` defaults to
    "standard" even with zero config files, but BUILTIN_MODELS previously had
    no "light"/"standard"/"strong" entries — so a project with no reyn.yaml
    (or one without a `models:` block) resolved "standard" via the unknown-
    name passthrough, sending the literal string "standard" to litellm
    (`litellm.BadRequestError: ... You passed model=standard`, reproduced via
    `litellm.utils.get_llm_provider("standard")`). Falsification: pre-fix,
    `r.resolve("standard").model == "standard"` (the bare class name, not a
    real model string).
    """
    r = ModelResolver({})
    for class_name in ("light", "standard", "strong"):
        spec = r.resolve(class_name)
        assert "/" in spec.model, f"{class_name} resolved to a bare, unresolved name: {spec.model!r}"


# ── #3374: partial generic-tier declaration warning ─────────────────────────
#
# The built-in tier aliases (#3368) make an undeclared tier resolve silently to
# reyn's default instead of the project's. These tests witness the warning's
# CONTENT (a test that only counts warnings passes on a useless message) and its
# non-vacuity (zero-config and fully-declared must stay silent).

#: Substring that identifies the partial-tier warning specifically, so a count
#: anchor cannot accidentally match the `/`-prefix or #3372 passthrough warning
#: (neither says "tier"). Content is asserted separately and substantively.
_TIER_WARN_MARKER = "tier(s) but omits"


def _tier_warnings(caplog) -> list[str]:
    return [m for m in caplog.messages if _TIER_WARN_MARKER in m]


@pytest.mark.parametrize("omitted", sorted(BUILTIN_TIER_ALIASES))
def test_partial_tier_declaration_warns_naming_tier_and_its_fallback_model(
    omitted, caplog,
):
    """Tier 2: omitting one tier while declaring the others warns actionably (#3374).

    Parametrized over ``BUILTIN_TIER_ALIASES`` (the single producer) rather than
    a hand-written name list, so a fourth tier added there automatically gains a
    case instead of silently escaping this gate (the #3363 drift shape).

    Witnesses the three things the operator needs to act without opening source
    or docs: WHICH tier is missing, WHAT it now resolves to, and WHAT to write
    in reyn.yaml.
    """
    declared = {
        t: f"openai/my-{t}" for t in BUILTIN_TIER_ALIASES if t != omitted
    }
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        r = ModelResolver(declared)

    # Anchor: exactly one partial-tier warning — the tuple unpack fails loudly
    # on zero (mechanism dead) and on duplicates (warned twice per resolver).
    warnings = _tier_warnings(caplog)
    assert warnings, f"expected a partial-tier warning, got: {caplog.messages}"
    (msg,) = warnings

    # (a) reports EXACTLY the omitted tier — asserting on the extracted value,
    #     which also catches a declared tier being misreported as missing.
    reported_omitted = msg.split("but omits ", 1)[1].split(" —", 1)[0]
    assert {t.strip() for t in reported_omitted.split(",")} == {omitted}

    # (b) names the model it actually fell back to — "falls back to the
    #     built-in" is useless without the resolved name.
    fallback_model = r.resolve(omitted).model
    assert "/" in fallback_model  # sanity: a real model string, not a bare name
    assert fallback_model in msg, (
        f"warning must name the resolved fallback model {fallback_model!r}: {msg}"
    )

    # (c) tells the operator what to write to take control.
    assert "reyn.yaml" in msg and "llm.models:" in msg
    assert f"{omitted}: {fallback_model}" in msg, (
        f"warning must include a copy-pasteable `{omitted}: <model>` line: {msg}"
    )


def test_zero_config_does_not_warn_about_partial_tiers(caplog):
    """Tier 2: declaring NO tiers is the zero-config case, not a partial one (#3374).

    Non-vacuity guard: this is precisely the case the built-in aliases exist to
    serve, so warning here would fire for every new user with no reyn.yaml.
    """
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({})
    assert _tier_warnings(caplog) == []


def test_fully_declared_tiers_do_not_warn(caplog):
    """Tier 2: declaring every tier warns about nothing — no tier is falling back (#3374)."""
    full = {t: f"openai/my-{t}" for t in BUILTIN_TIER_ALIASES}
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver(full)
    assert _tier_warnings(caplog) == []


def test_non_tier_models_only_is_not_partial(caplog):
    """Tier 2: a `models:` block with only non-tier entries declares zero tiers (#3374).

    Deliberate decision: "partial" is measured over the TIER set, so a project
    that maps only its own custom classes is the zero-declared case (silent),
    not a partial one.
    """
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"my-custom": "openai/custom"})
    assert _tier_warnings(caplog) == []


def test_partial_tier_warning_does_not_double_warn_with_3372_passthrough(caplog):
    """Tier 2: the omitted tier does not ALSO trip #3372's unknown-class warning (#3374).

    A tier present in the built-in catalog is by definition in the resolved
    namespace, so ``resolve()`` never takes its unknown-name branch for one —
    the two warnings cover disjoint inputs rather than stacking on this one.
    """
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        r = ModelResolver({"light": "openai/my-light"})
        caplog.clear()
        r.resolve("standard")  # an omitted-but-built-in tier
    assert caplog.messages == [], (
        f"resolving an omitted tier should emit nothing: {caplog.messages}"
    )


def test_builtin_disabled_has_no_fallback_to_warn_about(caplog):
    """Tier 2: with ``builtin={}`` there is no alias to fall back to (#3374).

    The warning's whole claim is "it resolves via reyn's built-in default" — a
    caller that disabled built-ins has no such default, so the warning would be
    false. Guards against pointing at a fallback that does not exist.
    """
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"light": "openai/a"}, builtin={})
    assert _tier_warnings(caplog) == []


def test_standard_classes_is_derived_from_the_tier_alias_producer():
    """Tier 2: STANDARD_CLASSES derives from BUILTIN_TIER_ALIASES, not a copy (#3374).

    Two hand-written lists of the tier names would drift the moment a tier is
    added to one of them. Falsification: pre-fix, STANDARD_CLASSES was an
    independent literal tuple that a new alias would not have reached.
    """
    assert STANDARD_CLASSES == tuple(BUILTIN_TIER_ALIASES)


# ── #1454 PR-B: resolve_class_or_fallback (the closed-world class gate) ──────


def test_resolve_class_or_fallback_known_class_is_honoured():
    """Tier 2: #1454 — a requested value that IS a known class is returned."""
    r = ModelResolver({"strong": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback("strong", "standard", where="t") == "strong"


def test_resolve_class_or_fallback_unknown_falls_back():
    """Tier 2: #1454 — a non-class value (e.g. an LLM-injected literal model
    string) is rejected and the trusted fallback is returned (closed-world:
    op/skill-supplied class-typed fields never pass through as a name)."""
    r = ModelResolver({"standard": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback(
        "gpt-3.5-turbo", "standard", where="t",
    ) == "standard"
    # even a provider-prefixed literal is rejected here — class position only
    assert r.resolve_class_or_fallback(
        "openai/gpt-4o", "standard", where="t",
    ) == "standard"


def test_resolve_class_or_fallback_none_requested_uses_fallback():
    """Tier 2: #1454 — no requested class → the fallback is used as-is."""
    r = ModelResolver({"standard": "openai/gpt-4o"}, builtin={})
    assert r.resolve_class_or_fallback(None, "standard", where="t") == "standard"


def test_resolve_class_or_fallback_none_everywhere_defaults_standard():
    """Tier 2: #1454 — requested and fallback both absent → 'standard'."""
    r = ModelResolver({}, builtin={})
    assert r.resolve_class_or_fallback(None, None, where="t") == "standard"


def test_bare_model_name_warns_prefixed_does_not(caplog):
    """Tier 2: #1454 PR-B — a name position (models[*].model) lacking a '/'
    provider prefix warns at load (degraded-but-allowed); a prefixed name is
    silent. The class/name unified rule's name-position leg."""
    import logging

    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"bare": {"model": "gpt-4o-mini"}}, builtin={})
    assert any("no provider prefix" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="reyn.llm.model_resolver"):
        ModelResolver({"ok": {"model": "openai/gpt-4o"}}, builtin={})
    assert not any("no provider prefix" in r.message for r in caplog.records)
