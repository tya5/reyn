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

Off-loop durability (#1765 Step 1a). The fsync runs OFF the event loop via the shared
`DurabilityWorker` (so a slow disk no longer freezes the loop — TUI repaint + other
sessions keep running DURING the fsync), yet `append` still returns only once durable: the
per-append crash-recovery contract is UNCHANGED (no relaxed-durability window — that is the
deferred Step 2). The off-loop fsync would reopen the #1751 surface (a lockless `iter_from`
reading a written-but-not-yet-fsync'd entry) — closed structurally by `_inflight_seq`: the
worker marks the seq it is fsyncing right now, and `iter_from` skips exactly that one entry
(a single-entry exclusion, NOT a durable ceiling, so cross-instance reads of a process-shared
WAL + recovery still see every durable entry). The seq is assigned + the worker write
submitted under the lock, so seq order = file order, and `truncate_below` (also under the
lock) never overlaps a write.
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
    # NEW (#1800 slice 4b) — next-turn-context staging for wake=false ride-alongs.
    # Staged (persisted) before the trigger turn; cleared durably after injection.
    "next_turn_context_staged",
    "next_turn_context_cleared",
    # NEW (ADR-0038 Stage 1b) — user-facing time-travel rewind reset-record.
    # Append-only compensating record: moves the active pointer to ``target_n``;
    # the abandoned future is retained as an inactive branch (reconstruct honors
    # the active path). A non-state marker — apply_events no-ops it.
    "rewind",
    # NEW (#2103 S2) — agent-lifecycle events for rewind-reconstruction. The
    # as-of-cut DROP primitive (#2114) + S2 reconstruct EXISTENCE from these:
    # created → re-materialise (≤cut) or drop (>cut); archived → hide-as-of-cut;
    # purged → permanent (out-of-time-travel, fork A — never re-materialised).
    # apply_events no-ops them (existence-affecting, not snapshot STATE).
    "agent_created",
    "agent_archived",
    "agent_purged",
    # NEW (#2103 S1bc) — LLM session-spawn lifecycle. ``session_spawned`` is a #2103
    # CREATE-event (unioned into the registry's _LIFECYCLE_CREATE_KINDS): it carries
    # {entity_kind:"session", name, sid, mode, narrowing} and its own seq = the
    # create-seq the as-of-cut DROP primitive keys off (a session spawned after a
    # rewind cut is dropped, not left an empty orphan); mode+narrowing make it
    # config-complete (uniform with agent_created, re-materialisable). ``session_
    # vanished`` records the ephemeral auto-vanish / explicit teardown. OS-level (P7).
    "session_spawned",
    "session_vanished",
    # NEW (#2103 Piece-2) — topology-lifecycle events for rewind-reconstruction of
    # the topology config-set (create / update / remove). OS-level (topology is an
    # OS concept), P7-safe; apply_events no-ops them (config-set existence/shape,
    # not agent-snapshot STATE). Each create/update carries the FULL topology config
    # → as-of-cut reconstruction is latest-≤-cut-wins (no delta-fold).
    "topology_created",
    "topology_updated",
    "topology_removed",
    # #2187 backend-master: the Task SUBSCRIPTION (the Reyn-internal task↔session
    # binding — assignee + requester). The backend is the external MASTER of task-STATE
    # (status/content/DAG, NOT in the WAL); the WAL holds only what Reyn owns + rewinds:
    # session + subscription. ``task_subscribed`` = a task's initial binding;
    # ``task_rebound`` = the assignee binding changed (reassign / unbind). Applied to the
    # live SubscriptionRegistry (subscription.py) — as-of-cut reconstruction by replay.
    "task_subscribed",
    "task_rebound",
)


class StateLog:
    """Single-file WAL. Process-shared; ownership lives in AgentRegistry."""

    def __init__(self, path: Path, worker: "DurabilityWorker | None" = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._counter = self._scan_max_seq()
        # #1765 Step 1a: the off-loop durability worker (shared with other substrates so all
        # durable writes serialise at one point — the cross-substrate ordering). `_inflight_seq`
        # is the seq THIS log's worker is writing RIGHT NOW (between its file-write and its
        # fsync-ack) — the only non-durable entry. `iter_from` skips it, so the #1751
        # lockless-read surface (a concurrent read of a written-but-not-yet-fsync'd entry) is
        # closed structurally, WITHOUT a per-instance durable ceiling: a stale ceiling would
        # wrongly hide entries durably written by ANOTHER StateLog on the same file (the WAL is
        # process-shared) or already on disk at open. None = nothing in flight (a read at
        # recovery / between appends sees every durable entry). A caller may inject a shared
        # worker; else this log owns one.
        from reyn.core.events.durability_worker import DurabilityWorker  # noqa: PLC0415
        self._worker = worker if worker is not None else DurabilityWorker()
        self._inflight_seq: "int | None" = None
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
        """Append a new entry, fsync (off the event loop), return its seq.

        `kind` must be one of WAL_EVENT_KINDS — caught at write time so a
        typo doesn't silently fragment the recovery vocabulary.

        The seq is assigned under the lock; the worker write+fsync is submitted while the
        lock is held (so the seq order = the file write order, and `truncate_below` — which
        also holds the lock — never overlaps a write). `append` returns only after its entry
        is durable: the per-append crash-recovery contract is unchanged, but the loop is free
        during the (off-loop) fsync. Post-append observers fire after the durable write,
        outside the lock (best-effort, #1560).
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
            }
            entry.update(fields)
            await self._worker.submit(self._durable_wal_write(entry))
        await self._fire_post_append(kind, seq, fields)
        return seq

    def _durable_wal_write(self, entry: dict):
        """Build the durable-write task for `entry` (run by the DurabilityWorker, serially).
        Writes the line, fsyncs OFF the event loop, then bumps `_durable_seq` — strictly
        AFTER the fsync, so `iter_from` (seq <= `_durable_seq`) never exposes a non-durable
        entry. No lock here: the submitting `append` holds `self._lock` across the submit, so
        this runs exclusively (serialised with other appends + `truncate_below`)."""
        async def _task() -> None:
            # Mark this entry in-flight BEFORE it appears in the file, so a concurrent
            # `iter_from` during the fsync skips it (it is written but not yet durable);
            # cleared only AFTER the fsync, when it is durable + readable.
            self._inflight_seq = entry["seq"]
            try:
                payload = json.dumps(entry, ensure_ascii=False) + "\n"
                # Defensive lead-newline (a prior run may have crashed mid-write so the file
                # ends without a newline): start on our own line; the torn fragment parses as
                # garbage + is skipped on replay.
                need_lead_newline = self._needs_lead_newline()
                with self._path.open("a", encoding="utf-8") as f:
                    if need_lead_newline:
                        f.write("\n")
                    f.write(payload)
                    f.flush()
                    await asyncio.to_thread(os.fsync, f.fileno())
            finally:
                self._inflight_seq = None
        return _task

    async def submit_durable(self, do_durable_write) -> None:
        """#1765 Step 1a: submit a durable write from ANOTHER substrate (e.g. a snapshot
        save) to the SAME worker, so it serialises with WAL writes — the single cross-substrate
        ordering point. Blocking (awaits durability), like `append`."""
        await self._worker.submit(do_durable_write)

    async def aclose(self) -> None:
        """Drain + stop the durability worker (graceful shutdown — no in-flight write lost)."""
        await self._worker.aclose()

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
        """Yield WAL entries with `seq >= min_seq`, in file order, EXCEPT this log's
        currently-in-flight entry (written but not yet fsync'd).

        Bad lines (parse errors, partial writes from a crash) are skipped
        silently — recovery should be best-effort from whatever survived.

        #1765 Step 1a: while this log's worker is fsyncing entry N, N is in the file but not
        yet durable; `_inflight_seq == N` makes `iter_from` skip it, so a concurrent read never
        exposes a non-durable entry (the #1751 lockless-read surface, closed structurally) —
        `iter_from` stays a plain sync generator. Only that one in-flight entry is excluded:
        every other on-disk entry IS durable (this log's earlier appends, another StateLog's
        appends on the same shared file, or pre-existing entries), so reads + recovery see them.
        """
        if not self._path.is_file():
            return
        inflight = self._inflight_seq
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
                if seq == inflight:
                    break  # the in-flight (non-durable) entry — the seq-ordered tail
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
