"""Tier 1: #4975 — `audit_events.provider_body_include_text` /
`audit_events.provider_body_max_chars` config parsing.

Mirrors `tests/config/test_4496_pr2_audit_events_backend_config.py`'s own
pattern (`_build_audit_events_config` is the read-side of the same
config this repo already parses this way — #4479's malformed-value-
falls-back-to-default discipline, applied here to the cap's own
int-coercion and non-positive-value cases, neither of which
`LocalEventBackend`'s own constructor defaults exercise since it never
sees a malformed raw dict).
"""
from __future__ import annotations

from reyn.config.infra import AuditEventsConfig, _build_audit_events_config


def test_default_provider_body_include_text_is_false():
    """Tier 1: the shipped default is off — no operator action needed to
    keep provider_body/provider_response hidden."""
    cfg = _build_audit_events_config(None)
    assert cfg.provider_body_include_text is False


def test_explicit_true_parses_through():
    """Tier 1: an operator's own opt-in reaches the config object."""
    cfg = _build_audit_events_config({"provider_body_include_text": True})
    assert cfg.provider_body_include_text is True


def test_default_provider_body_max_chars_is_4000():
    """Tier 1: the shipped default cap."""
    cfg = _build_audit_events_config(None)
    assert cfg.provider_body_max_chars == 4000


def test_explicit_max_chars_parses_through():
    """Tier 1: an operator's own cap reaches the config object."""
    cfg = _build_audit_events_config({"provider_body_max_chars": 500})
    assert cfg.provider_body_max_chars == 500


def test_string_digit_max_chars_coerces_to_int():
    """Tier 1: a YAML value that arrives as a string digit (not unusual
    for hand-edited config) is coerced, not rejected."""
    cfg = _build_audit_events_config({"provider_body_max_chars": "250"})
    assert cfg.provider_body_max_chars == 250


def test_non_positive_max_chars_falls_back_to_default():
    """Tier 1: a cap of 0 or negative would hide every provider_body even
    when the meet holds, silently defeating the operator's own opt-in —
    falls back to the default instead."""
    cfg = _build_audit_events_config({"provider_body_max_chars": 0})
    assert cfg.provider_body_max_chars == 4000

    cfg = _build_audit_events_config({"provider_body_max_chars": -5})
    assert cfg.provider_body_max_chars == 4000


def test_unparseable_max_chars_falls_back_to_default():
    """Tier 1: a non-numeric value falls back cleanly (#4479 precedent),
    same discipline as every other malformed value in this parser."""
    cfg = _build_audit_events_config({"provider_body_max_chars": "not-a-number"})
    assert cfg.provider_body_max_chars == 4000


def test_malformed_top_level_raw_still_returns_provider_body_defaults():
    """Tier 1: not a dict at all → defaults, provider_body fields included."""
    cfg = _build_audit_events_config("not-a-dict")
    assert cfg == AuditEventsConfig()
    assert cfg.provider_body_include_text is False
    assert cfg.provider_body_max_chars == 4000
