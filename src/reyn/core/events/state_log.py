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
  step_started        — a plan step has begun
  step_completed      — a step completed successfully (result stored)
  step_failed         — a step failed (error stored)
  intervention_dispatched — ask_user / permission request emitted
  intervention_resolved   — intervention answered

Per P7: this is OS-level generic infrastructure — `kind` strings and
field names live here, not in any domain-specific code.

Off-loop durability (#1765 Step 1a, extended by Step 1b). Step 1a moved the `fsync` OFF the
event loop via the shared `DurabilityWorker` (so a slow disk no longer freezes the loop — TUI
repaint + other sessions keep running DURING the fsync); Step 1b (`_do_wal_write`) extends this
to the `open`/`write`/`flush` themselves + the defensive lead-newline `stat`/`read` check, since
those are NOT free on every platform (Windows AV re-scan / cloud-sync rehydration / a stale
file-lock can stall a plain `open()` after the file has sat idle — same loop-freezing failure
mode Step 1a fixed for `fsync`, just for a different syscall). `append` still returns only once
durable: the per-append crash-recovery contract is UNCHANGED (no relaxed-durability window for
`append`; `append_nowait` is the separate, deliberately-relaxed fire-and-forget path). The
off-loop write would reopen the #1751 surface (a lockless `iter_from` reading a
written-but-not-yet-durable entry) — closed structurally by `_inflight_seq`: the worker marks
the seq it is writing right now (for the WHOLE off-loop window, not just the fsync slice), and
`iter_from` skips exactly that one entry (a single-entry exclusion, NOT a durable ceiling, so
cross-instance reads of a process-shared WAL + recovery still see every durable entry). The seq
is assigned + the worker write submitted under the lock, so seq order = file order, and
`truncate_below` (also under the lock) never overlaps a write.
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
    # plan-step lifecycle (read by the replay/rewind analysis)
    "step_started",
    "step_completed",
    "step_failed",
    "intervention_dispatched",
    "intervention_resolved",
    # NEW (R-D12) — durable buffered intervention answer that survives a
    # second crash before the resuming run consumes it
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
    # (#2248 PR-A added `config_changed` here; #2259 PR-1 removed it — config recovery is now
    # a truncation-surviving GENERATION, not a truncatable WAL event. See config_generations.py.)
    # NEW (#2884) — the hook-driven-turns loop-valve counter's FULL current value (not a
    # delta), recorded at every reset (kind="user") and increment (kind="hook") edge. This
    # kind is a truncatable, between-snapshot replay-maintenance record ONLY — the
    # reconstruction SOURCE-of-truth is the snapshot field (AgentSnapshot.hook_driven_turns),
    # since consumed WAL entries are pruned by truncate_below (the #2259 config-loss class).
    "hook_driven_turns_set",
)


class StateLog:
    """Single-file WAL. Process-shared; ownership lives in AgentRegistry."""

    def __init__(self, path: Path, worker: "DurabilityWorker | None" = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._counter = self._scan_max_seq()
        # #2259 PR-2b: the seq COUNTER is assigned SYNCHRONOUSLY on the event loop (so
        # `current_seq` is immediately correct for the 7 synchronous consumers — config-gen
        # keying etc.; an async-assigned seq would key a config gen at a stale head and re-open
        # the PR-1 truncation bug), and only the WAL WRITE is async (off-loop, fire-and-forget
        # via the worker). No `asyncio.Lock`: the worker is the SINGLE serial venue — every WAL
        # write AND `truncate_below` runs as a worker job, so they serialise by FIFO (the seq
        # increment itself is sync-atomic, no await across it). `_last_assigned_seq` = the seq
        # the most recent append assigned (the paired snapshot save reads it to stamp
        # applied_seq); `_last_durable_seq` = the highest seq DURABLY written (set after fsync) —
        # the (a)=(ii) durable watermark the truncate floor + cut_generation read so they never
        # outrun durability. Both seed from the recovered max (every scanned entry is durable).
        self._last_assigned_seq = self._counter
        self._last_durable_seq = self._counter
        # #2259 PR-2b: truncate_below is FIRE-AND-FORGET (the blocking-invariant), so its stats
        # are recorded here (post-drain readable via `last_truncate_stats` / after `flush`)
        # instead of returned — the caller doesn't wait on the worker.
        self._last_truncate_stats: dict = {
            "dropped": 0, "kept": 0, "min_kept_seq": None, "max_kept_seq": None,
        }
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

    @property
    def last_assigned_seq(self) -> int:
        """#2259 PR-2b: the seq the most recent append assigned (sync). A paired
        ``save_nowait`` reads this (with no ``await`` between the two enqueues) to stamp the
        snapshot's ``applied_seq`` — so the snapshot records exactly its WAL entry's seq."""
        return self._last_assigned_seq

    @property
    def last_durable_seq(self) -> int:
        """#2259 PR-2b: the (a)=(ii) durable watermark — the highest seq DURABLY written
        (advanced after each fsync). The truncate floor + cut_generation read this (never the
        in-memory `current_seq`), so truncation can never outrun durability (drop a WAL entry a
        not-yet-durable snapshot still needs)."""
        return self._last_durable_seq

    @property
    def durability_failed(self) -> bool:
        """#2259 PR-3: True once a fire-and-forget durable write failed PERSISTENTLY (§4-exhausted)
        — the worker's latched health-signal. The session fail-stops on this (rejects new ops +
        halts the run loop) so in-memory state cannot race ahead of a dead disk."""
        return self._worker.durability_failed

    @property
    def last_truncate_stats(self) -> dict:
        """#2259 PR-2b: the stats of the most recent `truncate_below` (dropped / kept /
        min_kept_seq / max_kept_seq). Since truncate is fire-and-forget, read this AFTER `flush`
        (or `aclose`) to observe a truncate's result — it is not returned synchronously."""
        return self._last_truncate_stats

    async def flush(self) -> None:
        """#2259 PR-2b: wait until every enqueued durable write (WAL appends, snapshot saves,
        a truncate rewrite) has drained — without closing the worker. A barrier for callers that
        must observe a fire-and-forget write's durable effect (tests; deliberate sync points)."""
        await self._worker.flush()

    async def append(self, kind: str, **fields) -> int:
        """Append a new entry, fsync (off the event loop), return its seq — BLOCKING (awaits
        durability). The non-blocking counterpart is `append_nowait`.

        `kind` must be one of WAL_EVENT_KINDS — caught at write time so a
        typo doesn't silently fragment the recovery vocabulary.

        #2259 PR-2b: the seq is assigned SYNCHRONOUSLY (sync-atomic on the loop — no lock; the
        worker is the single serial venue serialising the write + `truncate_below`), then the
        write+fsync runs in the worker. `append` AWAITS it, so its per-append crash-recovery
        contract is unchanged (returns only once durable) while the loop is free during the
        (off-loop) fsync. Post-append observers fire in the durable-write job, after the fsync
        (best-effort, #1560).
        """
        if kind not in WAL_EVENT_KINDS:
            raise ValueError(f"unknown WAL event kind: {kind!r}")
        holder: dict = {}
        await self._worker.submit(self._wal_write_job(kind, fields, holder))
        return holder["seq"]

    def append_nowait(self, kind: str, **fields) -> None:
        """#2259 PR-2b: append NON-BLOCKING + NO-RETURN. The seq is assigned IN the worker (the
        WAL job, serial → monotonic = durability order), NEVER on the task loop — so no consumer
        can key a durable artifact at a not-yet-durable seq (the hole the sync-seq had; the owner
        caught it). The paired ``save_nowait``'s snapshot job reads ``last_assigned_seq`` (set by
        the WAL job, which the worker's FIFO runs first). SYNCHRONOUS enqueue, so an
        (append_nowait, save_nowait) pair with NO ``await`` between is atomic on the loop — no
        concurrent mutation's WAL job interleaves between the pair → ``snap_N`` reads ``WAL_N``'s
        seq, never a later one (invariant #2). The hot path NEVER awaits durability (the
        blocking-invariant: submit-and-proceed)."""
        if kind not in WAL_EVENT_KINDS:
            raise ValueError(f"unknown WAL event kind: {kind!r}")
        self._worker.submit_nowait(self._wal_write_job(kind, fields, None))

    def _wal_write_job(self, kind: str, fields: dict, holder: "dict | None"):
        """The durable WAL-write job (run by the DurabilityWorker, serially — the single serial
        venue, so no lock). At drain it ASSIGNS the seq (``++_counter`` — serial, so monotonic =
        durability order, never racing) and sets ``_last_assigned_seq`` (the paired snapshot job,
        FIFO-after, reads it to stamp ``applied_seq``); writes the line + fsyncs OFF the loop
        (``_do_wal_write``, off-loop — see #1765-Step-1b below); then advances
        ``_last_durable_seq`` strictly AFTER the write (the watermark never names a non-durable
        entry). ``_inflight_seq`` (the one entry mid-write — the worker is serial, so only ever
        one) keeps ``iter_from`` from exposing it for the WHOLE off-loop window. Post-append
        observers fire after the durable write. A blocking ``append`` passes a per-call
        ``holder`` to receive the seq."""
        async def _task() -> None:
            self._counter += 1
            seq = self._counter
            self._last_assigned_seq = seq
            if holder is not None:
                holder["seq"] = seq
            entry = {"seq": seq, "ts": datetime.now().isoformat(), "kind": kind}
            entry.update(fields)
            # Mark this entry in-flight BEFORE it appears in the file, so a concurrent
            # `iter_from` during the write/fsync skips it (written but not yet durable);
            # cleared only AFTER the write, when it is durable + readable.
            self._inflight_seq = seq
            try:
                payload = json.dumps(entry, ensure_ascii=False) + "\n"
                await asyncio.to_thread(self._do_wal_write, payload)
            finally:
                self._inflight_seq = None
            self._last_durable_seq = seq  # durable now → the watermark advances
            await self._fire_post_append(kind, seq, fields)
        return _task

    def _do_wal_write(self, payload: str) -> None:
        """#1765 Step 1b: the synchronous WAL-line write (run off-loop via ``asyncio.to_thread``,
        mirroring ``_do_truncate``'s pattern). Step 1a moved ONLY ``os.fsync`` off the loop; the
        preceding ``_needs_lead_newline`` check (a ``stat`` + tail-byte ``read``) and the
        ``open``/``write``/``flush`` themselves stayed SYNCHRONOUS on the loop, on the (POSIX-
        biased) assumption that appending to an already-resident file is effectively instant. On
        Windows that assumption doesn't hold after the file has sat idle: a real-time antivirus
        re-scan, a cloud-sync (OneDrive/Dropbox) placeholder needing to rehydrate, or a stale
        file-lock retry can each stall an ``open``/``stat``/``read`` for seconds to minutes —
        and because this job runs on the SAME event loop as the TUI repaint and the whole
        turn-processing pipeline, that stall freezes everything (the "message echoes, but
        Working… never appears" symptom), not just this WAL append. Folding the lead-newline
        check + open/write/flush/fsync into ONE thread-hop keeps every synchronous file access
        for this append off the loop, closing the gap Step 1a left open."""
        need_lead_newline = self._needs_lead_newline()
        with self._path.open("a", encoding="utf-8") as f:
            if need_lead_newline:
                f.write("\n")
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

    async def submit_durable(self, do_durable_write) -> None:
        """#1765 Step 1a: submit a durable write from ANOTHER substrate (e.g. a snapshot
        save) to the SAME worker, so it serialises with WAL writes — the single cross-substrate
        ordering point. Blocking (awaits durability), like `append`."""
        await self._worker.submit(do_durable_write)

    def submit_durable_nowait(self, do_durable_write) -> None:
        """#2259 PR-2b: submit a durable write from another substrate (a snapshot save)
        FIRE-AND-FORGET — the non-blocking counterpart of `submit_durable`. Used by
        `save_nowait` so the (WAL append_nowait, snapshot save_nowait) pair is two synchronous
        enqueues with no await between (atomic on the loop). Serialises with WAL writes (same
        worker, FIFO = durability order)."""
        self._worker.submit_nowait(do_durable_write)

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

    async def truncate_below(
        self,
        min_keep_seq: int,
        *,
        always_keep_kinds: "frozenset[str] | None" = None,
    ) -> None:
        """Atomically rewrite the WAL keeping only entries with ``seq >= min_keep_seq``.

        Drop policy: ``seq < min_keep_seq`` entries are dropped. Caller computes
        ``min_keep_seq`` as ``min(全 agent applied_seq) + 1`` (i.e. drop everything strictly below the
        last universally-absorbed seq).

        ``always_keep_kinds``: entries whose ``kind`` is in this set are kept
        unconditionally regardless of seq. Pass
        ``frozenset({REWIND_KIND})`` (``snapshot_generations.REWIND_KIND``) to
        protect reset-records from being dropped: ``_active_branch_history`` calls
        ``is_active_seq`` with ``wal_seq`` values from ``history.jsonl`` (which are
        append-only and can be below the floor), so dropping a rewind record causes
        abandoned conversation turns to reappear in the LLM context.

        Atomic strategy (mirrors per-snapshot atomic save):
          1. Stream-read current ``wal.jsonl``, write surviving entries to
             ``wal.jsonl.tmp``.
          2. ``fsync(tmp)`` then ``rename(tmp, wal.jsonl)``.
          3. A mid-rewrite crash leaves the original ``wal.jsonl`` intact
             — incomplete ``.tmp`` is ignored at next startup.

        Records a stats dict on ``last_truncate_stats`` (``{"dropped", "kept",
        "min_kept_seq", "max_kept_seq"}``) — read it after ``flush`` (the rewrite is
        fire-and-forget). ``min_keep_seq <= 1`` is a no-op (everything would be kept).

        #2259 PR-2b: FIRE-AND-FORGET (the blocking-invariant — the GC caller does not await the
        worker). The rewrite runs as a worker job (FIFO-serialised with every WAL write job — no
        append's write overlaps the rename — and off the event loop via ``to_thread``); a failure
        is handled by the worker's §4 retry → health-signal (no caller to raise to). The stats
        land on ``last_truncate_stats`` (read after ``flush``), not a return value.
        """
        if min_keep_seq <= 1 and not always_keep_kinds:
            self._last_truncate_stats = {
                "dropped": 0, "kept": 0, "min_kept_seq": None, "max_kept_seq": None,
            }
            return

        async def _job() -> None:
            self._last_truncate_stats = await asyncio.to_thread(
                self._do_truncate, min_keep_seq, always_keep_kinds,
            )

        self._worker.submit_nowait(_job)

    def _do_truncate(
        self,
        min_keep_seq: int,
        always_keep_kinds: "frozenset[str] | None" = None,
    ) -> dict:
        """The synchronous WAL rewrite (run off-loop, serialised by the worker — see
        `truncate_below`). Two-pass: identify the highest seq present (never drop ALL entries —
        that would let `_scan_max_seq` reset the counter + re-issue used seqs), then write the
        survivors to a `.tmp` and atomically rename. A mid-rewrite crash leaves the original
        intact (the incomplete `.tmp` is ignored at next startup)."""
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
                    # always_keep_kinds entries (e.g. "rewind" reset-records) survive
                    # truncation regardless of seq: _active_branch_history queries
                    # is_active_seq with wal_seq anchors from history.jsonl (append-only,
                    # never truncated), so dropping a rewind record below the floor lets
                    # abandoned conversation turns reappear in the LLM context.
                    if always_keep_kinds and entry.get("kind") in always_keep_kinds:
                        pass  # keep despite being below floor
                    else:
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
