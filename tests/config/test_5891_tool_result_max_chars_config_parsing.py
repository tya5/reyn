"""Tier 1: #5891 — `audit_events.tool_result_max_chars` config parsing.

Mirrors `tests/config/test_4975_provider_body_config_parsing.py`'s own
pattern (`_build_audit_events_config` is the read-side of the same
config this repo already parses this way — #4479's malformed-value-
falls-back-to-default discipline, applied here to `tool_result_max_chars`'s
own int-coercion and non-positive-value cases). This is its OWN
dedicated knob, not a reuse of `provider_body_max_chars` — see
`AuditEventsConfig.tool_result_max_chars`'s own docstring for why.
"""
from __future__ import annotations

from reyn.config.infra import AuditEventsConfig, _build_audit_events_config


def test_default_tool_result_max_chars_is_4000():
    """Tier 1: the shipped default cap."""
    cfg = _build_audit_events_config(None)
    assert cfg.tool_result_max_chars == 4000


def test_explicit_max_chars_parses_through():
    """Tier 1: an operator's own cap reaches the config object."""
    cfg = _build_audit_events_config({"tool_result_max_chars": 500})
    assert cfg.tool_result_max_chars == 500


def test_string_digit_max_chars_coerces_to_int():
    """Tier 1: a YAML value that arrives as a string digit (not unusual
    for hand-edited config) is coerced, not rejected."""
    cfg = _build_audit_events_config({"tool_result_max_chars": "250"})
    assert cfg.tool_result_max_chars == 250


def test_non_positive_max_chars_falls_back_to_default():
    """Tier 1: a cap of 0 or negative would either disable the excerpt or
    invert its meaning — falls back to the default instead, same
    discipline as `provider_body_max_chars`."""
    cfg = _build_audit_events_config({"tool_result_max_chars": 0})
    assert cfg.tool_result_max_chars == 4000

    cfg = _build_audit_events_config({"tool_result_max_chars": -5})
    assert cfg.tool_result_max_chars == 4000


def test_unparseable_max_chars_falls_back_to_default():
    """Tier 1: a non-numeric value falls back cleanly (#4479 precedent),
    same discipline as every other malformed value in this parser."""
    cfg = _build_audit_events_config({"tool_result_max_chars": "not-a-number"})
    assert cfg.tool_result_max_chars == 4000


def test_malformed_top_level_raw_still_returns_tool_result_max_chars_default():
    """Tier 1: not a dict at all -> defaults, tool_result_max_chars included."""
    cfg = _build_audit_events_config("not-a-dict")
    assert cfg == AuditEventsConfig()
    assert cfg.tool_result_max_chars == 4000


def test_tool_result_max_chars_is_independent_of_provider_body_max_chars():
    """Tier 1: setting one does not move the other — #5891's own knob is
    a plain size bound with no relation to #4975's lattice-gated cap."""
    cfg = _build_audit_events_config({"tool_result_max_chars": 111})
    assert cfg.tool_result_max_chars == 111
    assert cfg.provider_body_max_chars == 4000

    cfg = _build_audit_events_config({"provider_body_max_chars": 222})
    assert cfg.provider_body_max_chars == 222
    assert cfg.tool_result_max_chars == 4000
