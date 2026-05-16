"""Tier 2: FP-0009 Component B — `reyn cron` CLI surface.

Pins the contract for the `reyn cron` subcommands:
  - Subcommand registration: run / list / status visible in help
  - `reyn cron list` with no cron block prints "(no jobs configured)", exit 0
  - `reyn cron list` with 2 jobs prints both rows, exit 0
  - `reyn cron list` shows next_run_at for valid cron expressions
  - `reyn cron status` shows extended last-run fields (empty in standalone mode)
  - `reyn cron --help` lists run / list / status
  - Unknown subcommand exits 1 with clear error

Does NOT test `reyn cron run` foreground execution (= Tier 4 / e2e territory).
Argparse wiring for `run` subcommand is covered by the help-text tests.

Test strategy: uses the real CronJob / CronScheduler from reyn.cron; no
MagicMock / AsyncMock / patch of collaborators.  Config is constructed
directly via CronConfig + CronJobConfig dataclasses and monkeypatched at the
reyn.config module attribute level (same pattern as test_fp0016_c_auth_cli.py).
"""
from __future__ import annotations

import argparse
import contextlib
import io

import pytest

from reyn.cli import build_parser


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_help(parser: argparse.ArgumentParser, *args: str) -> str:
    """Capture --help output (SystemExit code 0) and return stdout."""
    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc_info:
        with contextlib.redirect_stdout(buf):
            parser.parse_args(list(args))
    assert exc_info.value.code == 0
    return buf.getvalue()


def _make_cron_config(jobs: list | None = None):
    """Build a minimal ReynConfig with a CronConfig.

    Uses real dataclasses, no mock objects.
    """
    from reyn.config import CronConfig, CronJobConfig, ReynConfig

    job_configs = [
        CronJobConfig(
            name=j["name"],
            skill=j["skill"],
            schedule=j["schedule"],
            input=j.get("input", {}),
            enabled=j.get("enabled", True),
        )
        for j in (jobs or [])
    ]
    return ReynConfig(cron=CronConfig(jobs=job_configs))


# ---------------------------------------------------------------------------
# 1. Subcommand registration
# ---------------------------------------------------------------------------


def test_cron_subcommand_registered() -> None:
    """Tier 2: build_parser() includes 'cron' in top-level help."""
    parser = build_parser()
    help_text = parser.format_help()
    assert "cron" in help_text


def test_cron_help_lists_run_list_status() -> None:
    """Tier 2: reyn cron --help shows run, list, and status subcommands."""
    parser = build_parser()
    help_text = _parse_help(parser, "cron", "--help")
    assert "run" in help_text
    assert "list" in help_text
    assert "status" in help_text


def test_cron_run_help_exits_0() -> None:
    """Tier 2: reyn cron run --help exits 0."""
    parser = build_parser()
    help_text = _parse_help(parser, "cron", "run", "--help")
    assert "run" in help_text


def test_cron_list_help_exits_0() -> None:
    """Tier 2: reyn cron list --help exits 0."""
    parser = build_parser()
    help_text = _parse_help(parser, "cron", "list", "--help")
    assert "list" in help_text


def test_cron_status_help_exits_0() -> None:
    """Tier 2: reyn cron status --help exits 0."""
    parser = build_parser()
    help_text = _parse_help(parser, "cron", "status", "--help")
    assert "status" in help_text


# ---------------------------------------------------------------------------
# 2. Unknown subcommand exits 1
# ---------------------------------------------------------------------------


def test_cron_unknown_subcommand_exits_nonzero() -> None:
    """Tier 2: unknown subcommand produces a non-zero exit and error output."""
    parser = build_parser()
    buf = io.StringIO()
    with pytest.raises(SystemExit) as exc_info:
        with contextlib.redirect_stderr(buf):
            parser.parse_args(["cron", "frobnicate"])
    assert exc_info.value.code != 0
    # argparse always writes to stderr on parse error
    err = buf.getvalue()
    assert err  # some error message present


# ---------------------------------------------------------------------------
# 3. reyn cron list — empty state
# ---------------------------------------------------------------------------


def test_cron_list_no_jobs_prints_empty_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list with no cron block prints '(no jobs configured)'."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=[])
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    assert "(no jobs configured)" in captured.out


# ---------------------------------------------------------------------------
# 4. reyn cron list — 2 jobs printed
# ---------------------------------------------------------------------------


_SAMPLE_JOBS = [
    {
        "name": "index_events_hourly",
        "skill": "index_events",
        "schedule": "0 */6 * * *",
        "enabled": True,
    },
    {
        "name": "weekly_ops_report",
        "skill": "ops_report",
        "schedule": "0 9 * * MON",
        "enabled": True,
    },
]


def test_cron_list_two_jobs_prints_both_rows(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list with 2 jobs prints a row for each job name."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=_SAMPLE_JOBS)
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    assert "index_events_hourly" in captured.out
    assert "weekly_ops_report" in captured.out
    # Both skill names appear
    assert "index_events" in captured.out
    assert "ops_report" in captured.out


def test_cron_list_shows_header_columns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list output contains NAME, SKILL, SCHEDULE, ENABLED, NEXT RUN headers."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=_SAMPLE_JOBS)
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    for col in ("NAME", "SKILL", "SCHEDULE", "ENABLED", "NEXT RUN"):
        assert col in captured.out


# ---------------------------------------------------------------------------
# 5. reyn cron list — next_run_at computed for valid cron expressions
# ---------------------------------------------------------------------------


def test_cron_list_shows_next_run_for_valid_schedules(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list shows a non-'-' next_run_at value for a valid cron expression."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=[_SAMPLE_JOBS[0]])
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    # The next_run_at for a valid cron should be an ISO datetime string (contains "T" or ":")
    # and should not just be the fallback "-"
    out = captured.out
    assert "index_events_hourly" in out
    # At least one row should contain a timestamp-like string
    lines = [line for line in out.splitlines() if "index_events_hourly" in line]
    assert lines
    # The next_run field should not be missing ("-" with nothing else)
    row_line = lines[0]
    # A valid next_run will contain digits (year, hour, minute)
    import re
    assert re.search(r"\d{4}", row_line), (
        f"Expected a year in next_run, got: {row_line!r}"
    )


def test_cron_list_shows_dash_for_invalid_schedule(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list shows '-' for next_run_at when schedule expression is invalid."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=[{
        "name": "bad_job",
        "skill": "some_skill",
        "schedule": "NOT_A_CRON_EXPR",
        "enabled": True,
    }])
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    # The row should be present; next_run shown as "-"
    assert "bad_job" in captured.out
    lines = [line for line in captured.out.splitlines() if "bad_job" in line]
    assert lines
    row_line = lines[0]
    assert "-" in row_line


# ---------------------------------------------------------------------------
# 6. reyn cron status — extended columns present; last-run empty standalone
# ---------------------------------------------------------------------------


def test_cron_status_shows_last_run_columns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_status output contains LAST RUN AT, LAST STATUS, LAST ERROR columns."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_status

    cfg = _make_cron_config(jobs=_SAMPLE_JOBS)
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_status(argparse.Namespace())

    captured = capsys.readouterr()
    for col in ("LAST RUN AT", "LAST STATUS", "LAST ERROR"):
        assert col in captured.out


def test_cron_status_last_run_empty_standalone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_status in standalone mode shows '-' for last_run_* fields (no persist)."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_status

    cfg = _make_cron_config(jobs=[_SAMPLE_JOBS[0]])
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_status(argparse.Namespace())

    captured = capsys.readouterr()
    # Data rows should show "-" for all three last_run columns
    lines = [
        line for line in captured.out.splitlines()
        if "index_events_hourly" in line
    ]
    assert lines
    row_line = lines[0]
    # Three "-" placeholders for the three empty last-run columns
    assert row_line.count("-") >= 3


# ---------------------------------------------------------------------------
# 7. reyn cron list — enabled flag shown correctly
# ---------------------------------------------------------------------------


def test_cron_list_enabled_flag_shown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Tier 2: run_list shows 'true'/'false' for enabled field correctly."""
    import reyn.config as _cfg_mod
    from reyn.cli.commands.cron import run_list

    cfg = _make_cron_config(jobs=[
        {"name": "enabled_job", "skill": "sk1", "schedule": "0 * * * *", "enabled": True},
        {"name": "disabled_job", "skill": "sk2", "schedule": "0 * * * *", "enabled": False},
    ])
    monkeypatch.setattr(_cfg_mod, "load_config", lambda cwd=None: cfg)

    run_list(argparse.Namespace())

    captured = capsys.readouterr()
    enabled_lines = [l for l in captured.out.splitlines() if "enabled_job" in l]
    disabled_lines = [l for l in captured.out.splitlines() if "disabled_job" in l]
    assert enabled_lines and "true" in enabled_lines[0]
    assert disabled_lines and "false" in disabled_lines[0]
