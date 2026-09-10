"""Tier 1: #5990 (part of) — 13 `src/reyn/config/chat.py` parse-error sites
that used to swallow a malformed value with a bare
``except (TypeError, ValueError): x = default`` report NOTHING an
operator would ever see (the issue's own census class). Each site now
calls ``logging.getLogger(__name__).warning(...)`` on the SAME except
branch before falling back — each test below pins, per site, BOTH
halves (#5990 dispatch's own requirement): (a) the malformed value
produces a visible warning, and (b) the default is still what is
returned — a test checking only one half would pass even if the OTHER
broke (the warning silently removed, or the fallback logic silently
changed).
"""
from __future__ import annotations

import logging

from reyn.config.chat import (
    CostConfig,
    CostLimitConfig,
    CostWarnConfig,
    HistoryResidentConfig,
    ImageConfig,
    LogsConfig,
    ProcessMemoryConfig,
    ReadCapConfig,
    RenderTemplateConfig,
    TuiConfig,
    _build_cost_config,
    _build_cost_limit,
    _build_cost_warn_config,
    _build_history_resident_config,
    _build_image_config,
    _build_logs_config,
    _build_process_memory_config,
    _build_read_cap_config,
    _build_render_template_config,
    _build_tui_config,
)

_LOGGER_NAME = "reyn.config.chat"


def test_render_template_max_output_chars_warns_and_falls_back(caplog) -> None:
    """Tier 1: `render_template.max_output_chars` — invalid value warns
    AND the default (256_000) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_render_template_config({"max_output_chars": "oops"})
    assert cfg.max_output_chars == RenderTemplateConfig().max_output_chars
    assert any(
        "render_template.max_output_chars" in r.message for r in caplog.records
    )


def test_render_template_wall_clock_seconds_warns_and_falls_back(caplog) -> None:
    """Tier 1: `render_template.wall_clock_seconds` — invalid value warns
    AND the default (5.0) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_render_template_config({"wall_clock_seconds": "oops"})
    assert cfg.wall_clock_seconds == RenderTemplateConfig().wall_clock_seconds
    assert any(
        "render_template.wall_clock_seconds" in r.message for r in caplog.records
    )


def test_read_cap_inline_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `read_cap.inline_bytes` — invalid value warns AND the
    default (10_240) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_read_cap_config({"inline_bytes": "oops"})
    assert cfg.inline_bytes == ReadCapConfig().inline_bytes
    assert any("read_cap.inline_bytes" in r.message for r in caplog.records)


def test_history_resident_max_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `history_resident.max_bytes` — invalid value warns AND the
    default (256 MiB) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_history_resident_config({"max_bytes": "oops"})
    assert cfg.max_bytes == HistoryResidentConfig().max_bytes
    assert any("history_resident.max_bytes" in r.message for r in caplog.records)


def test_logs_max_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `logs.max_bytes` — invalid value warns AND the default
    (16 MiB) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_logs_config({"max_bytes": "oops"})
    assert cfg.max_bytes == LogsConfig().max_bytes
    assert any("logs.max_bytes" in r.message for r in caplog.records)


def test_logs_backup_count_warns_and_falls_back(caplog) -> None:
    """Tier 1: `logs.backup_count` — invalid value warns AND the default
    (4) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_logs_config({"backup_count": "oops"})
    assert cfg.backup_count == LogsConfig().backup_count
    assert any("logs.backup_count" in r.message for r in caplog.records)


def test_process_memory_max_bytes_warns_and_falls_back(caplog) -> None:
    """Tier 1: `process_memory.max_bytes` — invalid value warns AND falls
    back to the field's real default (None = no cap), never a magic
    number."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_process_memory_config({"max_bytes": "oops"})
    assert cfg.max_bytes == ProcessMemoryConfig().max_bytes is None
    assert any("process_memory.max_bytes" in r.message for r in caplog.records)


def test_image_row_height_cells_warns_and_falls_back(caplog) -> None:
    """Tier 1: `image.row_height_cells` — invalid value warns AND the
    default (20) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_image_config({"row_height_cells": "oops"})
    assert cfg.row_height_cells == ImageConfig().row_height_cells
    assert any("image.row_height_cells" in r.message for r in caplog.records)


def test_tui_context_usage_warn_percent_warns_and_falls_back(caplog) -> None:
    """Tier 1: `tui.context_usage_warn_percent` — invalid value warns AND
    the default (80) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_tui_config({"context_usage_warn_percent": "oops"})
    assert cfg.context_usage_warn_percent == TuiConfig().context_usage_warn_percent
    assert any(
        "tui.context_usage_warn_percent" in r.message for r in caplog.records
    )


def test_cost_warn_threshold_warns_and_falls_back(caplog) -> None:
    """Tier 1: `cost_warn.model_threshold_per_1m_input_usd` — invalid
    value warns AND the default ($5/1M) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_cost_warn_config(
            {"model_threshold_per_1m_input_usd": "oops"}
        )
    assert (
        cfg.model_threshold_per_1m_input_usd
        == CostWarnConfig().model_threshold_per_1m_input_usd
    )
    assert any(
        "cost_warn.model_threshold_per_1m_input_usd" in r.message
        for r in caplog.records
    )


def test_cost_limit_hard_limit_warns_and_falls_back(caplog) -> None:
    """Tier 1: `cost.*.hard_limit` (any `_build_cost_limit` namespace,
    e.g. `per_agent_tokens`/`daily_tokens`) — invalid value warns AND
    falls back to the field's real default (None = unlimited)."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_cost_limit({"hard_limit": "oops"})
    assert cfg.hard_limit == CostLimitConfig().hard_limit is None
    assert any("cost.*.hard_limit" in r.message for r in caplog.records)


def test_cost_limit_warn_ratio_warns_and_falls_back(caplog) -> None:
    """Tier 1: `cost.*.warn_ratio` — invalid value warns AND the default
    (0.8) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_cost_limit({"warn_ratio": "oops"})
    assert cfg.warn_ratio == 0.8
    assert any("cost.*.warn_ratio" in r.message for r in caplog.records)


def test_cost_rate_limit_warn_ratio_warns_and_falls_back(caplog) -> None:
    """Tier 1: `cost.rate_limit_warn_ratio` — invalid value warns AND the
    default (0.8) is still what ships."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_cost_config({"rate_limit_warn_ratio": "oops"})
    assert cfg.rate_limit_warn_ratio == CostConfig().rate_limit_warn_ratio == 0.8
    assert any("cost.rate_limit_warn_ratio" in r.message for r in caplog.records)


# ── negative controls: a well-formed value never warns ──────────────────────


def test_well_formed_render_template_values_do_not_warn(caplog) -> None:
    """Tier 1: negative control — a well-formed `render_template:` block
    parses through with no warning at all (the warning is specific to
    the malformed-value branch, not emitted unconditionally)."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_render_template_config(
            {"max_output_chars": 10, "wall_clock_seconds": 1.0}
        )
    assert cfg.max_output_chars == 10
    assert cfg.wall_clock_seconds == 1.0
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []


def test_well_formed_cost_config_values_do_not_warn(caplog) -> None:
    """Tier 1: negative control — a well-formed `cost:` block (including
    a nested `_build_cost_limit` namespace) parses through with no
    warning at all."""
    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        cfg = _build_cost_config(
            {"rate_limit_warn_ratio": 0.5, "per_agent_tokens": {"hard_limit": 10}}
        )
    assert cfg.rate_limit_warn_ratio == 0.5
    assert cfg.per_agent_tokens.hard_limit == 10.0
    assert [r for r in caplog.records if r.name == _LOGGER_NAME] == []
