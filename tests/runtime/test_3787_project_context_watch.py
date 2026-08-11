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
    watcher = ProjectContextWatcher(path=None, events=log)

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
    watcher = ProjectContextWatcher(path=agents_md, events=log)

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
    watcher = ProjectContextWatcher(path=agents_md, events=log)

    assert watcher.check() is False  # baseline, nothing changed yet

    _touch(agents_md, mtime_ns=2_000_000_000)  # the "edit"
    assert watcher.check() is True
    assert watcher.check() is False  # same mtime again — quiet
    assert watcher.check() is False

    (event,) = [e for e in collected if e.type == "project_context_changed"]
    assert event.data["path"] == str(agents_md)


def test_a_second_edit_fires_again(tmp_path) -> None:
    """Tier 2: #3787 — the detector re-arms: a SECOND edit after the first was
    observed fires its own event (not a one-shot "changed once, ever")."""
    agents_md = tmp_path / "AGENTS.md"
    _touch(agents_md, mtime_ns=1_000_000_000)
    log = EventLog()
    collected = collect_events(log)
    watcher = ProjectContextWatcher(path=agents_md, events=log)
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
    watcher = ProjectContextWatcher(path=agents_md, events=None)
    watcher.check()

    _touch(agents_md, mtime_ns=2_000_000_000)
    assert watcher.check() is True  # detected, no raise, nothing to assert on a sink
