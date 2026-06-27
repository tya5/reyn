"""Pure helper for reading recently-completed skill runs from the event store.

Runtime-layer helper with no UI dependency: ``session.py`` reads an agent's
recent skill runs through this for the MCP ``:agents`` surface.
"""
from __future__ import annotations

import json
import logging
import time as _time
from datetime import date as _date
from pathlib import Path

logger = logging.getLogger(__name__)

# How many recent completed items to surface per agent. Bumped from 2
# to 5 after dogfood feedback ("直近 N 個のヒストリ対応してたっけ？").
# 5 still keeps the tab readable on a typical 24-row terminal even with
# 2-3 agents.
_RECENT_LIMIT = 5


def _recent_skill_runs_for_agent(
    project_root: Path | None,
    agent_name: str,
    running_run_ids: set[str],
    limit: int = _RECENT_LIMIT,
) -> list[dict]:
    """Return up to ``limit`` recently-completed skill runs for ``agent_name``.

    Each entry: ``skill_name``, ``run_id`` (8-char prefix), ``status``,
    ``duration_s``, ``ts`` (ISO string of completion).

    Source layout (as of 2026-05): ::

        .reyn/events/agents/<name>/skill_runs/<YYYY-MM>/<isots>_<skill>.jsonl

    The file name is ``<isots-no-tz>_<skill_name>.jsonl`` — there's no
    run_id in the filename, so we pull it out of the FIRST event in
    the file (``workflow_started.data.run_id``). The LAST event tells
    us the terminal type:

      * ``workflow_finished``  → status "ok"
      * ``workflow_aborted``   → status "aborted"
      * (anything else)        → fall back to the event type as a label

    ``rglob`` (not ``glob``) so we recurse into the YYYY-MM subdirs.
    """
    out: list[dict] = []
    if project_root is None:
        return out
    skill_dir = (
        project_root / ".reyn" / "events"
        / "agents" / agent_name / "skill_runs"
    )
    if not skill_dir.is_dir():
        return out

    # Collect candidate files newest-first by mtime. rglob to walk the
    # YYYY-MM subdirectories. Reading mtime up front avoids parsing
    # files we won't display.
    files: list[tuple[float, Path]] = []
    for jsonl in skill_dir.rglob("*.jsonl"):
        try:
            files.append((jsonl.stat().st_mtime, jsonl))
        except OSError:
            continue
    files.sort(reverse=True)

    for _mtime, jsonl in files:
        if len(out) >= limit:
            break
        # Filename: "<isots>_<skill_name>.jsonl". The skill name itself
        # may contain underscores (web_search_display, chat_compactor,
        # etc.), so split only ONCE — the head is the timestamp, the
        # tail is the entire skill name.
        stem = jsonl.stem
        if "_" not in stem:
            continue
        start_iso, skill_name = stem.split("_", 1)

        # Read the file once: keep the first event (for run_id) and the
        # last event (for completion timestamp + terminal type).
        first_event: dict | None = None
        last_event: dict | None = None
        try:
            for raw in jsonl.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except Exception:
                    continue
                if first_event is None:
                    first_event = ev
                last_event = ev
        except OSError as exc:
            logger.warning(
                "agent_skill_runs: read of %s failed: %s", jsonl, exc,
            )
            continue
        if first_event is None or last_event is None:
            continue

        # run_id lives in workflow_started.data.run_id; fall back to
        # last event if that's missing for any reason.
        run_id = ""
        for ev in (first_event, last_event):
            data = ev.get("data") or {}
            rid = data.get("run_id", "")
            if rid:
                run_id = str(rid)
                break
        if run_id and run_id in running_run_ids:
            continue

        ev_type = last_event.get("type", "")
        ts = str(last_event.get("timestamp", ""))
        # Terminal-type → status mapping. Includes legacy
        # `skill_run_completed` shape for forward-compat with future
        # event renames; new code emits workflow_finished/aborted.
        # When the LAST event is NOT a known terminal type, the run
        # never finished cleanly — typical causes are session crash,
        # SIGKILL, exception escaping the OS layer, or stdio fd
        # corruption mid-LLM-call. We mark these as "stuck" so the
        # display can distinguish them from genuine aborts.
        if ev_type in ("workflow_finished", "skill_run_completed"):
            status = "ok"
            stuck_at = ""
        elif ev_type in ("workflow_aborted", "skill_run_failed"):
            status = "aborted"
            stuck_at = ""
        else:
            status = "stuck"
            stuck_at = ev_type or "unknown"

        # Duration — both timestamps include timezone offsets in the
        # current event format (e.g. "2026-05-09T08:44:43.210059+09:00").
        # The filename's ts is ALSO local time but without a tz suffix,
        # so parse it as naive and pretend it matches the event tz.
        duration_s = 0.0
        if start_iso and ts:
            try:
                from datetime import datetime
                t0 = datetime.fromisoformat(start_iso)
                t1_str = ts
                # `datetime.fromisoformat` accepts the +HH:MM suffix
                # natively; drop fractional microseconds beyond 6 digits
                # if present (= some platforms emit nanoseconds).
                t1 = datetime.fromisoformat(t1_str)
                # Normalise to naive for the diff if mismatched.
                if t0.tzinfo is None and t1.tzinfo is not None:
                    t1 = t1.replace(tzinfo=None)
                duration_s = max(0.0, (t1 - t0).total_seconds())
            except Exception:
                duration_s = 0.0

        # ``run_id`` here is e.g. "20260508T234443Z_chat_compactor"; the
        # leading 8 chars ("20260508") are date-only and identical across
        # runs of the same skill on the same day, which makes the agents
        # tab unreadable. Use the time chunk (after the "T") so each
        # entry's badge is genuinely unique within a tab refresh.
        rid_compact = run_id
        if "T" in run_id:
            rid_compact = run_id.split("T", 1)[1][:6]
        out.append({
            "skill_name": skill_name or "?",
            "run_id": (rid_compact or stem)[:8],
            # Full run_id (= as it appears in workflow_started.data.run_id).
            # Needed by the orchestrator to look up triggered_by from the
            # session-local map keyed on the full id.
            "run_id_full": run_id or "",
            "status": status,
            # Last event type when status == "stuck"; lets the renderer
            # show "(stuck @ llm_called)" instead of just "(llm_called)".
            "stuck_at": stuck_at,
            "duration_s": duration_s,
            "ts": ts[:19].replace("T", " "),
            # Carry the absolute path so the preview pane can re-read
            # the jsonl on demand (= without holding all events in
            # memory across panel refreshes).
            "jsonl_path": jsonl,
        })
    return out


__all__ = ["_recent_skill_runs_for_agent"]
