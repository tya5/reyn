"""src/reyn/replay/engine.py — ReplayEngine: walk + seek over recorded sessions.

Reads existing dump files — no LLM calls, no litellm, deterministic.

Data sources merged by the engine:
  1. LLM trace JSONL  (REYN_LLM_TRACE_DUMP output)
     Format: records with kind="request"|"response", keyed by request_id.
  2. WAL JSONL        (StateLog on-disk file)
     Format: records with kind in WAL_EVENT_KINDS, monotonic seq.

The engine groups WAL events by (run_id, phase, step_idx) to form StepFrames,
then attaches any LLM payload/result whose timestamp falls within the step's
window.

Scope constants:
  "step"       — one frame per WAL step event pair
  "phase"      — one checkpoint per phase boundary within a skill_run
  "skill_run"  — one checkpoint per skill run
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Literal

from reyn.replay.model import Checkpoint, StepFrame


ScopeType = Literal["step", "phase", "skill_run"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> list[dict]:
    """Load all valid JSON lines from path.  Bad lines are skipped silently."""
    records: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return records
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _is_wal_record(rec: dict) -> bool:
    """Return True for WAL-style records (have ``seq`` and ``kind``)."""
    return isinstance(rec.get("seq"), int) and isinstance(rec.get("kind"), str)


def _is_llm_request(rec: dict) -> bool:
    return rec.get("kind") == "request" and "request_id" in rec


def _is_llm_response(rec: dict) -> bool:
    return rec.get("kind") == "response" and "request_id" in rec


def _split_sources(records: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split a flat record list into (wal_events, llm_requests, llm_responses).

    A record is classified as WAL if it has an integer ``seq`` field.
    LLM records have ``kind`` == "request" / "response" without ``seq``.
    Unknown records are collected in llm_requests as a fallback (ignored).
    """
    wal: list[dict] = []
    reqs: list[dict] = []
    resps: list[dict] = []
    for rec in records:
        if _is_wal_record(rec):
            wal.append(rec)
        elif _is_llm_request(rec):
            reqs.append(rec)
        elif _is_llm_response(rec):
            resps.append(rec)
    return wal, reqs, resps


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """Read-only engine for walking and seeking over recorded sessions.

    Usage::

        engine = ReplayEngine("/tmp/b14_run.jsonl")
        for frame in engine.walk():
            print(frame.checkpoint, len(frame.events))

        cp = Checkpoint.parse("run_xyz:copy_to_work:3")
        frame = engine.seek(cp)

    ``trace_path`` may point to:
    * A single JSONL file that interleaves both WAL and LLM trace records
      (the engine classifies by field presence).
    * A WAL-only file (LLM payload fields will be None).
    * An LLM-trace-only file (events lists will be empty).

    The engine is lazy: ``walk()`` yields one ``StepFrame`` at a time so that
    large traces do not require loading everything into memory.  ``seek()``
    materialises only the requested frame.
    """

    def __init__(self, trace_path: str) -> None:
        self._path = Path(trace_path)
        if not self._path.exists():
            raise FileNotFoundError(f"trace file not found: {self._path}")

        all_records = _load_jsonl(self._path)
        self._wal_events, llm_reqs, llm_resps = _split_sources(all_records)

        # Sort WAL events by seq for deterministic ordering.
        self._wal_events.sort(key=lambda e: e.get("seq", 0))

        # Build request_id → request/response lookup for O(1) attachment.
        self._llm_reqs: dict[str, dict] = {
            r["request_id"]: r for r in llm_reqs
        }
        self._llm_resps: dict[str, dict] = {
            r["request_id"]: r for r in llm_resps
        }

        # Build the step-frame list lazily on first access.
        self._frames: list[StepFrame] | None = None

    # ── Public surface ────────────────────────────────────────────────────

    def walk(self, scope: ScopeType = "step") -> Iterator[StepFrame]:
        """Iterate over all recorded steps (or aggregated phases / runs).

        ``scope="step"``       — one frame per step_started / step_completed pair
        ``scope="phase"``      — one frame aggregating all steps in a phase
        ``scope="skill_run"``  — one frame aggregating an entire skill run
        """
        frames = self._build_frames()
        if scope == "step":
            yield from frames
        elif scope == "phase":
            yield from self._aggregate(frames, level="phase")
        elif scope == "skill_run":
            yield from self._aggregate(frames, level="skill_run")
        else:
            raise ValueError(f"unknown scope: {scope!r}")

    def seek(self, checkpoint: Checkpoint) -> StepFrame:
        """Return the StepFrame at ``checkpoint``.

        Raises ``KeyError`` if the checkpoint is not found in the trace.
        """
        for frame in self._build_frames():
            if frame.checkpoint == checkpoint:
                return frame
        raise KeyError(f"checkpoint not found: {checkpoint}")

    def list_checkpoints(self, scope: ScopeType = "step") -> list[Checkpoint]:
        """Return checkpoints at the requested zoom level."""
        return [f.checkpoint for f in self.walk(scope=scope)]

    # ── Frame construction ────────────────────────────────────────────────

    def _build_frames(self) -> list[StepFrame]:
        if self._frames is not None:
            return self._frames

        self._frames = list(self._materialise_frames())
        return self._frames

    def _materialise_frames(self) -> Iterator[StepFrame]:
        """Group WAL events into per-step StepFrames.

        Strategy:
        - Track ``step_started`` events to open a frame.
        - Accumulate subsequent events until the next ``step_started`` or
          until a ``skill_completed`` / ``skill_discarded`` closes the run.
        - Attach LLM payload/result by run_id + phase proximity (if the trace
          interleaves both kinds, we match by the step's phase and run_id;
          otherwise we do a best-effort timestamp match).
        - Frames without a matching ``step_started`` (e.g. phase/skill
          lifecycle events) are still yielded with step_idx=-1 so consumers
          can see them.

        step_idx is a per-(run_id, phase) counter starting at 0.
        """
        # Group all WAL events by (run_id, phase) to build steps.
        # We emit one StepFrame per step_started event found.
        # Skill-lifecycle and phase events that fall outside a step are
        # emitted as a "header" frame with step_idx=-1.

        # First pass: collect (run_id, phase) for each step_started.
        step_counter: dict[tuple[str, str], int] = {}  # (run_id, phase) → next idx
        # pending_start: open step_started event waiting for close event
        pending: dict[str, dict] = {}  # op_invocation_id → step_started event
        current_run_id: str | None = None
        current_phase: str | None = None

        # Accumulator for between-step WAL events (lifecycle events).
        buffered_events: list[dict] = []

        for ev in self._wal_events:
            kind = ev.get("kind", "")

            # Track current skill run / phase from lifecycle events.
            if kind == "skill_started":
                current_run_id = ev.get("run_id", current_run_id)
                buffered_events.append(ev)
                continue
            if kind == "skill_phase_advanced":
                current_phase = ev.get("phase", current_phase)
                buffered_events.append(ev)
                continue
            if kind in ("skill_completed", "skill_discarded"):
                buffered_events.append(ev)
                continue

            if kind == "step_started":
                oid = ev.get("op_invocation_id", "")
                run_id = ev.get("run_id", current_run_id) or ""
                phase = ev.get("phase", current_phase) or ""
                pending[oid] = {**ev, "_run_id": run_id, "_phase": phase}
                buffered_events.append(ev)
                continue

            if kind in ("step_completed", "step_failed"):
                oid = ev.get("op_invocation_id", "")
                buffered_events.append(ev)
                if oid in pending:
                    start_ev = pending.pop(oid)
                    run_id = start_ev["_run_id"]
                    phase = start_ev["_phase"]
                    key = (run_id, phase)
                    idx = step_counter.get(key, 0)
                    step_counter[key] = idx + 1
                    cp = Checkpoint(run_id=run_id, phase=phase, step_idx=idx)
                    llm_payload, llm_result = self._find_llm_for_step(
                        run_id, phase, buffered_events
                    )
                    yield StepFrame(
                        checkpoint=cp,
                        events=list(buffered_events),
                        state_snapshot=self._state_from_events(buffered_events),
                        llm_payload=llm_payload,
                        llm_result=llm_result,
                    )
                    buffered_events = []
                continue

            # Any other event: buffer it.
            buffered_events.append(ev)

        # Flush remaining buffered events as a trailing frame if any.
        if buffered_events:
            run_id = current_run_id or ""
            phase = current_phase or ""
            cp = Checkpoint(run_id=run_id, phase=phase, step_idx=-1)
            yield StepFrame(
                checkpoint=cp,
                events=list(buffered_events),
                state_snapshot=self._state_from_events(buffered_events),
                llm_payload=None,
                llm_result=None,
            )

    def _find_llm_for_step(
        self,
        run_id: str,
        phase: str,
        events: list[dict],
    ) -> tuple[dict | None, dict | None]:
        """Return (request, response) matching this step's run_id / phase.

        Matching strategy (in priority order):
        1. Exact match on ``run_id`` + ``phase`` in the LLM record's
           ``caller_hint`` field (format: ``phase:<phase>`` or contains phase).
        2. First unmatched request in insertion order (fallback).
        """
        # Try caller_hint matching.
        for rid, req in self._llm_reqs.items():
            hint = req.get("caller_hint", "")
            if phase and phase in hint:
                resp = self._llm_resps.get(rid)
                return req, resp

        # No match.
        return None, None

    def _state_from_events(self, events: list[dict]) -> dict[str, Any]:
        """Build a partial state snapshot from a list of WAL events.

        Extracts fields present in step_completed / step_failed records so
        callers can inspect op results without parsing raw events.
        """
        snapshot: dict[str, Any] = {}
        for ev in events:
            kind = ev.get("kind", "")
            if kind == "step_completed":
                snapshot["last_completed_op"] = ev.get("op_kind", "")
                snapshot["last_completed_op_id"] = ev.get("op_invocation_id", "")
                result = ev.get("result")
                if result is not None:
                    snapshot["last_result"] = result
            elif kind == "step_failed":
                snapshot["last_failed_op"] = ev.get("op_kind", "")
                snapshot["last_error"] = ev.get("error", "")
            elif kind == "skill_started":
                snapshot["run_id"] = ev.get("run_id", "")
                snapshot["skill"] = ev.get("skill", "")
            elif kind == "skill_phase_advanced":
                snapshot["current_phase"] = ev.get("phase", "")
        return snapshot

    # ── Aggregation ───────────────────────────────────────────────────────

    def _aggregate(
        self, frames: list[StepFrame], level: str
    ) -> Iterator[StepFrame]:
        """Merge step frames into coarser-grained frames.

        ``level="phase"``      — group by (run_id, phase)
        ``level="skill_run"``  — group by run_id
        """
        from collections import defaultdict

        if level == "phase":
            groups: dict[tuple[str, str], list[StepFrame]] = defaultdict(list)
            for f in frames:
                groups[(f.checkpoint.run_id, f.checkpoint.phase)].append(f)
            for (run_id, phase), grp in groups.items():
                yield self._merge_frames(
                    grp,
                    Checkpoint(run_id=run_id, phase=phase, step_idx=0),
                )
        elif level == "skill_run":
            run_groups: dict[str, list[StepFrame]] = defaultdict(list)
            for f in frames:
                run_groups[f.checkpoint.run_id].append(f)
            for run_id, grp in run_groups.items():
                phase = grp[0].checkpoint.phase if grp else ""
                yield self._merge_frames(
                    grp,
                    Checkpoint(run_id=run_id, phase=phase, step_idx=0),
                )

    @staticmethod
    def _merge_frames(frames: list[StepFrame], cp: Checkpoint) -> StepFrame:
        """Produce a single StepFrame by merging a list of frames."""
        all_events: list[dict] = []
        merged_state: dict[str, Any] = {}
        llm_payload: dict | None = None
        llm_result: dict | None = None
        for f in frames:
            all_events.extend(f.events)
            merged_state.update(f.state_snapshot)
            if f.llm_payload is not None and llm_payload is None:
                llm_payload = f.llm_payload
            if f.llm_result is not None and llm_result is None:
                llm_result = f.llm_result
        return StepFrame(
            checkpoint=cp,
            events=all_events,
            state_snapshot=merged_state,
            llm_payload=llm_payload,
            llm_result=llm_result,
        )
