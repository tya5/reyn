"""Tier 2: #1679 — model_class_by_purpose.compaction is a REAL key (was a dead key).

`compaction` was a documented `model_class_by_purpose` purpose, but no call site
read it: both CompactionEngine sites resolved their model from a config-following
source (chat session = `self.model`, planner = the router-purpose model), so
setting `model_class_by_purpose.compaction` silently no-opped.

The fix wires both sites through `ModelResolver.purpose_class_or(purpose, default)`
— the per-purpose override if configured, else the *caller-supplied* fallback
(NOT the resolver's `default_class`). Keeping each site's existing fallback is what
makes the wiring byte-identical for every current config: it preserves
`self.model` / `router_model` even when those diverge from `default_class`, which
they do under `SkillRuntime.from_config(config, model=X)` (the model-override path). The
naive `resolve_purpose_class(None, …, "compaction")` would fall back to
`default_class` and silently move compaction OFF the agent's explicit model — a
behavior change masked as a wiring. `purpose_class_or` avoids that by construction.

Production call sites (grep-verified, non-test): `runtime/session.py` and
`chat/planner.py` both call `purpose_class_or("compaction", <site fallback>)`.

No mocks: real `ModelResolver` instances.
"""
from __future__ import annotations

from reyn.llm.model_resolver import ModelResolver

# A resolver whose default_class deliberately differs from the per-site fallback,
# modelling `SkillRuntime.from_config(config, model="agent_model")`: the session/planner
# fallback is the agent's model ("agent_model"), while the resolver's default_class
# is the config default ("config_default"). This is the divergence the fix must
# preserve.
_DEFAULT_CLASS = "config_default"
_SITE_FALLBACK = "agent_model"  # = self.model / router_model under a model override


def test_purpose_class_or_unset_uses_supplied_fallback_not_default_class() -> None:
    """Tier 2: #1679 (a) — compaction UNSET → the caller-supplied fallback, NOT the
    resolver's default_class. This is the no-regression proof for the model-override
    (SkillRuntime.from_config(model=X)) case: compaction must stay on the agent's model
    even though it differs from default_class. (Naive resolve_purpose_class would
    return default_class here = the divergence avoided.)"""
    r = ModelResolver({}, default_class=_DEFAULT_CLASS, purpose_classes={})
    assert r.purpose_class_or("compaction", _SITE_FALLBACK) == _SITE_FALLBACK
    # and it is NOT silently the default_class (= the bug we avoided)
    assert r.purpose_class_or("compaction", _SITE_FALLBACK) != _DEFAULT_CLASS


def test_purpose_class_or_set_resolves_to_configured_class() -> None:
    """Tier 2: #1679 (b) — compaction SET → the configured class wins over the
    site fallback. This is the dead-key-now-real proof: setting
    model_class_by_purpose.compaction now takes effect."""
    r = ModelResolver(
        {}, default_class=_DEFAULT_CLASS, purpose_classes={"compaction": "heavy"},
    )
    assert r.purpose_class_or("compaction", _SITE_FALLBACK) == "heavy"


def test_purpose_class_or_other_purpose_unaffected() -> None:
    """Tier 2: #1679 — a compaction override does not leak into other purposes;
    each purpose with no override still falls back to the supplied default."""
    r = ModelResolver(
        {}, default_class=_DEFAULT_CLASS, purpose_classes={"compaction": "heavy"},
    )
    assert r.purpose_class_or("router", _SITE_FALLBACK) == _SITE_FALLBACK


def test_class_for_purpose_still_uses_default_class() -> None:
    """Tier 2: #1679 — the class_for_purpose refactor (delegating to
    purpose_class_or with default_class) is behavior-preserving: an unset purpose
    still resolves to default_class, an override still wins (mirrors the #1672
    contract, which is unchanged)."""
    r = ModelResolver(
        {}, default_class=_DEFAULT_CLASS, purpose_classes={"router": "light"},
    )
    assert r.class_for_purpose("control_ir") == _DEFAULT_CLASS  # unset → default_class
    assert r.class_for_purpose("router") == "light"  # override wins
