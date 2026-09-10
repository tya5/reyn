"""Tier 1: #4496 PR-2/PR-4 — `audit_events.backend` config parsing.

`local` (default, preserves pre-PR-2 behavior unchanged) / `discard`
(sink-null, wired) / `network` (PR-4, wired — requires `network_endpoint`,
see below). An operator who sets an unrecognized string gets the standard
malformed-value-falls-back-to-default discipline this parser already uses
for its other fields (#4479 precedent), not a raise and not a silent
accept of a string nothing can resolve to a real backend.
"""
from __future__ import annotations

from reyn.config.infra import AuditEventsConfig, _build_audit_events_config


def test_default_backend_is_local():
    """Tier 1: the shipped default — no operator action needed to keep the
    pre-PR-2 shape (audit-events land under `.reyn/events` as before)."""
    cfg = _build_audit_events_config(None)
    assert cfg.backend == "local"


def test_explicit_local_parses_through():
    """Tier 1: an operator who names the default explicitly still gets it."""
    cfg = _build_audit_events_config({"backend": "local"})
    assert cfg.backend == "local"


def test_discard_parses_through():
    """Tier 1: `discard` is a real, wired value — read, not ignored."""
    cfg = _build_audit_events_config({"backend": "discard"})
    assert cfg.backend == "discard"


def test_network_with_endpoint_parses_through():
    """Tier 1: #4496 PR-4 — `network` is now a real, wired value, AS LONG
    AS an endpoint is configured alongside it (see the no-endpoint test
    below for the fallback that protects `_build_events_backend` from
    reaching a half-built backend)."""
    cfg = _build_audit_events_config(
        {"backend": "network", "network_endpoint": "http://example.invalid/events"},
    )
    assert cfg.backend == "network"
    assert cfg.network_endpoint == "http://example.invalid/events"


def test_network_without_endpoint_falls_back_to_default():
    """Tier 1: #4496 PR-4's own documented choice (not specified by the
    issue thread) — `backend: network` with no usable endpoint configured
    cannot build a real `NetworkEventBackend`, so the WHOLE backend
    selection falls back to `local` rather than reaching
    `Session._build_events_backend` with a value it can't resolve. This
    premise REPLACES the pre-PR-4 test of the same shape (`network` used
    to have no implementation at all, so ANY value of it fell back) — now
    only the no-endpoint case does."""
    cfg = _build_audit_events_config({"backend": "network"})
    assert cfg.backend == "local"

    cfg = _build_audit_events_config({"backend": "network", "network_endpoint": "   "})
    assert cfg.backend == "local"


def test_unrecognized_string_falls_back_to_default():
    """Tier 1: an operator typo falls back cleanly, same discipline as
    every other malformed value in this parser (#4479 precedent)."""
    cfg = _build_audit_events_config({"backend": "s3"})
    assert cfg.backend == "local"


def test_malformed_top_level_raw_still_returns_backend_default():
    """Tier 1: not a dict at all → defaults, backend included."""
    cfg = _build_audit_events_config("not-a-dict")
    assert cfg == AuditEventsConfig()
    assert cfg.backend == "local"
