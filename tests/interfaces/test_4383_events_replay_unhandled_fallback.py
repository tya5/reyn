"""Tier 2: #4383 — `reyn events` replay no longer silently drops an event
type ConsoleLogger has no dedicated `on_<type>` handler for.

Owner's real-environment report: `reyn events --filter compaction_shrink_recovered`
matched 21 events, printed 0 lines, and the summary line ("21 events
replayed") read as success — nothing distinguished "found and shown" from
"found and dropped". Root cause: `ConsoleLogger.__call__` (reporters/__init__.py)
only has dedicated renderers for 11 of the 211 declared audit-event kinds;
everything else fell through `getattr(self, f"on_{event.type}", None)`
returning `None` and was silently skipped.

Drives the REAL `run_replay` CLI entry point against a REAL JSONL file (not
a hand-called ConsoleLogger unit test) — the bug was in the combination
(replay counts everything, ConsoleLogger renders almost nothing, the
summary conflates the two), which only a real end-to-end pass through
`run_replay` actually exercises.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


def _replay_args(target: Path, *, filter_types: "list[str] | None" = None) -> argparse.Namespace:
    return argparse.Namespace(
        target=str(target),
        filter_types=filter_types or [],
        skip_types=[],
        conversation=False,
        since=None,
        until=None,
    )


def test_an_unhandled_event_type_now_renders_a_fallback_line(tmp_path, capsys) -> None:
    """Tier 2: #4383 — `tool_called` (the issue's own reproduction case,
    no dedicated ConsoleLogger.on_tool_called) prints at least one line on
    replay, not zero. RED before the fix: ConsoleLogger silently dropped
    it, and the old summary ("103 events replayed") gave no sign anything
    had been skipped."""
    from reyn.interfaces.cli.commands.events import run_replay

    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, [
        {"type": "tool_called", "data": {"tool": "shell", "call_id": "abc123"}},
    ])

    run_replay(_replay_args(events_file))
    out = capsys.readouterr().out

    assert "tool_called" in out, (
        "an event type with no dedicated ConsoleLogger handler must still "
        "produce a visible line — it must not be silently dropped"
    )


def test_matched_and_rendered_counts_are_reported_separately(tmp_path, capsys) -> None:
    """Tier 2: #4383 — the summary line distinguishes how many events
    MATCHED the filter from how many actually RENDERED a line, so a future
    silent-drop regression is visible as matched != rendered rather than
    reading as an ordinary success count (the owner's own misread:
    "21 events replayed" looked like 21 shown, not 21 found-and-discarded)."""
    from reyn.interfaces.cli.commands.events import run_replay

    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, [
        {"type": "tool_called", "data": {"tool": "shell"}},
        {"type": "tool_called", "data": {"tool": "file_read"}},
        {"type": "tool_called", "data": {"tool": "grep"}},
    ])

    run_replay(_replay_args(events_file))
    out = capsys.readouterr().out

    assert "3 matched" in out
    assert "3 rendered" in out, (
        "with the fallback renderer in place, every matched event should "
        "also render — this pins the fix, not just the wording"
    )


def test_a_handler_backed_event_type_still_renders_through_its_own_handler(tmp_path, capsys) -> None:
    """Tier 2: #4383 accept-side — an event type WITH a dedicated
    ConsoleLogger handler (shell_started, one of the 11) renders through
    its own specific formatting, not the generic fallback. The fallback
    must only catch the ~200 handler-less kinds, not shadow the 11 real
    renderers."""
    from reyn.interfaces.cli.commands.events import run_replay

    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, [
        {"type": "shell_started", "data": {"cmd": "ls -la", "timeout": 30}},
    ])

    run_replay(_replay_args(events_file))
    out = capsys.readouterr().out

    # The dedicated on_shell_started format ("[shell] <cmd> (timeout=...)"),
    # not the generic fallback's "[shell_started] cmd='ls -la' ..." shape.
    assert "[shell] ls -la" in out
    assert "(timeout=30s)" in out
    assert "[shell_started]" not in out, (
        "a handler-backed event type must render via ITS OWN handler, "
        "not fall through to the generic unhandled-type fallback"
    )


def test_the_fallback_line_bounds_the_number_of_fields_shown(tmp_path, capsys) -> None:
    """Tier 2: #4383 — the fallback renders a BOUNDED number of top-level
    fields, not a full dump of an arbitrarily wide payload. Without a
    bound, one event carrying a large embedded structure could turn a
    routine scan of thousands of events into an unreadable wall of text
    (the same class of concern this whole issue is about — a viewing tool
    that technically shows data but not usably)."""
    from reyn.interfaces.cli.commands.events import run_replay

    events_file = tmp_path / "events.jsonl"
    wide_data = {f"field_{i}": f"value_{i}" for i in range(20)}
    _write_events(events_file, [{"type": "tool_called", "data": wide_data}])

    run_replay(_replay_args(events_file))
    out = capsys.readouterr().out

    shown = sum(1 for i in range(20) if f"field_{i}=" in out)
    assert 0 < shown <= 6, (
        f"expected a bounded number of fields (1-6), got {shown} — the "
        "fallback must not dump an arbitrarily wide payload verbatim"
    )
