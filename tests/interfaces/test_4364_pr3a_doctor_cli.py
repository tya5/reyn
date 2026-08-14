"""Tier 2: #4364 PR-3a — ``reyn doctor`` skeleton + D-3 coverage disclosure
+ C-7 disk visibility.

No mocks — drives the real ``run`` against real on-disk state under
``tmp_path``, matching ``test_4488_storage_cli.py``'s own established
shape for this command family. ``select_purge_targets`` (the read-only
query C-7 uses to detect a declared-policy violation) is the SAME function
``reyn events purge`` and the automatic trigger both use — no re-derived
selection logic here.
"""
from __future__ import annotations

import argparse
from argparse import Namespace
from datetime import date, timedelta
from pathlib import Path

import pytest

from reyn.interfaces.cli.commands.doctor import (
    _MEASURABLE_LEAF_KEYS,
    _events_dir_stats,
    register,
    run,
)
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _write_yaml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_event_file(
    events_dir: Path, *, start_date: date, size_bytes: int = 22, suffix: str = "abc123",
) -> Path:
    """A real dated events .jsonl file, matching the real on-disk naming
    convention (YYYY-MM-DDTHHMMSS-<suffix>.jsonl) collect_dated_files parses.
    ``suffix`` must be unique per call within the same start_date+time or
    one write silently overwrites another."""
    subdir = events_dir / "agents" / "default" / "chat" / start_date.strftime("%Y-%m")
    subdir.mkdir(parents=True, exist_ok=True)
    path = subdir / f"{start_date.isoformat()}T000000-{suffix}.jsonl"
    path.write_text("x" * size_bytes, encoding="utf-8")
    return path


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    _write_yaml(tmp_path / "reyn.yaml", MINIMAL_REYN_YAML)
    return tmp_path


# ── Reachability (the exact shape #4478's own review flagged) ─────────────


def test_doctor_is_registered_on_the_reyn_parser():
    """Tier 2: (reachability) 'reyn doctor' is wired into the real top-level
    parser, not just importable in isolation — this is the 'declared,
    implemented, tested, invoked by nobody' shape #4478's review flagged.
    This exact test class would have caught this PR's own initial mistake:
    doctor.py was imported in commands/__init__.py but never added to the
    ALL list, so the subcommand was importable but not reachable through
    argparse — caught by a live smoke test before this test was written,
    now pinned so it can't silently regress."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    sub.required = True
    register(sub)

    args = parser.parse_args(["doctor", "--project-root", "/tmp"])
    assert args.func is run


def test_doctor_is_registered_via_the_real_command_registry():
    """Tier 2: (reachability) the ALL list in commands/__init__.py — not
    just this module's own register() — actually includes doctor. This is
    the specific list a forgotten entry silently drops a subcommand from
    while every other test (including the one above) stays green, since
    they call register() directly rather than through the real registry."""
    from reyn.interfaces.cli import build_parser

    parser = build_parser()
    # argparse exposes registered subparser names via the subparsers action.
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    assert "doctor" in subparsers_action.choices


# ── D-3: coverage disclosure ────────────────────────────────────────────


def test_coverage_disclosure_line_reports_measurable_and_uncovered(project, capsys):
    """Tier 2: the summary line states N total / M measurable / N-M
    uncovered, and the measurability criterion is printed alongside — not
    just the bare number (#4364 owner note: a scope claim must be
    derivable from this line, not asserted independently)."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    first_line = out.splitlines()[0]
    assert "config leaves total" in first_line
    assert "measurable effective surface" in first_line
    assert "uncovered" in first_line
    assert "Measurable means:" in out


def test_measurable_leaf_keys_are_real_schema_keys():
    """Tier 2: every key _MEASURABLE_LEAF_KEYS declares actually exists in
    the live config schema — a rename that doesn't update this list would
    silently claim to measure a leaf that no longer exists."""
    from reyn.config.config_schema import walk_config_schema

    real_keys = {node.key for node in walk_config_schema()}
    for key in _MEASURABLE_LEAF_KEYS:
        assert key in real_keys, f"{key!r} is not a real config schema key"


# ── C-7: .reyn/events/ — declared vs. actual ────────────────────────────


def test_events_section_is_absent_gracefully_when_no_events_dir_exists(project, capsys):
    """Tier 2: accept-side — a fresh project with no .reyn/events/ yet does
    not error or fabricate a finding."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "no .reyn/events/ directory yet" in out
    assert not (project / ".reyn" / "events").exists()


def test_events_reports_real_file_count_and_bytes(project, capsys):
    """Tier 2: actual on-disk state (not the config value) drives the
    reported count/bytes."""
    events_dir = project / ".reyn" / "events"
    _write_event_file(events_dir, start_date=date.today(), size_bytes=42, suffix="one")
    _write_event_file(events_dir, start_date=date.today(), size_bytes=8, suffix="two")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    actual_line = next(line for line in out.splitlines() if line.strip().startswith("actual:"))
    assert "2 file(s)" in actual_line
    assert "50 bytes" in actual_line  # 42 + 8


def test_events_dir_stats_count_and_bytes_stay_consistent_when_a_file_vanishes_mid_scan(
    project, monkeypatch,
):
    """Tier 2: #4671 census — a prior revision fixed ``count`` to
    ``len(files)`` BEFORE the per-file ``stat()`` loop, so a file that
    vanished mid-scan (e.g. a concurrent ``reyn events purge``) still
    counted toward ``count`` while silently not counting toward
    ``total_bytes`` — the two figures could disagree with no disclosure.
    Fixed by only incrementing ``count`` alongside a successful ``stat()``
    — this asserts both figures now describe the SAME population."""
    events_dir = project / ".reyn" / "events"
    survivor = _write_event_file(events_dir, start_date=date.today(), size_bytes=10, suffix="s")
    vanishing = _write_event_file(
        events_dir, start_date=date.today(), size_bytes=25, suffix="v",
    )

    real_stat = Path.stat

    def _stat_raises_for_vanishing(self, *args, **kwargs):
        if self == vanishing:
            raise FileNotFoundError(f"simulated race: {self} vanished mid-scan")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_raises_for_vanishing)

    count, total_bytes, _oldest = _events_dir_stats(events_dir)
    assert count == 1
    assert total_bytes == 10
    assert survivor.is_file()


def test_events_dir_stats_permission_error_is_not_swallowed(project, monkeypatch):
    """Tier 2: #4671 census — only ``FileNotFoundError`` (a race-vanished
    file) is treated as "skip silently". A ``PermissionError`` must
    propagate, not be absorbed into a quietly-undercounted total (D-1:
    measure, don't fake)."""
    events_dir = project / ".reyn" / "events"
    blocked = _write_event_file(events_dir, start_date=date.today(), size_bytes=10, suffix="b")

    real_stat = Path.stat

    def _stat_raises_permission_error(self, *args, **kwargs):
        if self == blocked:
            raise PermissionError(13, "Permission denied", str(self))
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _stat_raises_permission_error)

    with pytest.raises(PermissionError):
        _events_dir_stats(events_dir)


def test_a_declared_policy_violation_is_detected(project, capsys):
    """Tier 2: THE core C-7 value — a file older than the declared
    cleanup_period_days is flagged as a real, currently-unenforced
    violation. This is a live declared-vs-effective mismatch, not a
    hypothetical: reyn's own default cleanup_period_days=30 with a 45-day-
    old file reproduces exactly the #4480 concern (a policy exists but
    isn't demonstrably working)."""
    events_dir = project / ".reyn" / "events"
    old_date = date.today() - timedelta(days=45)
    _write_event_file(events_dir, start_date=old_date)

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "exceed the declared policy" in out
    assert "1 file(s) currently exceed" in out
    assert "45 day(s) old" in out


def test_a_compliant_state_shows_no_violation_warning(project, capsys):
    """Tier 2: accept-side — files well within the declared retention
    window must not trip the violation warning (a false positive here
    would teach an operator to ignore doctor's own output)."""
    events_dir = project / ".reyn" / "events"
    _write_event_file(events_dir, start_date=date.today())

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "exceed the declared policy" not in out
    assert "no file currently exceeds the declared policy" in out


def test_doctor_never_deletes_the_violating_file(project, capsys):
    """Tier 2: D-2 falsify — the exact file doctor reports as violating the
    declared policy must still exist on disk after doctor runs. select_
    purge_targets is a query; doctor must never call purge_files/
    apply_auto_purge on what it finds."""
    events_dir = project / ".reyn" / "events"
    old_date = date.today() - timedelta(days=45)
    violating_path = _write_event_file(events_dir, start_date=old_date)

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "exceed the declared policy" in out  # sanity: the case fired
    assert violating_path.is_file(), "doctor must never delete what it reports on"


# ── C-7: media/ / tool-results/ / history.jsonl visibility ────────────────


def test_no_declared_policy_section_lists_all_three_unowned_resources(project, capsys):
    """Tier 2: the #4480-motivated finding — media/, tool-results/, and
    history.jsonl each get their own visibility row, unconditionally
    (their value IS being listed, even at zero), since none has a declared
    retention policy to check compliance against."""
    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    assert "no declared retention policy" in out
    assert "media/:" in out
    assert "tool-results/:" in out
    assert "history.jsonl:" in out


def test_no_declared_policy_section_reflects_real_writes(project, capsys):
    """Tier 2: real on-disk history.jsonl content shows up, reusing the
    same aggregate_history_stats #4476's own storage command uses."""
    hist = project / ".reyn" / "agents" / "alice" / "history.jsonl"
    hist.parent.mkdir(parents=True)
    hist.write_text('{"seq": 1}\n{"seq": 2}\n', encoding="utf-8")

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    hist_line = next(line for line in out.splitlines() if line.strip().startswith("history.jsonl:"))
    assert "1 file(s)" in hist_line
    assert "2 turn(s)" in hist_line


def test_honors_an_explicit_project_root_not_cwd(project, capsys, monkeypatch):
    """Tier 2: --project-root overrides cwd, same contract as 'reyn storage
    stats' (#4488) — doctor must not implicitly assume cwd is the project."""
    events_dir = project / ".reyn" / "events"
    _write_event_file(events_dir, start_date=date.today())

    other_dir = project.parent / "elsewhere"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    run(Namespace(project_root=str(project)))
    out = capsys.readouterr().out
    actual_line = next(line for line in out.splitlines() if line.strip().startswith("actual:"))
    assert "1 file(s)" in actual_line
