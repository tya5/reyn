"""Shared PID advisory-lock primitives for index build coordination.

FP-0057 Phase 0: before this module, "is a PID still alive?" + "read/write
a ``{pid, ts}`` marker file" existed in **two separately-implemented
shapes**:

  - ``reyn.tools.action_index`` — a *non-blocking take-or-skip* lock
    (``_try_acquire_build_lock``): a live holder means "skip, someone
    else is already building"; the caller falls back to whatever's on
    disk rather than waiting.
  - ``reyn.data.index.source_manifest`` — a *raise-on-contention* lock
    (``SourceManifest.acquire_source_lock``): a live holder means "refuse
    loudly" (``SourceLockedError``) so the caller doesn't proceed at all.

Both shapes are legitimate (different call sites want different
contention behaviour) but each carried its own private ``_pid_alive`` +
marker read/write. This module is the single canonical home for the
liveness check and the marker file mechanics; the two call sites keep
their distinct control flow but delegate the PID plumbing here.
"""
from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Iterator


def pid_alive(pid: int) -> bool:
    """Best-effort check whether a PID corresponds to a live process.

    On POSIX, ``os.kill(pid, 0)`` raises ProcessLookupError when the PID
    is gone and PermissionError when it exists but is not ours; both
    mean "still alive enough to defer to". Windows is best-effort.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def read_lock_holder(lock_path: Path) -> int | None:
    """Return the PID recorded in a ``{pid, ts}`` marker file, or None.

    None covers: file absent, unreadable, malformed JSON, or a missing/
    non-integer ``pid`` key — all treated as "no usable holder" so
    callers reap rather than wedge on a corrupt marker.
    """
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
        return int(data.get("pid", 0)) or None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def write_lock_marker(lock_path: Path) -> None:
    """Write the current process's ``{pid, ts}`` marker to ``lock_path``."""
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "ts": time.time()}),
        encoding="utf-8",
    )


def remove_lock_marker(lock_path: Path) -> None:
    """Remove a marker file, tolerating concurrent removal / absence."""
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


@contextlib.contextmanager
def try_acquire_build_lock(
    lock_dir: Path, lock_filename: str = ".build.lock",
) -> Iterator[bool]:
    """Advisory cross-process build lock — non-blocking, take-or-skip.

    Writes a marker file at ``<lock_dir>/<lock_filename>`` carrying
    ``{pid, ts}``. The contract:

      - If the file is absent OR the previous holder's PID is dead,
        we take the lock and yield ``True``. The caller proceeds with
        the build and the marker is removed on exit.
      - If a live holder is detected, we yield ``False`` immediately
        (= no waiting, no embed-call duplication). The caller is
        expected to either fall back to whatever's already on disk or
        skip the build entirely and let the next attempt observe the
        finished state.

    Atomicity: uses ``O_CREAT | O_EXCL`` for the take so two processes
    racing the take produce exactly one winner. A subsequent stale-PID
    reap is also atomic (unlink + re-take).

    Filesystem-write errors fall through with ``False`` (= caller skips
    the build rather than crashing on a permission / quota issue).
    """
    lock_path = lock_dir / lock_filename
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield False
        return

    def _take_atomic() -> bool:
        try:
            fd = os.open(
                str(lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
        except FileExistsError:
            return False
        except OSError:
            return False
        try:
            os.write(
                fd,
                json.dumps({"pid": os.getpid(), "ts": time.time()}).encode(),
            )
        finally:
            os.close(fd)
        return True

    took = _take_atomic()
    if not took:
        # Existing lock — see if the holder is alive.
        holder_pid = read_lock_holder(lock_path)
        if holder_pid and pid_alive(holder_pid):
            yield False
            return
        # Stale lock — reap and retry exactly once.
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            yield False
            return
        if not _take_atomic():
            yield False
            return

    try:
        yield True
    finally:
        remove_lock_marker(lock_path)


__all__ = [
    "pid_alive",
    "read_lock_holder",
    "write_lock_marker",
    "remove_lock_marker",
    "try_acquire_build_lock",
]
