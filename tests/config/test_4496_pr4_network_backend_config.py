"""Tier 1: #4496 PR-4 — `audit_events.on_failure` / `network_endpoint` /
`network_timeout_s` / `network_spool_max_bytes` config parsing.

Mirrors `tests/config/test_4496_pr2_audit_events_backend_config.py`'s own
pattern (`_build_audit_events_config` is the read-side of the same config
this repo already parses this way — #4479's malformed-value-falls-back-to-
default discipline, applied here to the 3 new PR-4 fields).
"""
from __future__ import annotations

from reyn.config.infra import AuditEventsConfig, _build_audit_events_config


def test_default_on_failure_is_discard():
    """Tier 1: the owner's own ruling (issue #4496, 2026-08-13 verbatim:
    "呼び戻しがないと reyn 動けないわけじゃない") — discard-and-continue is
    the shipped default, not something an operator must opt into."""
    cfg = _build_audit_events_config(None)
    assert cfg.on_failure == "discard"


def test_explicit_spool_parses_through():
    """Tier 1: `spool` is a real, wired opt-in value."""
    cfg = _build_audit_events_config({"on_failure": "spool"})
    assert cfg.on_failure == "spool"


def test_unrecognized_on_failure_falls_back_to_discard():
    """Tier 1: an operator typo (or a rejected value like "stop" — #4496's
    own design thread explicitly rejects a "halt the run" option) falls
    back to the default rather than reaching `_build_events_backend` with
    a value nothing can resolve."""
    cfg = _build_audit_events_config({"on_failure": "stop"})
    assert cfg.on_failure == "discard"


def test_default_network_endpoint_is_empty():
    """Tier 1: no endpoint configured by default — `backend: network` with
    this default has nothing to send to (see the `backend` parsing test's
    own no-endpoint fallback)."""
    cfg = _build_audit_events_config(None)
    assert cfg.network_endpoint == ""


def test_explicit_network_endpoint_parses_through():
    """Tier 1: an operator's own endpoint reaches the config object."""
    cfg = _build_audit_events_config({"network_endpoint": "http://example.invalid/x"})
    assert cfg.network_endpoint == "http://example.invalid/x"


def test_default_network_timeout_s_is_5():
    """Tier 1: the shipped per-POST timeout default."""
    cfg = _build_audit_events_config(None)
    assert cfg.network_timeout_s == 5.0


def test_non_positive_network_timeout_s_falls_back_to_default():
    """Tier 1: a timeout of 0 or negative would mean "never time out" or
    something nonsensical — falls back to the default instead, same
    discipline as every other numeric field in this parser."""
    cfg = _build_audit_events_config({"network_timeout_s": 0})
    assert cfg.network_timeout_s == 5.0
    cfg = _build_audit_events_config({"network_timeout_s": -1})
    assert cfg.network_timeout_s == 5.0


def test_unparseable_network_timeout_s_falls_back_to_default():
    """Tier 1: a non-numeric value falls back cleanly (#4479 precedent)."""
    cfg = _build_audit_events_config({"network_timeout_s": "soon"})
    assert cfg.network_timeout_s == 5.0


def test_default_network_spool_max_bytes_is_10mb():
    """Tier 1: the shipped spool cap default, same as `max_bytes`'s own
    default above (architect's "spool + capacity limit" framing)."""
    cfg = _build_audit_events_config(None)
    assert cfg.network_spool_max_bytes == 10 * 1024 * 1024


def test_explicit_network_spool_max_bytes_parses_through():
    """Tier 1: an operator's own spool cap reaches the config object."""
    cfg = _build_audit_events_config({"network_spool_max_bytes": 500})
    assert cfg.network_spool_max_bytes == 500


def test_non_positive_network_spool_max_bytes_falls_back_to_default():
    """Tier 1: a cap of 0 or negative would defeat the capacity limit's
    own purpose — falls back to the default instead."""
    cfg = _build_audit_events_config({"network_spool_max_bytes": 0})
    assert cfg.network_spool_max_bytes == 10 * 1024 * 1024


def test_malformed_top_level_raw_still_returns_pr4_field_defaults():
    """Tier 1: not a dict at all → defaults, all 4 PR-4 fields included."""
    cfg = _build_audit_events_config("not-a-dict")
    assert cfg == AuditEventsConfig()
    assert cfg.on_failure == "discard"
    assert cfg.network_endpoint == ""
    assert cfg.network_timeout_s == 5.0
    assert cfg.network_spool_max_bytes == 10 * 1024 * 1024
