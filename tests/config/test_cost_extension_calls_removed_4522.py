"""Tier 1: #4522 — `cost.*.extension_calls` removed (deprecation-warn +
ignore, matching `ask_on_exceed`'s own #1877 precedent exactly).

Investigation trace (recorded here so a future reader has it without
re-deriving): `extension_calls`'s only real, tested implementation was
the `per_chain_skill_calls` budget-extension flow (#1877/#1879 —
`Session._ask_budget_extension` / `SkillRunner`'s spawn gate). That whole
subsystem was deliberately, audit-approved removed in #2448 ("skill
machinery is gone → zero live callers"). The field's NAME lived on here
only because every `CostLimitConfig` (`daily_tokens`/`per_agent_tokens`/
etc.) shares one dataclass — none of THOSE dimensions ever had a working
consumer of their own. Declared, parsed, never read — CLAUDE.md's
testing-policy six-questions ③ names this exact class (#3850).
"""
from __future__ import annotations

import warnings

from reyn.config.chat import _build_cost_limit
from reyn.runtime.budget.budget import CostLimitConfig


def test_extension_calls_key_emits_a_deprecation_warning():
    """Tier 1: an operator who still sets `extension_calls` gets a
    DeprecationWarning naming #4522 and #2448 — safety/budget config, so
    a silent drop (matching `ask_on_exceed`'s own precedent) is worse
    than staying silent."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_cost_limit({"hard_limit": 10.0, "extension_calls": 5})
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert any("extension_calls" in str(w.message) for w in deprecation), (
        f"expected a DeprecationWarning naming extension_calls, got: "
        f"{[str(w.message) for w in caught]}"
    )


def test_extension_calls_value_is_ignored_not_stored():
    """Tier 1: the value itself is dropped — CostLimitConfig no longer has
    an extension_calls field to store it in at all (the field itself was
    removed, not merely left at its default)."""
    cfg = _build_cost_limit({"hard_limit": 10.0, "extension_calls": 5})
    assert not hasattr(cfg, "extension_calls")


def test_hard_limit_and_warn_ratio_still_parse_unaffected():
    """Tier 1: regression guard — the other two CostLimitConfig fields
    parse exactly as before; only extension_calls changed."""
    cfg = _build_cost_limit({"hard_limit": 25.0, "warn_ratio": 0.5})
    assert cfg.hard_limit == 25.0
    assert cfg.warn_ratio == 0.5


def test_no_extension_calls_key_emits_no_warning():
    """Tier 1: accept-side — a config that never mentions extension_calls
    (the common, correct case going forward) triggers no warning at all."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _build_cost_limit({"hard_limit": 10.0})
    deprecation = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not any("extension_calls" in str(w.message) for w in deprecation)


def test_cost_limit_config_dataclass_has_no_extension_calls_field():
    """Tier 1: the dataclass itself no longer declares the field — a
    stronger guard than the parser alone, so a future call site
    constructing CostLimitConfig directly (bypassing the parser) can't
    silently re-populate it either."""
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(CostLimitConfig)}
    assert "extension_calls" not in field_names
