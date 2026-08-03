"""Tier 2: startup timing reports on screen, is off by default, and admits gaps.

An operator reported `reyn chat` taking minutes on Windows / git-bash, and reyn
could not say where the time went — 93% of startup happens before the first
audit event exists. They also cannot copy files off that machine, so the report
has to be readable in the terminal rather than written somewhere.

The assertions below are about the three properties that make it usable there:
nothing is printed unless asked, every declared stage appears even at zero, and
time the stages do not explain is stated rather than hidden.
"""
from __future__ import annotations

from reyn.runtime.startup_timing import STAGES, StartupTiming, enabled, stage


def test_it_is_off_unless_the_env_var_says_otherwise(monkeypatch) -> None:
    """Tier 2: default is off.

    Startup happens on every run, so unlike the #3539 loop tripwire there is no
    "the moment arrives unannounced" case for paying anything by default.
    """
    monkeypatch.delenv("REYN_STARTUP_TIMING", raising=False)

    assert enabled() is False


def test_the_flag_accepts_what_an_operator_would_type(monkeypatch) -> None:
    """Tier 2: `1`, `true`, `on` all work; a stray value does not enable it.

    The operator turning this on is already dealing with a slow startup and may
    be relaying instructions verbally — a flag that silently ignores `true`
    would look like the feature is broken.
    """
    for value in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("REYN_STARTUP_TIMING", value)
        assert enabled() is True, value

    for value in ("0", "false", "", "maybe"):
        monkeypatch.setenv("REYN_STARTUP_TIMING", value)
        assert enabled() is False, value


def test_a_stage_that_never_ran_is_still_reported() -> None:
    """Tier 2: every declared stage appears, at zero if it did not run.

    "This took no time" and "this is missing from the report" are different
    findings, and the second is the more interesting one. A report that omits
    what did not happen cannot tell them apart.
    """
    timing = StartupTiming()
    timing.record("config", 0.5)

    lines = timing.report_lines(wall_seconds=1.0)
    body = "\n".join(lines)

    for name in STAGES:
        assert name in body, f"{name} vanished from the report"


def test_a_repeated_stage_accumulates() -> None:
    """Tier 2: reading config twice shows as the total, not the last read.

    #3671's own defect list includes config being loaded more than once. A
    report that overwrote per stage would hide exactly the thing being hunted.
    """
    timing = StartupTiming()
    timing.record("config", 0.30)
    timing.record("config", 0.20)

    assert timing.total_seconds == 0.50


def test_time_the_stages_cannot_explain_is_stated() -> None:
    """Tier 2: the gap is a line, not a silence.

    The most useful thing this can say is "it is not in any of these". Without
    it, a tidy breakdown of 1% of a startup reads as an answer.
    """
    timing = StartupTiming()
    timing.record("config", 0.40)

    lines = timing.report_lines(wall_seconds=40.0)
    unaccounted = next(line for line in lines if "unaccounted" in line)

    assert timing.unaccounted_seconds(40.0) == 39.6
    assert "39.6" in unaccounted


def test_shares_are_of_wall_time_not_of_the_measured_sum() -> None:
    """Tier 2: a stage's percentage answers "of the startup", not "of what we measured".

    Sharing out the measured sum would print `config 100%` for a startup where
    config is 1% and something unmeasured is the rest — the exact misreading
    this issue is trying to escape.
    """
    timing = StartupTiming()
    timing.record("config", 0.40)

    config_line = next(line for line in timing.report_lines(40.0) if "config" in line)

    assert "1.0%" in config_line


def test_the_context_manager_records_elapsed_time() -> None:
    """Tier 2: `stage()` measures the block it wraps."""
    import time

    from reyn.runtime import startup_timing

    before = startup_timing.TIMING.total_seconds
    with stage("config"):
        time.sleep(0.02)

    assert startup_timing.TIMING.total_seconds - before >= 0.02
