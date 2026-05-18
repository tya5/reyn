"""Tier 2: dogfood_long_session driver wait-for-skill-completion semantics
(B41-NF-W7-2 fix).

Pinned invariants:

- ``_is_spawn_ack`` detects the en + ja router spawn-ack reply forms via
  substring match (= robust to minor OS wording tweaks).
- ``_wait_for_skill_completion`` polls a real on-disk events JSONL file for
  a ``skill_completion_injected`` event newer than the supplied ``since_ts``,
  returning True on first match or False after the deadline.
- ``_read_latest_assistant_text`` returns the content of the most recent
  ``role=assistant`` history.jsonl record (= the router narration emitted
  after the spawned skill completes), or None when no such record exists.

Reference: B41 W7-S5 patch-isolation evidence (agent 3 timing analysis) +
B41-NF-W7-2 carry-over finding.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# scripts/ is not on sys.path during normal test discovery; add it lazily so
# the driver module is importable without restructuring the dogfood scripts.
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from dogfood_long_session import (  # noqa: E402
    _is_spawn_ack,
    _iso_to_unix,
    _read_latest_assistant_text,
    _wait_for_skill_completion,
)

# ---------------------------------------------------------------------------
# _is_spawn_ack
# ---------------------------------------------------------------------------


def test_is_spawn_ack_detects_english_form():
    """Tier 2: English spawn-ack form is detected by substring."""
    msg = "Skill is running in the background. Use `/tasks` to monitor progress."
    assert _is_spawn_ack(msg) is True


def test_is_spawn_ack_detects_japanese_form():
    """Tier 2: Japanese spawn-ack form is detected by substring."""
    msg = "スキルをバックグラウンドで実行しています。 `/tasks` で進行状況を確認できます。"
    assert _is_spawn_ack(msg) is True


def test_is_spawn_ack_rejects_unrelated_reply():
    """Tier 2: ordinary assistant reply is not flagged as spawn-ack."""
    assert _is_spawn_ack("The answer is 42.") is False


def test_is_spawn_ack_rejects_empty():
    """Tier 2: empty / falsy reply is not flagged."""
    assert _is_spawn_ack("") is False
    assert _is_spawn_ack(None) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _wait_for_skill_completion
# ---------------------------------------------------------------------------


def _write_event(path: Path, event_type: str, ts_iso: str) -> None:
    """Append a minimal JSONL event record."""
    record = {"type": event_type, "timestamp": ts_iso, "data": {}}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def test_wait_for_skill_completion_returns_true_when_event_present(tmp_path: Path):
    """Tier 2: completion event newer than since_ts → return True immediately."""
    events_file = tmp_path / "events.jsonl"
    # Event timestamp is 1s in the past relative to now; since_ts is 60s ago.
    now = time.time()
    since_ts = now - 60.0
    event_ts_unix = now - 1.0
    import datetime as dt
    event_ts_iso = dt.datetime.fromtimestamp(event_ts_unix, tz=dt.timezone.utc).isoformat()
    _write_event(events_file, "skill_completion_injected", event_ts_iso)

    result = _wait_for_skill_completion(
        events_file, since_ts=since_ts, deadline_s=2.0, poll_interval_s=0.1
    )
    assert result is True


def test_wait_for_skill_completion_returns_false_on_deadline(tmp_path: Path):
    """Tier 2: no completion event before deadline → return False."""
    events_file = tmp_path / "events.jsonl"
    events_file.touch()
    result = _wait_for_skill_completion(
        events_file, since_ts=time.time(), deadline_s=0.3, poll_interval_s=0.05
    )
    assert result is False


def test_wait_for_skill_completion_ignores_older_events(tmp_path: Path):
    """Tier 2: completion event predating since_ts is not counted."""
    events_file = tmp_path / "events.jsonl"
    now = time.time()
    since_ts = now  # now
    import datetime as dt
    older_ts_iso = dt.datetime.fromtimestamp(now - 30.0, tz=dt.timezone.utc).isoformat()
    _write_event(events_file, "skill_completion_injected", older_ts_iso)

    result = _wait_for_skill_completion(
        events_file, since_ts=since_ts, deadline_s=0.3, poll_interval_s=0.05
    )
    assert result is False


def test_wait_for_skill_completion_ignores_other_event_types(tmp_path: Path):
    """Tier 2: only ``skill_completion_injected`` triggers the wait release."""
    events_file = tmp_path / "events.jsonl"
    now = time.time()
    since_ts = now - 60.0
    import datetime as dt
    ev_ts_iso = dt.datetime.fromtimestamp(now - 1.0, tz=dt.timezone.utc).isoformat()
    # Different event types should not satisfy the wait.
    _write_event(events_file, "skill_run_spawned", ev_ts_iso)
    _write_event(events_file, "routing_decided", ev_ts_iso)

    result = _wait_for_skill_completion(
        events_file, since_ts=since_ts, deadline_s=0.3, poll_interval_s=0.05
    )
    assert result is False


def test_wait_for_skill_completion_missing_file_returns_false(tmp_path: Path):
    """Tier 2: events file absence at deadline → return False (= no error raised)."""
    events_file = tmp_path / "does_not_exist.jsonl"
    result = _wait_for_skill_completion(
        events_file, since_ts=time.time(), deadline_s=0.1, poll_interval_s=0.05
    )
    assert result is False


# ---------------------------------------------------------------------------
# _read_latest_assistant_text
# ---------------------------------------------------------------------------


def test_read_latest_assistant_text_returns_last_assistant_entry(tmp_path: Path):
    """Tier 2: returns content of the latest ``role=assistant`` history line."""
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join([
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "older assistant text"}),
            json.dumps({"role": "user", "content": "follow-up"}),
            json.dumps({"role": "assistant", "content": "the latest narration"}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert _read_latest_assistant_text(history) == "the latest narration"


def test_read_latest_assistant_text_missing_file_returns_none(tmp_path: Path):
    """Tier 2: missing history file → None (no exception)."""
    assert _read_latest_assistant_text(tmp_path / "absent.jsonl") is None


def test_read_latest_assistant_text_no_assistant_returns_none(tmp_path: Path):
    """Tier 2: history with only user entries → None."""
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps({"role": "user", "content": "only user msg"}) + "\n",
        encoding="utf-8",
    )
    assert _read_latest_assistant_text(history) is None


def test_read_latest_assistant_text_skips_blank_content(tmp_path: Path):
    """Tier 2: blank-only assistant entries are skipped (= prior non-blank wins)."""
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join([
            json.dumps({"role": "assistant", "content": "real narration"}),
            json.dumps({"role": "assistant", "content": "   "}),
            json.dumps({"role": "assistant", "content": ""}),
        ]) + "\n",
        encoding="utf-8",
    )
    assert _read_latest_assistant_text(history) == "real narration"


# ---------------------------------------------------------------------------
# _iso_to_unix (= small but kept-honest helper)
# ---------------------------------------------------------------------------


def test_iso_to_unix_handles_timezone_offset():
    """Tier 2: ISO-8601 with explicit offset round-trips to unix seconds."""
    # 2026-05-18T22:25:53.453696+09:00 == 2026-05-18T13:25:53.453696Z
    ts = _iso_to_unix("2026-05-18T22:25:53.453696+09:00")
    # Re-construct same ISO and compare timestamps within 1 ms
    import datetime as dt
    expected = dt.datetime(2026, 5, 18, 13, 25, 53, 453696, tzinfo=dt.timezone.utc).timestamp()
    assert abs(ts - expected) < 1e-3


def test_iso_to_unix_rejects_unparseable():
    """Tier 2: malformed ISO raises ValueError (= caller is expected to skip)."""
    with pytest.raises(ValueError):
        _iso_to_unix("not-a-timestamp")
