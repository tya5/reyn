"""Tier 2: #3787 — ``ProjectContextWatcher`` detects an edit to the project
context file (AGENTS.md/REYN.md) at the turn boundary via mtime comparison,
and emits a ``project_context_changed`` P6 event exactly once per edit.

Read-only by design (architect ruling, #3787): the watcher never re-reads the
file's content into anything — it only compares ``stat().st_mtime_ns`` and
emits a detection signal. What consumes that signal (surface-only vs. actually
reloading the live prompt) is a separate, not-yet-made owner decision — this
test file covers ONLY the detection mechanism, never a reload.

No mocks: a real ``EventLog`` + real files on ``tmp_path``, real ``os.utime``
to force a distinguishable mtime.
"""
from __future__ import annotations

import os

from reyn.core.events.events import EventLog
from reyn.runtime.project_context_watch import ProjectContextWatcher
from tests._support.events import collect_events


def _touch(path, *, mtime_ns: int) -> None:
    path.write_text("content", encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_no_configured_path_never_fires(tmp_path) -> None:
    """Tier 2: #3787 — ``path=None`` (context disabled or never resolved) is a
    permanent no-op, never touches the event log."""
    log = EventLog()
    collected = collect_events(log)
    watcher = ProjectContextWatcher(path=None, events=log, scope="project")

    assert watcher.check() is False
    assert watcher.check() is False
    assert not [e for e in collected if e.type == "project_context_changed"]


def test_unchanged_file_never_fires(tmp_path) -> None:
    """Tier 2: #3787 — accept-side: a file whose mtime never moves produces no
    event across repeated turn-boundary checks (no false positives)."""
    agents_md = tmp_path / "AGENTS.md"
    _touch(agents_md, mtime_ns=1_000_000_000)
    log = EventLog()
    collected = collect_events(log)
    watcher = ProjectContextWatcher(path=agents_md, events=log, scope="project")

    for _ in range(3):
        assert watcher.check() is False
    assert not [e for e in collected if e.type == "project_context_changed"]


def test_edit_fires_exactly_once(tmp_path) -> None:
    """Tier 2: #3787 — an mtime change fires the event exactly once, then goes
    quiet again (idempotent per edit — a subsequent unchanged check does not
    repeat it)."""
    agents_md = tmp_path / "AGENTS.md"
    _touch(agents_md, mtime_ns=1_000_000_000)
    log = EventLog()
    collected = collect_events(log)
    watcher = ProjectContextWatcher(path=agents_md, events=log, scope="project")

    assert watcher.check() is False  # baseline, nothing changed yet

    _touch(agents_md, mtime_ns=2_000_000_000)  # the "edit"
    assert watcher.check() is True
    assert watcher.check() is False  # same mtime again — quiet
    assert watcher.check() is False

    (event,) = [e for e in collected if e.type == "project_context_changed"]
    assert event.data["path"] == str(agents_md)
    assert event.data["scope"] == "project"


def test_a_second_edit_fires_again(tmp_path) -> None:
    """Tier 2: #3787 — the detector re-arms: a SECOND edit after the first was
    observed fires its own event (not a one-shot "changed once, ever")."""
    agents_md = tmp_path / "AGENTS.md"
    _touch(agents_md, mtime_ns=1_000_000_000)
    log = EventLog()
    collected = collect_events(log)
    watcher = ProjectContextWatcher(path=agents_md, events=log, scope="project")
    watcher.check()  # baseline

    _touch(agents_md, mtime_ns=2_000_000_000)
    assert watcher.check() is True

    _touch(agents_md, mtime_ns=3_000_000_000)
    assert watcher.check() is True

    # Unpack-enforcement: exactly two fires, one per edit (not zero, not more).
    (first, second) = [e for e in collected if e.type == "project_context_changed"]
    assert first.data["path"] == second.data["path"] == str(agents_md)


def test_no_events_sink_is_a_safe_no_op(tmp_path) -> None:
    """Tier 2: #3787 — accept-side: ``events=None`` (no ambient EventLog, e.g.
    CLI one-shot paths) still detects the change (return value) without
    raising, it just cannot emit anywhere."""
    agents_md = tmp_path / "AGENTS.md"
    _touch(agents_md, mtime_ns=1_000_000_000)
    watcher = ProjectContextWatcher(path=agents_md, events=None, scope="project")
    watcher.check()

    _touch(agents_md, mtime_ns=2_000_000_000)
    assert watcher.check() is True  # detected, no raise, nothing to assert on a sink


def test_session_constructs_a_second_watcher_for_the_agent_own_file(tmp_path) -> None:
    """Tier 3: #3787 (owner ruling B) — a real ``Session`` constructs TWO
    ``ProjectContextWatcher`` instances, not one: the pre-existing
    ``_project_context_watcher`` (the project-wide file) and a new
    ``_agent_context_watcher`` pointed at THIS agent's own
    ``.reyn/agents/<agent_name>/AGENTS.md`` (``Session.workspace_dir``).
    Same class, reused unchanged — no new machinery. Real files, real
    ``EventLog``, no mocks; an edit to the agent's own file fires
    ``project_context_changed`` with a `path` that names the agent file, not
    the project one, so a consumer can tell the two apart without a new
    audit-event kind."""
    from reyn.config import SafetyConfig
    from reyn.core.events.state_log import StateLog
    from tests._support.agent_session import make_session

    workspace_state_dir = tmp_path / ".reyn"
    expected_agent_path = workspace_state_dir / "agents" / "t" / "AGENTS.md"
    # Constructed BEFORE the Session (matching this file's other tests'
    # pattern): the watcher's baseline mtime is captured at Session
    # construction, so the file must already exist with a known mtime by
    # then, or the FIRST check() sees "absent -> present" as a change of
    # its own (arguably correct — see ProjectContextWatcher's own
    # docstring — but not what this test is isolating).
    expected_agent_path.parent.mkdir(parents=True, exist_ok=True)
    _touch(expected_agent_path, mtime_ns=1_000_000_000)

    s = make_session(
        agent_name="t", model="standard",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / "snap.json",
        safety=SafetyConfig(),
        project_context="",
        workspace_state_dir=workspace_state_dir,
    )
    collected = collect_events(s)

    agent_watcher = s._agent_context_watcher
    watcher_path = agent_watcher._path
    assert watcher_path == expected_agent_path, (
        "Session must point the agent-side watcher at THIS agent's own "
        f"workspace file, not the project-wide one: {watcher_path!r}"
    )
    baseline_fired = agent_watcher.check()
    assert baseline_fired is False  # unchanged since construction

    _touch(expected_agent_path, mtime_ns=2_000_000_000)
    fired = agent_watcher.check()
    assert fired is True

    (event,) = [e for e in collected if e.type == "project_context_changed"]
    assert event.data["path"] == str(expected_agent_path), (
        "a consumer must be able to tell an agent-side edit apart from a "
        f"project-side one via the emitted path: {event.data!r}"
    )
    assert event.data["scope"] == "agent", (
        "#5742: a consumer must be able to tell the frame apart from a "
        f"typed field, not by sniffing path's shape: {event.data!r}"
    )
    # The pre-existing project-side watcher is untouched by this — it never
    # observed anything, since no project_context_path was configured.
    project_watcher = s._project_context_watcher
    project_fired = project_watcher.check()
    assert project_fired is False
