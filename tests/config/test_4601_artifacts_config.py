"""Tier 1: #4601 — `artifacts:` config parsing (the ref-table fallback's
own row cap).

`remote_fallback_limit` — a non-positive or non-numeric value falls back
to the default, same "malformed value falls back, never disables the cap
outright" discipline `_build_audit_events_config` (#4479) already
established for this module — an operator typo must not silently
reintroduce the exact unbounded-fallback defect #4601 exists to close.
"""
from __future__ import annotations

from reyn.config.infra import ArtifactsConfig, _build_artifacts_config


def test_default_is_fifty():
    """Tier 1: the shipped default — a UX-scale default (how many rows a
    human would scroll through), not a derived/measured number."""
    cfg = _build_artifacts_config(None)
    assert cfg.remote_fallback_limit == 50


def test_positive_value_passes_through():
    """Tier 1: an ordinary operator override parses through unmodified."""
    cfg = _build_artifacts_config({"remote_fallback_limit": 200})
    assert cfg.remote_fallback_limit == 200


def test_zero_falls_back_to_default():
    """Tier 1: (#4601, unlike #4479's audit_events axes) 0 does NOT mean
    "disabled" here — an unbounded fallback is exactly the defect this
    config knob exists to close, so 0 falls back to the default rather
    than reintroducing it."""
    cfg = _build_artifacts_config({"remote_fallback_limit": 0})
    assert cfg.remote_fallback_limit == ArtifactsConfig().remote_fallback_limit


def test_negative_value_falls_back_to_default():
    """Tier 1: a negative cap doesn't mean anything sensible — falls
    back rather than propagating nonsense into `list[:negative]` slicing."""
    cfg = _build_artifacts_config({"remote_fallback_limit": -5})
    assert cfg.remote_fallback_limit == ArtifactsConfig().remote_fallback_limit


def test_non_numeric_value_falls_back_to_default():
    """Tier 1: (accept-side) a malformed/non-numeric value falls back
    rather than raising or propagating a string into later int math."""
    cfg = _build_artifacts_config({"remote_fallback_limit": "not-a-number"})
    assert cfg.remote_fallback_limit == ArtifactsConfig().remote_fallback_limit


def test_non_dict_raw_returns_defaults():
    """Tier 1: (accept-side) no `artifacts:` block at all in reyn.yaml —
    `raw` is `None` (or any non-dict) — returns the plain defaults, not
    an error."""
    assert _build_artifacts_config(None) == ArtifactsConfig()
    assert _build_artifacts_config("not-a-dict") == ArtifactsConfig()
