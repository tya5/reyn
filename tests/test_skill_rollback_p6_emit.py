"""Tier 2: skill_rolled_back P6 event emission (FP-0006 E follow-up).

Four invariant tests for emit_cli_event and its wiring into cmd_rollback:

1. rollback emits a skill_rolled_back event with correct payload fields.
2. emit failure (monkeypatched) does not crash the CLI; rollback effects survive.
3. emit_cli_event creates .reyn/events/direct/cli/ idempotently on first call.
4. emit_cli_event is a graceful no-op (warn + return) outside a .reyn/ project.

No mocks of EventLog or EventStore — real instances are used. The only
monkeypatching is on emit_cli_event itself (test 2) to simulate a write failure
without touching collaborator internals.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pytest

from reyn.cli.commands.skill import cmd_rollback
from reyn.events.events import emit_cli_event

# ---------------------------------------------------------------------------
# helpers shared with test_skill_rollback_cli.py
# ---------------------------------------------------------------------------


def _rollback_args(skill_name: str, *, to: str | None = None) -> argparse.Namespace:
    ns = argparse.Namespace()
    ns.skill_name = skill_name
    ns.target_version = to
    return ns


def _make_project_skill(root: Path, skill_name: str, content: str = "current content") -> Path:
    skill_dir = root / "reyn" / "project" / skill_name
    skill_dir.mkdir(parents=True)
    skill_md = skill_dir / "skill.md"
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


def _make_versions(root: Path, skill_name: str, versions: dict[int, str], current: int) -> Path:
    ver_dir = root / ".reyn" / "skill-versions" / skill_name
    ver_dir.mkdir(parents=True)
    for num, content in versions.items():
        (ver_dir / f"v{num}.md").write_text(content, encoding="utf-8")
    (ver_dir / "current").write_text(str(current), encoding="utf-8")
    return ver_dir


def _read_all_cli_events(reyn_dir: Path) -> list[dict]:
    """Read every JSONL line from .reyn/events/direct/cli/**/*.jsonl."""
    cli_dir = reyn_dir / "events" / "direct" / "cli"
    events: list[dict] = []
    if not cli_dir.is_dir():
        return events
    for path in sorted(cli_dir.rglob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


# ---------------------------------------------------------------------------
# test 1: rollback emits skill_rolled_back with correct payload
# ---------------------------------------------------------------------------


def test_rollback_emits_skill_rolled_back_event(tmp_path, monkeypatch):
    """Tier 2: successful rollback writes a skill_rolled_back JSONL event with correct payload."""
    monkeypatch.chdir(tmp_path)

    _make_project_skill(tmp_path, "my_skill", "v3 content")
    _make_versions(
        tmp_path, "my_skill",
        {1: "v1 content", 2: "v2 content", 3: "v3 content"},
        current=3,
    )

    cmd_rollback(_rollback_args("my_skill", to="v2"))

    reyn_dir = tmp_path / ".reyn"
    events = _read_all_cli_events(reyn_dir)

    (ev,) = events

    assert ev["type"] == "skill_rolled_back"
    data = ev["data"]
    assert data["skill"] == "my_skill"
    assert data["from_version"] == 3
    assert data["to_version"] == 2
    assert data["reason"] == "user rollback via CLI"


# ---------------------------------------------------------------------------
# test 2: emit failure does not crash CLI; rollback effects survive
# ---------------------------------------------------------------------------


def test_rollback_emit_failure_does_not_crash_cli(tmp_path, monkeypatch, caplog):
    """Tier 2: if emit_cli_event raises, cmd_rollback still exits 0 and restores skill.md."""
    monkeypatch.chdir(tmp_path)

    skill_md = _make_project_skill(tmp_path, "my_skill", "v2 content")
    _make_versions(
        tmp_path, "my_skill",
        {1: "v1 content", 2: "v2 content"},
        current=2,
    )

    # Monkeypatch emit_cli_event inside the skill command module to raise.
    import reyn.cli.commands.skill as skill_mod

    def _raise(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise OSError("simulated disk full")

    monkeypatch.setattr(skill_mod, "emit_cli_event", _raise)

    with caplog.at_level(logging.WARNING, logger="reyn.cli.commands.skill"):
        # Must NOT raise SystemExit or any exception.
        cmd_rollback(_rollback_args("my_skill"))

    # (a) rollback effects applied — skill.md was restored to v1
    assert skill_md.read_text(encoding="utf-8") == "v1 content"

    # (b) current pointer updated
    current_file = tmp_path / ".reyn" / "skill-versions" / "my_skill" / "current"
    assert current_file.read_text(encoding="utf-8").strip() == "1"

    # (c) warning was logged (not raised)
    assert any("P6 emit failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# test 3: emit_cli_event creates dir idempotently
# ---------------------------------------------------------------------------


def test_emit_cli_event_creates_dir_idempotently(tmp_path, monkeypatch):
    """Tier 2: emit_cli_event creates .reyn/events/direct/cli/ on first call; second call appends."""
    monkeypatch.chdir(tmp_path)
    # Create a minimal .reyn/ so the helper finds the project root.
    (tmp_path / ".reyn").mkdir()

    cli_dir = tmp_path / ".reyn" / "events" / "direct" / "cli"
    assert not cli_dir.exists(), "pre-condition: dir should not exist yet"

    emit_cli_event("test_event_a", foo="bar")
    assert cli_dir.is_dir(), "dir should be created after first emit"

    events_after_first = _read_all_cli_events(tmp_path / ".reyn")
    (first_only,) = events_after_first

    # Second call — no exception, second event appended.
    emit_cli_event("test_event_b", baz=42)
    events_after_second = _read_all_cli_events(tmp_path / ".reyn")
    first_ev, second_ev = events_after_second

    types = {e["type"] for e in events_after_second}
    assert "test_event_a" in types
    assert "test_event_b" in types


# ---------------------------------------------------------------------------
# test 4: graceful no-op outside .reyn/ project
# ---------------------------------------------------------------------------


def test_emit_cli_event_no_op_when_outside_reyn_project(tmp_path, monkeypatch, caplog):
    """Tier 2: emit_cli_event warns and returns without raising when no .reyn/ dir exists."""
    # Use a tmp dir with no .reyn/ ancestor.
    no_reyn = tmp_path / "no_project_here"
    no_reyn.mkdir()
    monkeypatch.chdir(no_reyn)

    with caplog.at_level(logging.WARNING, logger="reyn.events.events"):
        # Must not raise.
        emit_cli_event("test_event", key="value")

    assert any("no .reyn/" in r.message for r in caplog.records), (
        "Expected a warning about missing .reyn/; got: "
        + str([r.message for r in caplog.records])
    )

    # No events written (no .reyn/ anywhere).
    assert not (no_reyn / ".reyn").exists()
