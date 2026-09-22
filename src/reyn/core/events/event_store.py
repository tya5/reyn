"""EventStore — file-backed audit log with rotation.

Used by both chat sessions (long-lived, rotated by size+age+date) and
agent runs (1 run = 1 file, no rotation). Same API for both — the
difference is the rotation policy passed at construction.

Files live under `<dir>/<YYYY-MM>/<YYYY-MM-DDTHHMMSS>[<suffix>].jsonl`.
filename start-time prefix means lexical sort = chronological order.

Rotation creates a NEW file (no rename). The previous file is left in
place and remains readable. This sidesteps mid-rotation crash hazards
that rename-based schemes have.

Per P7: this is OS-level generic infrastructure — it never references
specific event types or domain strings.

Off-loop write (owner dogfood finding, 2026-07-10): ``write()`` used to
``open()``/``write()`` synchronously, directly on the event loop — the SAME
class of bug as #1765's WAL append (a filesystem stall freezes the WHOLE
event loop), except unmitigated (not even fsync was offloaded) and far more
exposed (fires on every audit event — at least once per turn via
``turn_completed``, plus once per tool call, per hook, etc., vs. #1765's
WAL-append-only exposure). Fixed by routing the write through a
``DurabilityWorker`` (the SAME off-loop primitive #1765 introduced — reused,
not reinvented; substrate-agnostic by that class's own design) via
``submit_nowait``. ``write()`` stays a plain synchronous method — no API
change, no caller updates anywhere ``emit()``/``write()`` is called: only the
actual file I/O is deferred off-loop.

#6077 (owner-hit, proposal 3, architect ruling): the FIRST cut of this fix
still ran ``json.dumps`` — the CPU-bound serialization, not just the I/O — on
the event loop, plus the rotation accounting (``_should_rotate``) and
``_open_new_file``'s ``mkdir``/``touch``/purge-trigger, all synchronously in
``write()``. Architect's ruling: the cut is between ``model_dump`` (capture —
fixes THIS moment's value; verified empirically the dump is a deep copy, so a
caller mutating ``event.data`` after ``write()`` returns cannot change what
gets persisted) and ``dumps`` (representation — turns the captured value into
bytes). ``model_dump`` stays on the loop; ``dumps`` moves off. And per
architect: rotation's own accounting (the byte counter ``_should_rotate``
reads) is now DERIVED FROM ``dumps``'s output, so it has to move to the SAME
owner ``dumps`` moved to — otherwise the loop would still be "judging" file
state (size/date) while the worker does the "writing," the exact split
architect flagged as a window, not a speed concern. The OLD
``_open_new_file``'s own ``mkdir``/``touch`` disappear from the hot
(per-write) path for a subtler reason than "move them": the OLD
``_write_line_sync``'s own ``FileNotFoundError`` recovery ALREADY did
``mkdir(parents=True)`` + ``touch()`` on demand for a path whose parent
doesn't exist yet — which is exactly what a brand-new rotated file's
month-dir is, the first time. Reusing that ONE existing code path instead
of adding a second one (which duplicated it, on the loop) is the "single
owner" architect asked for. #6077 提案 6 (below) folds both OLD functions
(``_open_new_file`` and ``_write_line_sync``) into the SAME single owner
this paragraph describes — ``_ensure_active_handle`` is that one function
today: it decides "does this path need creating/reopening" and does it,
never two functions split across loop and worker.
The automatic-purge trigger (``submit_auto_purge``'s job body) moves inline
into the same off-loop call for the identical reason, called directly
(``apply_auto_purge``) rather than via ``submit_auto_purge`` — the write job
already runs on a worker thread via ``asyncio.to_thread``, and
``asyncio.get_running_loop()``/``submit_nowait`` are event-loop-thread-only
APIs, so re-entering them from there would be a cross-thread asyncio
violation, not a durability fix. ``submit_auto_purge`` itself stays available
unchanged as a directly-callable public method (`open()`'s own eager,
low-frequency path uses it, and it has its own direct tests).

Net effect: the loop-side ``write()`` now does exactly two things — capture
(``model_dump``) and enqueue (``submit_nowait``). Everything that decides
"does the active file need rotating," "does its directory/file need
creating," "does it need purging," and "what are its bytes" now happens in
ONE place, off-loop, serialized by the worker's own FIFO (one write job at a
time) — the same discipline the sync-mode fallback (below) mirrors inline
when no loop is running to protect.

Enqueue order still equals emission order (``write()``'s ``model_dump`` +
``submit_nowait`` call happens synchronously, with no ``await`` between, on
the caller's own tick); the worker's FIFO guarantee (enqueue order == write
order) then keeps on-disk order matching emission order too — this log's
ordering relative to other synchronous code (WAL appends included) is
unchanged from before this fix, only the blocking part moved off the loop.
Cross-log disk-durability ordering between this store and the WAL is NOT
guaranteed (separate workers/queues) — but nothing depends on that; event
timestamps are stamped synchronously at ``emit()`` time, so a consumer that
correlates the two logs (dogfood_trace, support_bundle) orders by timestamp,
not by which file landed first.

One observable consequence of moving rotation off-loop: ``active_path``
(previously always in sync immediately after a synchronous ``write()``
call, when a loop is running) is now eventually consistent for that case —
it reflects the state as of the last write the worker actually drained, not
the last one enqueued. A caller that needs the current value after enqueuing
writes must ``await flush()``/``aclose()`` first (the existing established
pattern every purge test in this file's test suite already uses). The
no-running-loop sync fallback is unaffected — there is no other coroutine
sharing that thread to protect, so it decides + creates + writes inline, and
``active_path`` stays immediately accurate there, same as before.

#6077 提案 6 (architect ruling, same PR — same ownership question as 提案 3
above): a 95,030-event workdir means 95,030 ``open``/``write``/``fsync``
round-trips through the worker — the SAME class of defect #6247 fixed for
``history.jsonl``'s own per-message ``open``/``close`` (real-time AV hooks
file OPEN, not write/flush), one order of magnitude up. Architect rejected
both a separate worker (removes zero syscalls — only resolves a shared-FIFO
wait) and batching writes (reduces ``fsync`` count, which WEAKENS
durability — a wider not-yet-fsynced window — and isn't needed anyway).
The ruling instead separates ``open``/``close`` from ``fsync`` as different
costs: ``EventStore`` now holds a session-lifetime file handle
(``_active_fh``), opened once (lazily) and reused across writes —
collapsing ``open``/``close`` from once-per-event to roughly
once-per-rotation — while ``fsync`` stays exactly once per event,
unchanged. Durability is NOT touched by this proposal in any way. Holding a
handle open introduces the SAME inode hazard #6247's own review flagged for
ITS held-open handle: a write through a handle whose file was deleted (the
existing ``FileNotFoundError``-recovery scenario documented below) or
replaced out from under it succeeds SILENTLY at the OS level while landing
in an orphaned, invisible inode — so ``_ensure_active_handle`` verifies the
handle's inode still matches ``active_path`` via a cheap ``stat`` (NOT an
``open`` — that's the cost being removed) before every write, and
reopens/recovers when it doesn't. See ``_ensure_active_handle``'s own
docstring.

Durability discipline mirrors #1765's WAL fix, per review: the single
off-loop unit (``EventStore._write_owned``, via its own file handle) does
write + flush + fsync TOGETHER for every event, so there is no "written but
not yet fsynced" exposure window within one queued job — a line is either
not-yet-durable (still queued) or fully durable (written and fsynced).
``submit_nowait`` (not ``submit``) means ``write()`` itself does not await
that durability — a RELAXED-durability window between ``write()`` returning
and the line actually landing durably, same accepted trade-off the WAL's
own ``append_nowait``/``submit_nowait`` fire-and-forget path already
carries elsewhere in this codebase — not a new risk class.

This is an AUDIT log, not a recovery source: ``anchor_store.py`` explicitly
documents that EventStore has no WAL seq and is deliberately not used for
rewind/reconstruction ("mining the audit EventStore... would need a
cross-log join" — see #1547). So this fix carries no crash-recovery
truncate-falsify obligation (CLAUDE.md's recovery-feature PR gate governs
WAL-event-derived reconstruction state; this isn't that) — but per review,
the audit log's at-least-once durability still matters for the
`events.md` audit-truth contract, hence ``aclose()`` below (drains every
queued write before process/session teardown, so a graceful ``/quit``
doesn't silently drop the tail of the audit trail — see its docstring).
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from io import TextIOWrapper

from reyn.core.events.durability_worker import DurabilityWorker
from reyn.schemas.models import Event


class EventStore:
    def __init__(
        self,
        dir_path: Path,
        *,
        max_bytes: int = 0,
        max_age_seconds: int = 0,
        suffix: str = "",
        cleanup_period_days: int = 0,
        max_disk_usage_percent: float = 0.0,
    ) -> None:
        """
        dir_path: e.g. `events/agents/researcher/chat`
        max_bytes:       0 disables size-based rotation
        max_age_seconds: 0 disables age-based rotation
                         (date-boundary rotation also gated on this)
        suffix:          "" for chat, e.g. "_run" for a run
        cleanup_period_days / max_disk_usage_percent: #4479 automatic
            purge axes (0 disables that axis; see `AuditEventsConfig`'s own
            docstring for the full rationale) — consulted (via
            `_run_auto_purge_sync`, or `submit_auto_purge` for `open()`'s
            own rare eager path) whenever a new active file is created.
        """
        self._dir = Path(dir_path)
        self._max_bytes = int(max_bytes)
        self._max_age_seconds = int(max_age_seconds)
        self._suffix = suffix
        self._cleanup_period_days = int(cleanup_period_days)
        self._max_disk_usage_percent = float(max_disk_usage_percent)
        self._active: Path | None = None
        self._active_started_at: datetime | None = None
        # Running byte count of the active file — avoids a `.stat()` syscall
        # on every write() (max_bytes defaults to 10MB, i.e. nonzero, so the
        # old `_should_rotate()` stat() fired on literally every call, not a
        # rare path). Reset to 0 on rotation.
        self._active_size = 0
        # #6077 提案 6 (architect ruling): a session-lifetime append handle
        # onto `self._active`, opened once (lazily) and reused by every
        # write until rotation/recovery closes it — collapses `open`/
        # `close` from once-per-event to roughly once-per-rotation (a
        # 95,030-event workdir's own count, per the same-issue's own
        # measurement). Owned + mutated ONLY from `_write_owned` (and
        # `open()`/`aclose()`, which never race it — see their own
        # docstrings) — never opened/closed per write. See
        # `_ensure_active_handle`'s docstring for why a per-write `.stat()`
        # (not `.open()`) is still required despite holding this handle.
        self._active_fh: "TextIOWrapper | None" = None
        # Off-loop write worker (see module docstring). Lazily binds to
        # whichever loop is running on first write() — a store constructed
        # before any loop exists is fine; only submit_nowait touches the loop.
        self._worker = DurabilityWorker()

    # ── public API ──────────────────────────────────────────────────────

    def __call__(self, event: Event) -> None:
        """Subscriber-callable form so EventStore can be plugged into EventLog."""
        self.write(event)

    def write(self, event: Event) -> None:
        """Capture + enqueue one line for off-loop writing.

        Synchronous (unchanged signature — every ``EventLog.emit()`` caller
        across the codebase stays untouched). Only ``model_dump`` (the
        capture — fixes THIS moment's value; a deep copy, verified
        empirically, so a caller mutating ``event.data`` after this call
        cannot change what gets persisted) happens here, on the caller's
        tick, so enqueue order == emission order. EVERYTHING else —
        ``json.dumps``, the rotation decision + its own byte/date
        accounting, the active file's handle (open/close/recovery), and
        the auto-purge trigger — is owned by ``_write_owned`` (see its own
        docstring for why all of that had to move together, not just
        ``dumps``), invoked off-loop via the worker's FIFO (enqueue order
        == write order) when a loop is running.

        Falls back to calling ``_write_owned`` directly (no worker) when no
        event loop is running (e.g. a CLI entry point that never starts one
        — see ``events.py``'s CLI-mode EventStore construction) —
        ``submit_nowait`` requires a running loop and would otherwise raise,
        a regression this fix must not introduce for synchronous callers.
        There is no other coroutine sharing that thread to protect, so
        deciding + creating + writing inline is exactly as safe as always.
        """
        data = event.model_dump(mode="json")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._write_owned(data)
            return
        self._worker.submit_nowait(lambda d=data: self._do_write(d))

    async def aclose(self) -> None:
        """Drain every enqueued write, then close the active file handle.

        Without the drain, a normal ``/quit`` can drop the trailing audit
        events (e.g. ``session_completed`` — the very event recording the
        graceful exit) because ``asyncio.run`` cancels outstanding tasks at
        loop teardown. Mirrors ``StateLog.aclose`` — call from the same
        teardown path. Closing the handle here (rather than leaving it for
        GC) matters for the SAME reason #6247 closes its own session-
        lifetime handle at end-of-life: an unclosed handle is a Windows file
        lock past this store's own usable lifetime. A no-op (drain-wise) if
        the worker was never used (nothing queued) or if called on a
        different loop than the one bound at first ``write()`` — but the
        handle close still runs regardless (it touches no loop-bound state).
        """
        await self._worker.aclose()
        self._close_active_handle()

    async def flush(self) -> None:
        """Wait until every currently-enqueued write has landed on disk,
        WITHOUT closing the store (it stays usable afterward).

        For any caller that needs to observe this store's on-disk state
        synchronized with its own emit() calls before doing something that
        reads the file from OUTSIDE this process (e.g. a test spawning a
        subprocess that reads the events file fresh) — since ``write()`` is
        fire-and-forget, nothing else guarantees that ordering. A no-op if
        nothing is queued or if called on a different loop than the one
        bound at first ``write()``."""
        await self._worker.flush()

    async def _do_write(self, data: dict) -> None:
        await asyncio.to_thread(self._write_owned, data)

    def _write_owned(self, data: dict) -> None:
        """SOLE owner of "does the active file need rotating, creating, or
        recovering, and what are this line's bytes" (#6077 提案 3 + 提案 6,
        architect ruling) — invoked either directly (no-loop sync fallback)
        or off-loop via ``asyncio.to_thread``, serialized one job at a time
        by the worker's own drain loop, so mutating
        ``self._active``/``_active_started_at``/``_active_size``/
        ``_active_fh`` here needs no lock: nothing else touches them
        concurrently (``write()`` itself never reads or writes them any
        more — it only calls ``model_dump`` + enqueues).

        Rotation decision unchanged in spirit (in-memory counter, never a
        ``.stat()`` for SIZE — see ``_should_rotate``'s own docstring) but
        now evaluated here because the byte count it reads is derived from
        THIS function's own ``json.dumps`` output, so the "judge" (does the
        file need rotating, by size/date) and the "act" (create/open/write
        it) must be the same owner — the split architect flagged as a
        window, not a speed concern.

        ``open``/``close`` collapse from once-per-event to once-per-active-
        file (#6077 提案 6) via ``_ensure_active_handle``'s session-lifetime
        handle. ``fsync`` stays exactly once per event — durability is
        UNCHANGED by either proposal (see ``_ensure_active_handle`` and
        module docstring)."""
        now = datetime.now()
        is_new_path = self._active is None or self._should_rotate()
        if is_new_path:
            self._begin_new_active_file(now)
        self._ensure_active_handle()
        line = json.dumps(data, ensure_ascii=False)
        self._active_size += len(line.encode("utf-8")) + 1  # +1 for the trailing "\n"
        fh = self._active_fh
        assert fh is not None
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
        if is_new_path:
            self._run_auto_purge_sync()

    def _begin_new_active_file(self, now: datetime) -> None:
        """Pick the NEW active path (timestamp + ``_unique`` dedup) and
        reset its accounting. Closes any handle open on the OLD active file
        first — rotation always means a new inode, so the old handle would
        otherwise leak (mirrors #6247's own close-before-replace
        discipline)."""
        self._close_active_handle()
        self._active = self._next_active_path(now)
        self._active_started_at = now
        self._active_size = 0

    def _next_active_path(self, now: datetime) -> Path:
        month_dir = self._dir / now.strftime("%Y-%m")
        ts = now.strftime("%Y-%m-%dT%H%M%S")
        candidate = month_dir / f"{ts}{self._suffix}.jsonl"
        return self._unique(candidate)

    def _ensure_active_handle(self) -> None:
        """(Re)open ``self._active_fh`` onto ``self._active`` — the SOLE
        place this store opens a file for writing (#6077 提案 6: collapses
        `open`/`close` from once-per-event to roughly once-per-rotation).

        A held-open handle needs its own staleness check that a per-write
        ``open()`` never did: on POSIX, ``write()`` through a handle whose
        file was deleted/replaced out from under it (external deletion —
        e.g. dogfood scripts wiping ``.reyn/events/`` while the server is
        live — or a rotation elsewhere naming the SAME path) succeeds
        SILENTLY at the OS level while becoming invisible to every future
        reader of ``self._active`` — the exact inode hazard #6247 hit for
        ``history.jsonl``'s own held-open handle. So every write verifies
        the handle's inode still matches what ``self._active`` currently
        names via ``stat`` (cheap, and NOT the cost proposal 6 removes —
        that cost is ``open``, which real-time AV hooks; a bare ``stat``
        isn't an open) — never trusts "no exception yet" as proof the
        handle is still good, the way the old per-write ``open()`` could.

        ``FileNotFoundError`` recovery (parent dir or file missing —
        rotation into a fresh month-dir, or the same external-deletion
        case above) recreates the path and reopens, mirroring the OLD
        ``_write_line_sync``'s own recovery (now folded in here, the one
        place file creation happens, instead of split across the loop's
        ``_open_new_file`` and the worker's ``_write_line_sync`` — the
        duplication architect's ownership ruling removes)."""
        path = self._active
        assert path is not None
        if self._active_fh is not None and not self._active_fh.closed:
            if self._handle_matches_path(self._active_fh, path):
                return
            self._close_active_handle()
        try:
            self._active_fh = path.open("a", encoding="utf-8")
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            self._active_fh = path.open("a", encoding="utf-8")

    @staticmethod
    def _handle_matches_path(fh: "TextIOWrapper", path: Path) -> bool:
        """True when ``fh``'s underlying inode is still the one ``path``
        currently names. See ``_ensure_active_handle``'s docstring for why
        this check exists at all (a held-open handle has no other way to
        learn its file was deleted/replaced)."""
        try:
            fh_ino = os.fstat(fh.fileno()).st_ino
            path_ino = path.stat().st_ino
        except OSError:
            return False
        return fh_ino == path_ino

    def _close_active_handle(self) -> None:
        fh = self._active_fh
        self._active_fh = None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass

    def _run_auto_purge_sync(self) -> None:
        """Run the #4479 automatic-purge check INLINE, in the same off-loop
        call as the write that just created a new active file (#6077 提案
        6, architect ruling) — a direct ``apply_auto_purge`` call, NOT
        ``submit_auto_purge``'s own ``get_running_loop()``-gated
        re-enqueue: this already executes on a worker thread (when reached
        via the async path) or the caller's own thread (sync fallback), and
        ``asyncio.get_running_loop()``/``submit_nowait`` are event-loop-
        thread-only APIs — calling them from here would be a cross-thread
        asyncio violation, not a durability fix. ``submit_auto_purge`` stays
        available unchanged as its own directly-callable public method
        (``open()``'s low-frequency eager path uses it, and it has its own
        direct tests)."""
        if self._cleanup_period_days <= 0 and self._max_disk_usage_percent <= 0:
            return
        from reyn.core.events.event_purge import apply_auto_purge
        apply_auto_purge(
            self._dir,
            max_age_days=self._cleanup_period_days,
            max_disk_usage_percent=self._max_disk_usage_percent,
        )

    def iter_all(self) -> Iterator[Event]:
        """Yield every event in this store in chronological order.

        Walks `<dir>/<YYYY-MM>/*.jsonl` in lexical order — since filenames
        are start-time prefixed, lexical order is chronological. Bad lines
        are skipped silently (mid-write crash leaves the last line partial).
        """
        for path in self.iter_files():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        yield Event.model_validate(raw)
                    except Exception:
                        continue

    def iter_files(self) -> list[Path]:
        """Return all .jsonl files in this store, chronological order."""
        if not self._dir.is_dir():
            return []
        out: list[Path] = []
        for month_dir in sorted(self._dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for f in sorted(month_dir.glob("*.jsonl")):
                out.append(f)
        return out

    @property
    def active_path(self) -> Path | None:
        return self._active

    def open(self) -> Path:
        """Eagerly create the active file (+ its handle) and return its path.

        Useful for callers that print the destination before any event is
        actually written (e.g. `reyn run` shows `events saved → ...`). Rare
        + low-frequency (unlike `write()`'s hot path), so it stays a
        straightforward direct call — no worker involved either way.
        """
        if self._active is None:
            self._begin_new_active_file(datetime.now())
            self._ensure_active_handle()
            self.submit_auto_purge()
        return self._active  # type: ignore[return-value]

    # ── internals ───────────────────────────────────────────────────────

    def _should_rotate(self) -> bool:
        """Size check reads the in-memory running counter, never a `.stat()`
        of `st_size` — `max_bytes` defaults to a nonzero 10MB (ON by
        default; see the module docstring's #6077 note), so a `.stat()`-per-
        call here would fire on literally EVERY write, not a rare path.
        (`_ensure_active_handle`'s own per-write `.stat()`, added by #6077
        提案 6, is a DIFFERENT check — inode identity, not size — so it does
        not reintroduce this.) The counter drifts (harmlessly) if an
        external process appends to the same file, after a
        FileNotFoundError recovery re-creates it, or on Windows where
        text-mode `\n` -> `\r\n` translation makes bytes-on-disk exceed the
        counted `len(line.encode("utf-8")) + 1` — all three only shift the
        rotation point by a bounded amount, never break correctness. Called
        only from `_write_owned` (#6077 提案 3) — the judge (this) and the
        actor (creating/writing the file) are now the same owner."""
        if self._active is None or self._active_started_at is None:
            return False
        if self._max_bytes <= 0 and self._max_age_seconds <= 0:
            return False
        if self._max_bytes > 0 and self._active_size >= self._max_bytes:
            return True
        now = datetime.now()
        if self._max_age_seconds > 0:
            elapsed = (now - self._active_started_at).total_seconds()
            if elapsed >= self._max_age_seconds:
                return True
            # Date boundary: rotation also fires when the local date rolls
            # over, so a "daily" file naturally aligns with calendar days.
            if now.date() != self._active_started_at.date():
                return True
        return False

    def submit_auto_purge(self) -> None:
        """Fire-and-forget an automatic purge check (#4479) off the event
        loop, via this store's own `DurabilityWorker`. No-op when both
        axes are disabled, or when no event loop is running (a sync-mode
        caller — e.g. the CLI-mode `EventStore`, `events.py`'s own replay
        construction — gets no automatic purge; `reyn events purge` stays
        the explicit path for those).

        This is the STANDALONE, directly-callable entry point (has its own
        tests calling it in isolation) — `write()`'s own hot path does NOT
        route through this any more (#6077 提案 6): it calls
        `apply_auto_purge` directly from `_run_auto_purge_sync`, inline in
        the SAME off-loop call as the write that triggered a new active
        file, because that call already runs on a worker thread where
        `asyncio.get_running_loop()`/`submit_nowait` (both used below)
        would be a cross-thread asyncio violation. `open()`'s own rare,
        eager path is this method's only remaining internal caller —
        low-frequency enough that the loop-thread-only re-enqueue this
        does is fine there."""
        if self._cleanup_period_days <= 0 and self._max_disk_usage_percent <= 0:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        root = self._dir
        max_age_days = self._cleanup_period_days
        max_disk_usage_percent = self._max_disk_usage_percent

        async def _job() -> None:
            from reyn.core.events.event_purge import apply_auto_purge
            await asyncio.to_thread(
                apply_auto_purge, root,
                max_age_days=max_age_days,
                max_disk_usage_percent=max_disk_usage_percent,
            )

        self._worker.submit_nowait(_job)

    @staticmethod
    def _unique(path: Path) -> Path:
        """If `path` already exists, append `_1`, `_2`, ... before `.jsonl`.

        We use `_N` (not `-N`) so collisions sort AFTER the base file
        lexically: `.` (0x2E) < `_` (0x5F). With `-N` (0x2D) the collision
        files would sort BEFORE the base, breaking chronological iter_all.
        """
        if not path.exists():
            return path
        stem = path.stem  # "<ts><suffix>"
        for n in range(1, 10000):
            candidate = path.with_name(f"{stem}_{n}.jsonl")
            if not candidate.exists():
                return candidate
        # Implausible — bail out with the original to avoid infinite loop
        return path
