"""Tier 1: #5990 (part of) — 8 `src/reyn/config/infra.py` parse-error sites
that used to swallow a malformed value with a bare
``except (TypeError, ValueError): x = default`` report NOTHING an
operator would ever see. Each site now calls
``logging.getLogger(__name__).warning(...)`` on the SAME except branch
before falling back — each test below pins, per site, BOTH halves: (a)
the malformed value produces a visible warning, and (b) the default is
still what is returned.
"""
from __future__ import annotations

import logging

from reyn.config.infra import (
    ArtifactsConfig,
    AuditEventsConfig,
    FsWatchConfig,
    _build_artifacts_config,
    _build_audit_events_config,
    _build_fs_watch_config,
)

_LOGGER_NAME = "reyn.config.infra"


def test_fs_watch_debounce_seconds_warns_and_falls_back(caplog) -> None:
    """Tier 1: `fs_watch.debounce_seconds` — invalid value warns AND the
    default (0.2) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_fs_watch_config({"paths": ["/x"], "debounce_seconds": "oops"})
    assert cfg.debounce_seconds == FsWatchConfig().debounce_seconds
    assert any("fs_watch.debounce_seconds" in r.message for r in caplog.records)


def test_audit_events_cleanup_period_days_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.cleanup_period_days` — invalid value warns
    AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config({"cleanup_period_days": "oops"})
    assert cfg.cleanup_period_days == AuditEventsConfig().cleanup_period_days
    assert any(
        "audit_events.cleanup_period_days" in r.message for r in caplog.records
    )


def test_audit_events_max_disk_usage_percent_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.max_disk_usage_percent` — invalid value
    warns AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config({"max_disk_usage_percent": "oops"})
    assert cfg.max_disk_usage_percent == AuditEventsConfig().max_disk_usage_percent
    assert any(
        "audit_events.max_disk_usage_percent" in r.message for r in caplog.records
    )


def test_audit_events_coalesce_fragments_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.agent_delta_coalesce_fragments` — invalid
    value warns AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config(
            {"agent_delta_coalesce_fragments": "oops"}
        )
    assert (
        cfg.agent_delta_coalesce_fragments
        == AuditEventsConfig().agent_delta_coalesce_fragments
    )
    assert any(
        "audit_events.agent_delta_coalesce_fragments" in r.message
        for r in caplog.records
    )


def test_audit_events_coalesce_interval_ms_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.agent_delta_coalesce_interval_ms` — invalid
    value warns AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config(
            {"agent_delta_coalesce_interval_ms": "oops"}
        )
    assert (
        cfg.agent_delta_coalesce_interval_ms
        == AuditEventsConfig().agent_delta_coalesce_interval_ms
    )
    assert any(
        "audit_events.agent_delta_coalesce_interval_ms" in r.message
        for r in caplog.records
    )


def test_audit_events_provider_body_max_chars_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.provider_body_max_chars` — invalid value
    warns AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config({"provider_body_max_chars": "oops"})
    assert (
        cfg.provider_body_max_chars == AuditEventsConfig().provider_body_max_chars
    )
    assert any(
        "audit_events.provider_body_max_chars" in r.message for r in caplog.records
    )


def test_audit_events_tool_result_max_chars_warns_and_falls_back(caplog) -> None:
    """Tier 1: `audit_events.tool_result_max_chars` — invalid value warns
    AND the default (4000) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_audit_events_config({"tool_result_max_chars": "oops"})
    assert cfg.tool_result_max_chars == AuditEventsConfig().tool_result_max_chars
    assert any(
        "audit_events.tool_result_max_chars" in r.message for r in caplog.records
    )


def test_artifacts_remote_fallback_limit_warns_and_falls_back(caplog) -> None:
    """Tier 1: `artifacts.remote_fallback_limit` — invalid value warns
    AND the default is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_artifacts_config({"remote_fallback_limit": "oops"})
    assert cfg.remote_fallback_limit == ArtifactsConfig().remote_fallback_limit
    assert any(
        "artifacts.remote_fallback_limit" in r.message for r in caplog.records
    )


# ── negative controls: well-formed values never warn ─────────────────────────


def test_well_formed_fs_watch_and_audit_events_do_not_warn(caplog) -> None:
    """Tier 1: negative control — well-formed `fs_watch:`/`audit_events:`
    blocks parse through with no warning at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        fs_cfg = _build_fs_watch_config({"paths": ["/x"], "debounce_seconds": 0.5})
        audit_cfg = _build_audit_events_config(
            {"cleanup_period_days": 3, "max_disk_usage_percent": 5.0}
        )
    assert fs_cfg.debounce_seconds == 0.5
    assert audit_cfg.cleanup_period_days == 3
    assert audit_cfg.max_disk_usage_percent == 5.0
    assert caplog.records == []
