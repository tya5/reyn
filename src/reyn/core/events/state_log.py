"""StateLog — append-only WAL for crash recovery (PR21).

Records state-change events with monotonically increasing `seq`, fsync'd
per append for durability. On restart, each AgentSnapshot's `applied_seq`
identifies the last event already absorbed — replay starts from
`min(applied_seq) + 1` across all known agents.

Event kinds recorded (state-mutating only; processing internals
like LLM calls live in the audit log under `.reyn/events/`):

  inbox_put           — message put on agent X's inbox
  inbox_consume       — message removed from agent X's inbox
  chain_register      — new pending_chain created on agent X
  chain_update        — pending_chain's waiting_on shrunk
  chain_resolve       — pending_chain completed
  chain_timeout_fired — PR18 watchdog fired
  skill_started       — skill execution begun; creates per-skill snapshot
  skill_phase_advanced — skill transitioned to next phase
  step_started        — a step within a phase has begun
  step_completed      — a step completed successfully (result stored)
  step_failed         — a step failed (error stored)
  intervention_dispatched — ask_user / permission request emitted
  intervention_resolved   — intervention answered
  skill_resumed       — audit marker; no agent-level state mutation
  skill_completed     — skill execution finished; deletes per-skill snapshot

Per P7: this is OS-level generic infrastructure — `kind` strings and
field names live here, not in any skill/domain code.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

WAL_EVENT_KINDS = (
    # Existing PR21 — inbox and chain lifecycle
    "inbox_put",
    "inbox_consume",
    "chain_register",
    "chain_update",
    "chain_resolve",
    "chain_timeout_fired",
    # NEW (skill resume design — PR-state-foundation)
    "skill_started",
    "skill_phase_advanced",
    "step_started",
    "step_completed",
    "step_failed",
    "intervention_dispatched",
    "intervention_resolved",
    "skill_resumed",
    "skill_completed",
    # NEW (PR-resume-ux β U1) — skill discarded by user via prompt or /skill discard
    "skill_discarded",
    # NEW (R-D12) — durable buffered intervention answer that survives a
    # second crash before the resuming skill consumes it
    "intervention_answer_buffered",
    "intervention_answer_consumed",
    # NEW (ADR-0022) — plan-mode lifecycle (Phase 1: fail-safe + observability)
    "plan_started",
    "plan_completed",
    "plan_aborted",
    # NEW (ADR-0023 Phase 2 step 4) — per-step events promoted from
    # events-log into WAL for resume determinism. The analyzer pairs
    # (plan_step_started, plan_step_completed | plan_step_failed) by
    # (plan_id, step_id) to derive each step's recovery state. Forensic-
    # only events (plan_emitted / plan_aggregated / plan_run_interrupted)
    # remain in the events log — they don't drive resume decisions.
    "plan_step_started",
    "plan_step_completed",
    "plan_step_failed",
    # NEW (ADR-0038 Stage 1b) — user-facing time-travel rewind reset-record.
    # Append-only compensating record: moves the active pointer to ``target_n``;
    # the abandoned future is retained as an inactive branch (reconstruct honors
    # the active path). A non-state marker — apply_events no-ops it.
    "rewind",
)


class StateLog:
    """Single-file WAL. Process-shared; ownership lives in AgentRegistry."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._counter = self._scan_max_seq()
        # Generic post-append observers (#1560). Each is invoked AFTER a durable
        # append (post-fsync, outside the lock) with `(kind, seq, fields)`. The
        # WAL stays workspace/feature-agnostic (P7) — observers carry all domain
        # knowledge. Empty by default → zero cost on the append path; a registrant
        # (e.g. the registry's act-turn workspace capture) opts in explicitly.
        self._post_append_cbs: list = []

    @property
    def path(self) -> Path:
        return self._path

    @property
    def current_seq(self) -> int:
        return self._counter

    async def append(self, kind: str, **fields) -> int:
        """Append a new entry, fsync, return its seq.

        `kind` must be one of WAL_EVENT_KINDS — caught at write time so a
        typo doesn't silently fragment the recovery vocabulary.
        """
        if kind not in WAL_EVENT_KINDS:
            raise ValueError(f"unknown WAL event kind: {kind!r}")
        async with self._lock:
            self._counter += 1
            seq = self._counter
            entry = {
                "seq": seq,
                "ts": datetime.now().isoformat(),
                "kind": kind,
                **fields,
            }
            payload = json.dumps(entry, ensure_ascii=False) + "\n"
            # Defensive: if the previous run crashed mid-write, the file may
            # end without a newline. Probe the last byte before appending so
            # our new entry starts on its own line — the torn fragment still
            # parses as garbage and gets skipped on replay.
            need_lead_newline = self._needs_lead_newline()
            with self._path.open("a", encoding="utf-8") as f:
                if need_lead_newline:
                    f.write("\n")
                f.write(payload)
                f.flush()
                # fsync synchronously so the append is durable before it returns
                # (the fsync-per-append recovery contract). Kept ON the event loop
                # deliberately: a synchronous fsync does not yield, so each append
                # is event-loop-atomic — there is no intra-append suspension point
                # at which another coroutine could interleave. (#1751 briefly moved
                # this off-loop via ``asyncio.to_thread`` to unblock TUI repaint,
                # but that opened a new intra-append concurrency surface; reverted —
                # the TUI latency is addressed in the UI layer instead.)
                os.fsync(f.fileno())
        # Post-append observers fire AFTER the durable write and OUTSIDE the lock
        # (#1560): durability is already secured, so a slow/failing observer
        # neither weakens crash-recovery nor serializes other appends. Best-effort
        # — failures are swallowed, so the append's result is never blocked.
        await self._fire_post_append(kind, seq, fields)
        return seq

    def register_post_append(self, cb) -> None:
        """Register a generic post-append observer ``cb(kind, seq, fields)``.

        Invoked after each durable append (post-fsync, outside the lock). The WAL
        passes only WAL vocabulary — observers own all domain knowledge (P7). Used
        by the registry's opt-in act-turn workspace capture (#1560); unregistered
        by default so the append path stays zero-cost.
        """
        self._post_append_cbs.append(cb)

    async def _fire_post_append(self, kind: str, seq: int, fields: dict) -> None:
        """Invoke each post-append observer, swallowing failures (best-effort).

        A raising / hanging-then-failing observer must never fail or corrupt the
        append — the WAL entry is already durable. Each callback is isolated.
        """
        for cb in self._post_append_cbs:
            try:
                await cb(kind, seq, fields)
            except Exception as e:  # noqa: BLE001 — never let an observer fail the append
                import logging
                logging.getLogger(__name__).warning(
                    "StateLog post-append observer failed (kind=%s seq=%s): %s",
                    kind, seq, e,
                )

    def _needs_lead_newline(self) -> bool:
        if not self._path.is_file():
            return False
        try:
            size = self._path.stat().st_size
        except OSError:
            return False
        if size == 0:
            return False
        try:
            with self._path.open("rb") as f:
                f.seek(-1, 2)
                return f.read(1) != b"\n"
        except OSError:
            return False

    def iter_from(self, min_seq: int) -> Iterator[dict]:
        """Yield WAL entries with `seq >= min_seq`, in file order.

        Bad lines (parse errors, partial writes from a crash) are skipped
        silently — recovery should be best-effort from whatever survived.
        """
        if not self._path.is_file():
            return
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                seq = entry.get("seq")
                if not isinstance(seq, int):
                    continue
                if seq < min_seq:
                    continue
                yield entry

    async def truncate_below(self, min_keep_seq: int) -> dict:
        """Atomically rewrite the WAL keeping only entries with ``seq >= min_keep_seq``.

        Drop policy: ``seq < min_keep_seq`` entries are dropped. Caller computes
        ``min_keep_seq`` as ``min(全 agent applied_seq, 全 active skill
        last_phase_applied_seq) + 1`` (i.e. drop everything strictly below the
        last universally-absorbed seq).

        Atomic strategy (mirrors per-snapshot atomic save):
          1. Stream-read current ``wal.jsonl``, write surviving entries to
             ``wal.jsonl.tmp``.
          2. ``fsync(tmp)`` then ``rename(tmp, wal.jsonl)``.
          3. A mid-rewrite crash leaves the original ``wal.jsonl`` intact
             — incomplete ``.tmp`` is ignored at next startup.

        Returns a stats dict ``{"dropped": int, "kept": int, "min_kept_seq":
        int|None, "max_kept_seq": int|None}``. ``min_keep_seq <= 1`` is a
        no-op (everything would be kept).

        Crash safety: holds ``self._lock`` for the duration so concurrent
        ``append`` calls are serialized — the rename is safe even on
        platforms where ``rename`` over an open handle would fail (we don't
        keep the destination open).
        """
        if min_keep_seq <= 1:
            return {"dropped": 0, "kept": 0,
                    "min_kept_seq": None, "max_kept_seq": None}
        async with self._lock:
            if not self._path.is_file():
                return {"dropped": 0, "kept": 0,
                        "min_kept_seq": None, "max_kept_seq": None}
            # Two-pass: first pass identifies the highest seq actually present
            # so we never drop *all* entries (next-startup ``_scan_max_seq``
            # would reset the counter and re-issue already-used seqs into the
            # audit log). Second pass writes survivors + the watermark entry.
            highest_seq: int | None = None
            with self._path.open("r", encoding="utf-8") as src:
                for line in src:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    seq = entry.get("seq")
                    if not isinstance(seq, int):
                        continue
                    if highest_seq is None or seq > highest_seq:
                        highest_seq = seq
            # Effective floor: never drop the highest seq present, even if
            # caller asked us to. This preserves the counter watermark.
            effective_floor = min_keep_seq
            if highest_seq is not None and effective_floor > highest_seq:
                effective_floor = highest_seq

            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            dropped = 0
            kept = 0
            min_kept: int | None = None
            max_kept: int | None = None
            with self._path.open("r", encoding="utf-8") as src, \
                    tmp.open("w", encoding="utf-8") as dst:
                for line in src:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        entry = json.loads(raw)
                    except json.JSONDecodeError:
                        # Torn fragment from prior crash — drop on rewrite.
                        dropped += 1
                        continue
                    if not isinstance(entry, dict):
                        dropped += 1
                        continue
                    seq = entry.get("seq")
                    if not isinstance(seq, int):
                        dropped += 1
                        continue
                    if seq < effective_floor:
                        dropped += 1
                        continue
                    dst.write(raw + "\n")
                    kept += 1
                    if min_kept is None or seq < min_kept:
                        min_kept = seq
                    if max_kept is None or seq > max_kept:
                        max_kept = seq
                dst.flush()
                os.fsync(dst.fileno())
            tmp.replace(self._path)
            # Counter (next-seq source) must remain ≥ max seq ever issued;
            # truncation does NOT reset it (would re-issue dropped seqs).
            return {"dropped": dropped, "kept": kept,
                    "min_kept_seq": min_kept, "max_kept_seq": max_kept}

    def _scan_max_seq(self) -> int:
        """Initialize the counter from the highest seq present in the WAL.

        Read the whole file once at construction. Cheap for typical sizes
        (millions of small lines = ~hundreds of MB), which we're nowhere
        near. WAL truncation is OOS for PR21.
        """
        if not self._path.is_file():
            return 0
        max_seq = 0
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(entry, dict):
                        seq = entry.get("seq")
                        if isinstance(seq, int) and seq > max_seq:
                            max_seq = seq
        except OSError:
            return 0
        return max_seq
