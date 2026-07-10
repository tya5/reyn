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
exposed (fires on every chat event — at least once per turn via
``turn_completed``, plus once per tool call, per hook, etc., vs. #1765's
WAL-append-only exposure). Fixed by routing the write through a
``DurabilityWorker`` (the SAME off-loop primitive #1765 introduced — reused,
not reinvented; substrate-agnostic by that class's own design) via
``submit_nowait``. ``write()`` stays a plain synchronous method — no API
change, no caller updates anywhere ``emit()``/``write()`` is called: only the
actual file I/O is deferred off-loop. Rotation decision + line serialization
still happen synchronously at ``write()``-call time, so enqueue order still
equals emission order; the worker's FIFO guarantee (enqueue order == write
order) then keeps on-disk order matching emission order too — this log's
ordering relative to other synchronous code (WAL appends included) is
unchanged from before this fix, only the blocking part moved off the loop.
Cross-log disk-durability ordering between this store and the WAL is NOT
guaranteed (separate workers/queues) — but nothing depends on that; event
timestamps are stamped synchronously at ``emit()`` time, so a consumer that
correlates the two logs (dogfood_trace, support_bundle) orders by timestamp,
not by which file landed first.

Durability discipline mirrors #1765's WAL fix, per review: the single
off-loop unit (``_write_line_sync``) does open + write + flush + fsync
TOGETHER, so there is no "written but not yet fsynced" exposure window
within one queued job — a line is either not-yet-durable (still queued) or
fully durable (opened, written, and fsynced). ``submit_nowait`` (not
``submit``) means ``write()`` itself does not await that durability — a
RELAXED-durability window between ``write()`` returning and the line
actually landing durably, same accepted trade-off the WAL's own
``append_nowait``/``submit_nowait`` fire-and-forget path already carries
elsewhere in this codebase — not a new risk class.

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
from typing import Iterator

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
    ) -> None:
        """
        dir_path: e.g. `events/agents/researcher/chat`
        max_bytes:       0 disables size-based rotation
        max_age_seconds: 0 disables age-based rotation
                         (date-boundary rotation also gated on this)
        suffix:          "" for chat, e.g. "_run" for a run
        """
        self._dir = Path(dir_path)
        self._max_bytes = int(max_bytes)
        self._max_age_seconds = int(max_age_seconds)
        self._suffix = suffix
        self._active: Path | None = None
        self._active_started_at: datetime | None = None
        # Running byte count of the active file — avoids a `.stat()` syscall
        # on every write() (max_bytes defaults to 10MB, i.e. nonzero, so the
        # old `_should_rotate()` stat() fired on literally every call, not a
        # rare path). Reset to 0 on rotation.
        self._active_size = 0
        # Off-loop write worker (see module docstring). Lazily binds to
        # whichever loop is running on first write() — a store constructed
        # before any loop exists is fine; only submit_nowait touches the loop.
        self._worker = DurabilityWorker()

    # ── public API ──────────────────────────────────────────────────────

    def __call__(self, event: Event) -> None:
        """Subscriber-callable form so EventStore can be plugged into EventLog."""
        self.write(event)

    def write(self, event: Event) -> None:
        """Serialize + enqueue one line for off-loop writing.

        Synchronous (unchanged signature — every ``EventLog.emit()`` caller
        across the codebase stays untouched). Rotation decision + JSON
        serialization happen here, still on the caller's tick, so enqueue
        order == emission order; only the actual ``open``/``write``/``fsync``
        moves off-loop via the worker's FIFO (enqueue order == write order).

        Falls back to a fully synchronous write when no event loop is
        running (e.g. a CLI entry point that never starts one — see
        ``events.py``'s CLI-mode EventStore construction) — ``submit_nowait``
        requires a running loop and would otherwise raise, a regression this
        fix must not introduce for synchronous callers.
        """
        if self._active is None or self._should_rotate():
            self._open_new_file(now=datetime.now())
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        self._active_size += len(line.encode("utf-8")) + 1  # +1 for the trailing "\n"
        path = self._active
        assert path is not None
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._write_line_sync(path, line)
            return
        self._worker.submit_nowait(lambda p=path, ln=line: self._do_write(p, ln))

    async def aclose(self) -> None:
        """Drain every enqueued write before the caller tears down.

        Without this, a normal ``/quit`` can drop the trailing audit events
        (e.g. ``session_completed`` — the very event recording the graceful
        exit) because ``asyncio.run`` cancels outstanding tasks at loop
        teardown. Mirrors ``StateLog.aclose`` — call from the same teardown
        path. A no-op if the worker was never used (nothing queued) or if
        called on a different loop than the one bound at first ``write()``.
        """
        await self._worker.aclose()

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

    async def _do_write(self, path: Path, line: str) -> None:
        await asyncio.to_thread(self._write_line_sync, path, line)

    @staticmethod
    def _write_line_sync(path: Path, line: str) -> None:
        """The actual blocking append — open + write + fsync TOGETHER (no
        written-but-not-fsynced exposure window; see module docstring).

        FileNotFoundError recovery (the active file/parent dir was deleted by
        an external process — e.g. dogfood scripts that wipe .reyn/events/
        between scenarios while the server is still live) recreates the SAME
        path rather than a new timestamped one (that decision belongs to the
        synchronous ``write()``/``_open_new_file`` path, not here — this may
        run off-loop, on a worker thread, with no access to instance state
        beyond the path/line it was given). If the second attempt also fails,
        the exception propagates as a persistent-failure health signal via
        the worker (mirrors #1765's durable-write retry escalation) instead
        of a synchronous raise to the ``emit()`` caller — an accepted
        trade-off for a non-durability-critical audit log (see module
        docstring's crash-recovery note).
        """
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())

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
        """Eagerly create the active file and return its path.

        Useful for callers that print the destination before any event is
        actually written (e.g. `reyn run` shows `events saved → ...`).
        """
        if self._active is None:
            self._open_new_file(now=datetime.now())
        return self._active  # type: ignore[return-value]

    # ── internals ───────────────────────────────────────────────────────

    def _should_rotate(self) -> bool:
        """Size check reads the in-memory running counter, not `.stat()` —
        `max_bytes` defaults to a nonzero 10MB, so `.stat()` used to fire a
        blocking syscall on literally EVERY `write()` call, not a rare path.
        The counter drifts (harmlessly) if an external process appends to the
        same file, after a FileNotFoundError recovery re-creates it, or on
        Windows where text-mode `\n` -> `\r\n` translation makes bytes-on-disk
        exceed the counted `len(line.encode("utf-8")) + 1` — all three only
        shift the rotation point by a bounded amount, never break
        correctness."""
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

    def _open_new_file(self, now: datetime) -> None:
        month_dir = self._dir / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y-%m-%dT%H%M%S")
        candidate = month_dir / f"{ts}{self._suffix}.jsonl"
        self._active = self._unique(candidate)
        self._active.touch()
        self._active_started_at = now
        self._active_size = 0

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
