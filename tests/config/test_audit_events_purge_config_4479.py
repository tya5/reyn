"""Tier 1: #4479 — `audit_events:` automatic-purge config parsing.

`cleanup_period_days` / `max_disk_usage_percent` — owner ruling: 0 disables
that axis (a deliberate reversal of this field's earlier stance, which
REJECTED 0 as a footgun). Covers defaults, the 0-disables shape, and the
malformed-input fallback discipline every other builder in this module
follows (a typo must not produce a nonsensical negative purge threshold).
"""
from __future__ import annotations

from reyn.config.infra import AuditEventsConfig, _build_audit_events_config


def test_defaults_are_thirty_days_and_ten_percent():
    """Tier 1: the shipped defaults — borrowed conventions (Claude Code's
    own cleanupPeriodDays; journald's own SystemMaxUse), not measurements."""
    cfg = _build_audit_events_config(None)
    assert cfg.cleanup_period_days == 30
    assert cfg.max_disk_usage_percent == 10.0


def test_zero_on_either_axis_disables_it_not_rejected():
    """Tier 1: #4479 owner ruling — 0 means disabled, parsed through
    cleanly (the OPPOSITE of this field's earlier ValueError-on-0 stance)."""
    cfg = _build_audit_events_config(
        {"cleanup_period_days": 0, "max_disk_usage_percent": 0},
    )
    assert cfg.cleanup_period_days == 0
    assert cfg.max_disk_usage_percent == 0.0


def test_positive_values_pass_through():
    """Tier 1: an ordinary operator override on both axes parses through
    unmodified."""
    cfg = _build_audit_events_config(
        {"cleanup_period_days": 7, "max_disk_usage_percent": 25.5},
    )
    assert cfg.cleanup_period_days == 7
    assert cfg.max_disk_usage_percent == 25.5


def test_negative_cleanup_period_days_falls_back_to_default():
    """Tier 1: a negative age doesn't mean anything sensible (delete
    future files?) — falls back rather than propagating nonsense, same
    discipline as every other numeric builder in this module."""
    cfg = _build_audit_events_config({"cleanup_period_days": -5})
    assert cfg.cleanup_period_days == AuditEventsConfig().cleanup_period_days


def test_negative_disk_usage_percent_falls_back_to_default():
    """Tier 1: a negative percentage is nonsensical (a budget below zero
    free space?) — falls back rather than propagating it."""
    cfg = _build_audit_events_config({"max_disk_usage_percent": -10})
    assert cfg.max_disk_usage_percent == AuditEventsConfig().max_disk_usage_percent


def test_non_numeric_values_fall_back_to_defaults():
    """Tier 1: an operator typo (a string where a number belongs) must not
    crash config loading — falls back to the documented defaults."""
    cfg = _build_audit_events_config(
        {"cleanup_period_days": "soon", "max_disk_usage_percent": "lots"},
    )
    assert cfg.cleanup_period_days == 30
    assert cfg.max_disk_usage_percent == 10.0


def test_malformed_top_level_raw_returns_defaults():
    """Tier 1: not a dict at all (e.g. a bare string in reyn.yaml) →
    defaults, not a crash."""
    cfg = _build_audit_events_config("not-a-dict")
    assert cfg == AuditEventsConfig()


def test_rotation_fields_still_parse_unchanged():
    """Tier 1: regression guard — the pre-existing max_bytes/max_age_seconds
    rotation fields are untouched by the #4479 purge-axis changes."""
    cfg = _build_audit_events_config({"max_bytes": 999, "max_age_seconds": 111})
    assert cfg.max_bytes == 999
    assert cfg.max_age_seconds == 111
