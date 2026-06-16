"""REST router — /api/runs.

Surfaces the event log stored under .reyn/events/. Treats all event
payloads as opaque JSON (P7): field names, event types, and artifact
values pass through without interpretation.

Layout on disk:
  .reyn/events/agents/<agent>/skill_runs/<YYYY-MM>/<ts>_<skill>.jsonl
  .reyn/events/agents/<agent>/chat/<YYYY-MM>/<ts>.jsonl
  .reyn/events/direct/skill_runs/...  (reyn run invocations)

Routes:
    GET /api/runs                         — list all skill run files
    GET /api/runs/{run_id}                — first event of a single run
    GET /api/runs/{run_id}/events         — full event list (JSON)
    GET /api/runs/{run_id}/events/stream  — SSE stream of events
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from reyn.interfaces.web.deps import get_project_root

router = APIRouter(tags=["runs"])

# ── run_id is embedded in the JSONL filename stem:
# e.g. "20260501T103200Z_skill_router"
_RUN_ID_RE = re.compile(r"^(\d{4}\d{2}\d{2}T\d{6}Z_[^.]+)")


# ── helpers ──────────────────────────────────────────────────────────────────


def _skill_runs_dirs(project_root: Path) -> list[Path]:
    """Yield all skill_runs sub-directories across agents + direct."""
    events_root = project_root / ".reyn" / "events"
    result: list[Path] = []
    if not events_root.is_dir():
        return result
    for caller_dir in sorted(events_root.iterdir()):
        if not caller_dir.is_dir():
            continue
        # agents/<name>/skill_runs  or  direct/skill_runs
        skill_runs = caller_dir / "skill_runs"
        if skill_runs.is_dir():
            result.append(skill_runs)
        # agents/<name>/<agent>/skill_runs (nested)
        for sub in caller_dir.iterdir():
            if sub.is_dir() and (sub / "skill_runs").is_dir():
                result.append(sub / "skill_runs")
    return result


def _run_id_from_file(path: Path) -> str | None:
    m = _RUN_ID_RE.match(path.stem)
    return m.group(1) if m else path.stem


def _iter_run_files(project_root: Path) -> list[tuple[str, Path]]:
    """Return (run_id, file_path) pairs for every skill-run JSONL, sorted by run_id."""
    result: list[tuple[str, Path]] = []
    for skill_runs_dir in _skill_runs_dirs(project_root):
        for month_dir in sorted(skill_runs_dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for f in sorted(month_dir.glob("*.jsonl")):
                run_id = _run_id_from_file(f)
                if run_id:
                    result.append((run_id, f))
    result.sort(key=lambda x: x[0])
    return result


def _find_run_file(run_id: str, project_root: Path) -> Path | None:
    for rid, path in _iter_run_files(project_root):
        if rid == run_id:
            return path
    return None


def _read_events(path: Path) -> list[dict]:
    events: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except OSError:
        pass
    return events


# ── response models ───────────────────────────────────────────────────────────


class RunSummary(BaseModel):
    run_id: str
    skill_name: str | None
    started_at: str | None
    # status, cost etc. could be added once we agree on a stable first-event schema.
    # For now surface only what the filename reliably tells us.


# ── routes ───────────────────────────────────────────────────────────────────


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(
    project_root: Path = Depends(get_project_root),
) -> list[RunSummary]:
    """List all skill run JSONL files found under .reyn/events/."""
    results: list[RunSummary] = []
    for run_id, _ in _iter_run_files(project_root):
        # run_id format: 20260501T103200Z_skill_name
        parts = run_id.split("_", 1)
        started_at_raw = parts[0] if parts else None
        skill_name = parts[1] if len(parts) > 1 else None
        # Parse ts into ISO-8601
        started_at: str | None = None
        if started_at_raw and len(started_at_raw) == 16:  # YYYYMMDDTHHMMSSz
            try:
                from datetime import datetime, timezone
                dt = datetime.strptime(started_at_raw, "%Y%m%dT%H%M%SZ")
                started_at = dt.replace(tzinfo=timezone.utc).isoformat()
            except Exception:
                started_at = started_at_raw
        results.append(RunSummary(
            run_id=run_id,
            skill_name=skill_name,
            started_at=started_at,
        ))
    return results


@router.get("/runs/{run_id}", response_model=dict)
async def get_run(
    run_id: str,
    project_root: Path = Depends(get_project_root),
) -> dict:
    """Return the first event of a run (opaque JSON pass-through)."""
    path = _find_run_file(run_id, project_root)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    events = _read_events(path)
    if not events:
        return {"run_id": run_id, "events": []}
    return {"run_id": run_id, "first_event": events[0], "event_count": len(events)}


@router.get("/runs/{run_id}/events", response_model=list[dict])
async def get_run_events(
    run_id: str,
    project_root: Path = Depends(get_project_root),
) -> list[dict]:
    """Return all events for a run as a JSON array (opaque pass-through)."""
    path = _find_run_file(run_id, project_root)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )
    return _read_events(path)


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(
    run_id: str,
    project_root: Path = Depends(get_project_root),
) -> StreamingResponse:
    """SSE stream of events for a run.

    For completed runs this streams all events immediately then closes.
    For in-progress runs (rare via HTTP — prefer WS) it streams what exists.
    Each SSE message is a single JSON object.
    """
    path = _find_run_file(run_id, project_root)
    if path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Run {run_id!r} not found.",
        )

    async def _sse_generator() -> AsyncIterator[str]:
        for event in _read_events(path):
            data = json.dumps(event, ensure_ascii=False)
            yield f"data: {data}\n\n"
        yield "data: {\"$sse\": \"done\"}\n\n"

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
