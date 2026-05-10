"""Events log reader helpers for the G4 spike driver."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _read_jsonl_dir_since(base_dir: Path, since_ts: float) -> list[dict]:
    """Read all JSONL events from a month-partitioned directory at or after since_ts."""
    events: list[dict] = []
    if not base_dir.exists():
        return events
    for month_dir in sorted(base_dir.iterdir()):
        if not month_dir.is_dir():
            continue
        for f in sorted(month_dir.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = ev.get("timestamp", "") or ev.get("ts", "")
                try:
                    ev_ts = datetime.fromisoformat(ts_str).timestamp()
                except (ValueError, TypeError):
                    ev_ts = 0.0
                if ev_ts >= since_ts:
                    events.append(ev)
    return events


def read_events_since(events_dir: Path, agent: str, since_ts: float) -> list[dict]:
    """Read all events for the agent written at or after since_ts (unix epoch).

    Reads from both ``agents/<agent>/chat/`` (session-level events such as
    ``skill_run_spawned`` / ``skill_run_completed``) and
    ``agents/<agent>/skill_runs/`` (per-skill-run events including
    ``llm_called`` and ``llm_response_received``).  Both are needed to
    accurately count LLM calls and extract the final structured output.
    """
    agent_base = events_dir / "agents" / agent
    return (
        _read_jsonl_dir_since(agent_base / "chat", since_ts)
        + _read_jsonl_dir_since(agent_base / "skill_runs", since_ts)
    )


def count_llm_calls(events: list[dict]) -> int:
    """Count LLM call events (Reyn uses 'llm_called' as the event type)."""
    return sum(
        1 for ev in events
        if ev.get("type") in ("llm_called", "llm_call_started")
    )


def count_flash_requests(events: list[dict]) -> int:
    """Count calls to gemini-2.5-flash (not flash-lite) for RPD tracking."""
    count = 0
    for ev in events:
        if ev.get("type") not in ("llm_called", "llm_call_started"):
            continue
        data = ev.get("data") or {}
        model = str(data.get("model", "")).lower()
        if "gemini-2.5-flash" in model and "flash-lite" not in model:
            count += 1
    return count


def extract_final_output(events: list[dict]) -> dict:
    """Return the skill run's final_output dict from the captured event list.

    Looks for the last ``llm_response_received`` event that carries a
    ``finish`` control decision — that event's ``raw.artifact.data`` is the
    structured final output the skill produced.

    Returns {} if no such event is found (e.g., the run aborted, was
    cap-exceeded, or the events list is empty). Callers should treat an
    empty dict as "no structured output available".
    """
    for ev in reversed(events):
        if ev.get("type") != "llm_response_received":
            continue
        raw = ev.get("data", {}).get("raw", {})
        control_type = raw.get("control", {}).get("type", "")
        if control_type != "finish":
            continue
        artifact_data = raw.get("artifact", {}).get("data", {})
        if isinstance(artifact_data, dict):
            return artifact_data
    return {}


def save_run_events(out_dir: Path, run_id: str, events: list[dict]) -> str:
    """Write events to spike_results/fp_0011/events/<safe_run_id>.jsonl."""
    safe_id = run_id.replace("/", "__")
    events_dir = out_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    path = events_dir / f"{safe_id}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for ev in events:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    return str(path)
