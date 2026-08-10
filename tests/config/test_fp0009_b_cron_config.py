"""Tier 1: Contract tests for CronJobConfig / CronConfig / _build_cron_config (FP-0009 Component B).

These tests verify the public contract of the config-layer dataclasses and
the reyn.yaml parser for the ``cron:`` block.  No mocking; real config
loader called with tmp_path YAML files.

Cron jobs are message-based (``to`` + ``message``). A legacy bare ``skill``
name is not a valid shape — an entry without ``to`` + ``message`` is rejected
at load with a ValueError naming it (the skill-dependent warn-and-skip
tolerance was removed). The final test pins that rejection.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.config import (
    CronConfig,
    CronJobConfig,
    _build_cron_config,
    load_config,
)

# ── 1. CronJobConfig — direct construction ──────────────────────────────────


def test_cron_job_config_required_fields() -> None:
    """Tier 1: CronJobConfig constructs with required fields; defaults apply."""
    job = CronJobConfig(
        name="my_job", to="news_agent", message="summarise today", schedule="0 * * * *"
    )
    assert job.name == "my_job"
    assert job.to == "news_agent"
    assert job.message == "summarise today"
    assert job.schedule == "0 * * * *"
    assert job.input == {}      # default_factory
    assert job.enabled is True  # default


def test_cron_job_config_explicit_input_and_enabled() -> None:
    """Tier 1: CronJobConfig preserves explicit input dict and enabled=False."""
    job = CronJobConfig(
        name="weekly_report",
        to="ops_agent",
        message="weekly ops report",
        schedule="0 9 * * MON",
        input={"since_days": 7},
        enabled=False,
    )
    assert job.input == {"since_days": 7}
    assert job.enabled is False


# ── 2. CronConfig — direct construction ─────────────────────────────────────


def test_cron_config_default_empty() -> None:
    """Tier 1: CronConfig() constructs empty (= default factory)."""
    cfg = CronConfig()
    assert cfg.jobs == []


# ── 3. Parser: no cron block ─────────────────────────────────────────────────


def test_no_cron_block_gives_empty_jobs(tmp_path: Path) -> None:
    """Tier 1: reyn.yaml with no cron: block → ReynConfig.cron.jobs == []."""
    (tmp_path / "reyn.yaml").write_text("model: standard\n", encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.cron.jobs == []


# ── 4. Parser: valid cron block with multiple jobs ──────────────────────────


def test_cron_block_parsed_correctly(tmp_path: Path) -> None:
    """Tier 1: YAML with cron.jobs list → CronJobConfig entries with correct values."""
    yaml_content = """\
model: standard
cron:
  jobs:
    - name: morning_news
      to: news_agent
      message: "今日の主要ニュースをまとめて"
      schedule: "0 */6 * * *"
      input: {}
      enabled: true
    - name: weekly_ops_report
      to: ops_agent
      message: "weekly ops report"
      schedule: "0 9 * * MON"
      input:
        since_days: 7
      enabled: false
"""
    (tmp_path / "reyn.yaml").write_text(yaml_content, encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.cron.jobs, "Expected at least one cron job parsed"
    job0 = cfg.cron.jobs[0]
    job1 = cfg.cron.jobs[1]  # IndexError if fewer than 2 jobs parsed
    assert not cfg.cron.jobs[2:], f"Expected exactly 2 cron jobs, got extras: {cfg.cron.jobs[2:]}"
    assert isinstance(job0, CronJobConfig)
    assert job0.name == "morning_news"
    assert job0.to == "news_agent"
    assert job0.schedule == "0 */6 * * *"
    assert job0.input == {}
    assert job0.enabled is True
    assert isinstance(job1, CronJobConfig)
    assert job1.name == "weekly_ops_report"
    assert job1.to == "ops_agent"
    assert job1.schedule == "0 9 * * MON"
    assert job1.input == {"since_days": 7}
    assert job1.enabled is False


# ── 5–7. Validation: missing required fields raise ValueError ────────────────


def test_missing_name_raises_value_error() -> None:
    """Tier 1: missing 'name' in a cron job raises ValueError naming the entry."""
    raw = {"jobs": [{"to": "a", "message": "m", "schedule": "0 * * * *"}]}
    with pytest.raises(ValueError, match="name"):
        _build_cron_config(raw)


def test_missing_to_message_raises_value_error() -> None:
    """Tier 1: a job with neither a message shape nor a (legacy) skill raises ValueError."""
    raw = {"jobs": [{"name": "my_job", "schedule": "0 * * * *"}]}
    with pytest.raises(ValueError, match="to.*message"):
        _build_cron_config(raw)


def test_missing_schedule_raises_value_error() -> None:
    """Tier 1: missing 'schedule' in a cron job raises ValueError naming the entry."""
    raw = {"jobs": [{"name": "my_job", "to": "a", "message": "m"}]}
    with pytest.raises(ValueError, match="schedule"):
        _build_cron_config(raw)


def test_empty_name_raises_value_error() -> None:
    """Tier 1: empty string 'name' also raises ValueError."""
    raw = {"jobs": [{"name": "", "to": "a", "message": "m", "schedule": "0 * * * *"}]}
    with pytest.raises(ValueError, match="name"):
        _build_cron_config(raw)


# ── 8. enabled: false preserved ──────────────────────────────────────────────


def test_enabled_false_preserved_via_parser(tmp_path: Path) -> None:
    """Tier 1: enabled: false is preserved by the parser (entry exists, scheduler skips later)."""
    yaml_content = """\
model: standard
cron:
  jobs:
    - name: disabled_job
      to: some_agent
      message: "do the thing"
      schedule: "0 0 * * *"
      enabled: false
"""
    (tmp_path / "reyn.yaml").write_text(yaml_content, encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.cron.jobs, "Expected exactly one cron job parsed"
    assert not cfg.cron.jobs[1:], f"Expected exactly one cron job, got extras: {cfg.cron.jobs[1:]}"
    assert cfg.cron.jobs[0].enabled is False


# ── 9. input dict preserved verbatim ─────────────────────────────────────────


def test_input_dict_preserved_verbatim(tmp_path: Path) -> None:
    """Tier 1: input: {since_days: 7} passes through the parser unchanged."""
    yaml_content = """\
model: standard
cron:
  jobs:
    - name: report_job
      to: ops_agent
      message: "ops report"
      schedule: "0 9 * * MON"
      input:
        since_days: 7
        format: markdown
"""
    (tmp_path / "reyn.yaml").write_text(yaml_content, encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.cron.jobs[0].input == {"since_days": 7, "format": "markdown"}


# ── Edge cases: graceful degradation ─────────────────────────────────────────


def test_empty_cron_block_gives_empty_jobs(tmp_path: Path) -> None:
    """Tier 1: empty cron: block (no jobs key) → CronConfig(jobs=[])."""
    (tmp_path / "reyn.yaml").write_text("model: standard\ncron: {}\n", encoding="utf-8")
    cfg = load_config(cwd=tmp_path)
    assert cfg.cron.jobs == []


def test_cron_block_none_raw() -> None:
    """Tier 1: _build_cron_config(None) returns CronConfig(jobs=[]) gracefully."""
    result = _build_cron_config(None)
    assert result == CronConfig()


def test_cron_block_non_dict_raw() -> None:
    """Tier 1: _build_cron_config with a non-dict value returns empty CronConfig gracefully."""
    result = _build_cron_config("invalid")
    assert result == CronConfig()


# ── Legacy skill-based jobs are rejected (skill-dependent tolerance removed) ──


def test_legacy_skill_based_job_rejected() -> None:
    """Tier 1: a legacy bare-``skill`` cron entry (no ``to`` + ``message``) is
    rejected at load with a ValueError naming it. The skill-dependent
    warn-and-skip tolerance was removed (compat cleanup, pre-release): an
    entry without the message shape now raises rather than silently degrading.

    The kept message-based path is unaffected — a message-only config parses.

    FALSIFICATION: if the removed warn-and-skip branch were still present, the
    bare-``skill`` entry would be skipped and ``_build_cron_config`` would
    return without raising — so ``pytest.raises`` would go RED.
    """
    # KEEP: a message-based entry parses unaffected.
    ok = _build_cron_config({
        "jobs": [
            {"name": "morning_news", "to": "news_agent",
             "message": "summarise today", "schedule": "0 9 * * *"},
        ]
    })
    assert [j.name for j in ok.jobs] == ["morning_news"]
    assert ok.jobs[0].to == "news_agent"
    assert ok.jobs[0].message == "summarise today"

    # CHANGE: a legacy bare-``skill`` entry (no to/message) now raises,
    # naming the offending entry (was warn-and-skip before the removal).
    raw = {
        "jobs": [
            {"name": "legacy_index", "skill": "index_events", "schedule": "0 */6 * * *"},
        ]
    }
    with pytest.raises(ValueError, match="legacy_index"):
        _build_cron_config(raw)
