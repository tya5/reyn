"""Tier 1: #4496 PR-2 — `audit_events.backend` config parsing.

`local` (default, preserves pre-PR-2 behavior unchanged) / `discard`
(sink-null, wired). `network` is a declared-future, not-yet-implemented
value (see `AuditEventsConfig.backend`'s own docstring for why) — an
operator who sets it, or any other unrecognized string, gets the standard
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


def test_network_falls_back_to_default_not_accepted_verbatim():
    """Tier 1: `network` is declared in the docstring as a future value but
    has no backend implementation behind it yet (#4496's open on-failure-
    semantics decision) — falls back to `local` rather than reaching
    `Session._build_events_backend` with a value it can't resolve."""
    cfg = _build_audit_events_config({"backend": "network"})
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
