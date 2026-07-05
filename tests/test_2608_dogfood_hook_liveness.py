"""Tier 2: #2608 observability — `dogfood_trace.py --mode hook-liveness`.

The investigation-confirmed gap: dogfood_trace's existing aggregate modes
read ONLY the EventLog (`.reyn/events/**/*.jsonl`) — never the WAL
(`.reyn/state/wal.jsonl`) — so a hook push that landed in a session's inbox
(WAL `inbox_put(msg_kind="hook")`) but was never drained by the run-loop (no
subsequent `turn_started(kind="hook")`) left NO trace the tooling could read.
`--mode hook-liveness` pairs the two artifacts and flags exactly that failure
signature (INERT).

Policy (docs/deep-dives/contributing/testing.md): fixtures are REAL artifacts
written by the real producer components (``StateLog`` for the WAL,
``EventLog`` + ``EventStore`` for the EventLog) — not hand-authored JSON
guessing at the on-disk shape, so a producer-format drift would break this
test rather than silently going unnoticed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog

SCRIPT = Path(__file__).parent.parent / "scripts" / "dogfood_trace.py"


def _run(args: list[str]) -> tuple[str, int]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True,
    )
    return result.stdout + result.stderr, result.returncode


def _event_log(reyn_dir: Path) -> EventLog:
    store = EventStore(reyn_dir / "events" / "agents" / "default" / "chat")
    return EventLog(subscribers=[store])


@pytest.mark.asyncio
async def test_hook_push_that_ran_is_not_flagged_inert(tmp_path: Path):
    """Tier 2: a WAL inbox_put(kind=hook) followed by a real turn_started(kind=hook)
    is reported as ran — the healthy pairing, not flagged INERT."""
    reyn_dir = tmp_path / ".reyn"
    wal = StateLog(reyn_dir / "state" / "wal.jsonl")
    log = _event_log(reyn_dir)

    await wal.append(
        "inbox_put", target="test-agent", session_id="main",
        msg_id="m1", msg_kind="hook",
        payload={"name": "turn_end", "text": "continue", "wake": True, "_msg_id": "m1"},
    )
    # The run-loop actually picks the push up and runs a turn (real EventLog.emit,
    # the same call site as Session._handle_inbox_message's turn_started emit).
    log.emit("turn_started", kind="hook", chain_id=None)

    out, rc = _run(["--root", str(reyn_dir), "--mode", "hook-liveness"])
    assert rc == 0
    assert "1 hook push(es): 1 ran, 0 INERT" in out


@pytest.mark.asyncio
async def test_inert_hook_push_is_flagged(tmp_path: Path):
    """Tier 2: a WAL inbox_put(kind=hook) with NO subsequent turn_started(kind=hook)
    — the inert-hook failure signature — is flagged INERT. This is the exact bug
    class the investigation found invisible to the packaged trace tooling."""
    reyn_dir = tmp_path / ".reyn"
    wal = StateLog(reyn_dir / "state" / "wal.jsonl")
    # An unrelated user-turn event exists (proves the mode doesn't just count
    # "any turn_started" — it specifically requires kind="hook").
    log = _event_log(reyn_dir)
    log.emit("turn_started", kind="user", chain_id=None)

    await wal.append(
        "inbox_put", target="test-agent", session_id="main",
        msg_id="m2", msg_kind="hook",
        payload={"name": "cron_fired", "text": "tick", "wake": True, "_msg_id": "m2"},
    )
    # No turn_started(kind=hook) ever follows — the push sat in the inbox forever.

    out, rc = _run(["--root", str(reyn_dir), "--mode", "hook-liveness"])
    assert rc == 0
    assert "INERT" in out
    assert "1 hook push(es): 0 ran, 1 INERT" in out


def test_no_wal_and_no_events_reports_clean_no_hooks(tmp_path: Path):
    """Tier 2: an empty .reyn/ (no WAL, no events) is a clean 'no hook pushes
    found' — robust to a missing WAL, never a crash/traceback."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()

    out, rc = _run(["--root", str(reyn_dir), "--mode", "hook-liveness"])
    assert rc == 0
    assert "no hook pushes found" in out
    assert "Traceback" not in out


@pytest.mark.asyncio
async def test_mixed_healthy_and_inert_pushes_both_counted(tmp_path: Path):
    """Tier 2: two hook pushes, one that ran and one that didn't — both are
    reported, and the summary counts split correctly (not just a boolean)."""
    reyn_dir = tmp_path / ".reyn"
    wal = StateLog(reyn_dir / "state" / "wal.jsonl")
    log = _event_log(reyn_dir)

    await wal.append(
        "inbox_put", target="test-agent", session_id="main",
        msg_id="ok1", msg_kind="hook",
        payload={"name": "turn_end", "text": "a", "wake": True, "_msg_id": "ok1"},
    )
    log.emit("turn_started", kind="hook", chain_id=None)  # pairs with ok1

    await wal.append(
        "inbox_put", target="test-agent", session_id="main",
        msg_id="inert1", msg_kind="hook",
        payload={"name": "file_changed", "text": "b", "wake": True, "_msg_id": "inert1"},
    )
    # no turn_started follows for inert1

    out, rc = _run(["--root", str(reyn_dir), "--mode", "hook-liveness"])
    assert rc == 0
    assert "2 hook push(es): 1 ran, 1 INERT" in out
