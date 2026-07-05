"""reyn.runtime.fs_watcher — filesystem watcher external-event source (#2608 H4).

The 4th external-event source in the external-event->hooks arc (after H1's MCP
``resources/updated`` bridge). Mirrors H1's shape — a source fires
``HookDispatcher.dispatch(point, template_vars)`` for its events, here
``point="file_changed"`` — but with one load-bearing difference: the producer
runs on a SEPARATE THREAD, not the session's asyncio task.

Uses the third-party ``watchdog`` library (extras-only — ``pip install
reyn[fs-watch]``; see ``pyproject.toml``'s ``fs-watch`` extra) to run an OS-level
filesystem observer. ``watchdog.observers.Observer`` owns a dedicated OS thread;
every event callback (``on_created``/``on_modified``/``on_deleted``) fires
ON THAT THREAD. It is therefore UNSAFE to touch an ``asyncio.Queue`` from the
callback directly (``Queue.put_nowait`` is not thread-safe — no lock protects
its internal deque against a concurrent ``get()`` on the loop thread). The
thread->async handoff instead goes through ``loop.call_soon_threadsafe(...)``
(``loop`` is captured, on the SESSION's own event loop, at :meth:`FsWatcher.start`
time): the watchdog-thread callback schedules a plain callable onto the loop,
which is the ONLY thing safe to call from a foreign thread per the asyncio
docs. That scheduled callable does the actual (loop-thread-only) bounded
``Queue.put_nowait`` — mirroring H1's ``MCPConnectionService.enqueue_external_event``
bound+drop+log discipline (``_QUEUE_MAXSIZE``, overflow drops the newest event
+ logs, never grows unboundedly, never blocks the watchdog thread).

Path normalization (#2623): on macOS ``/tmp`` is a symlink to ``/private/tmp``
(same class of footgun exists anywhere an operator's configured
``fs_watch.paths`` entry traverses a symlink). ``watchdog``'s OS backend
(fsevents on macOS) reports the RESOLVED path in every fired event regardless
of which path string ``Observer.schedule`` was given — so a naive matcher
written against the operator's CONFIGURED path (e.g. ``matcher: {path:
'/tmp/x/**'}``) silently never matches an event path of
``/private/tmp/x/...``. Fixed at registration (:meth:`FsWatcher.start`): for
each configured path whose ``os.path.realpath`` differs from its own
(normalized) form, a resolved-path -> configured-path REWRITE is recorded
(:attr:`_path_rewrites`). Every fired event's path is rewritten back onto the
operator's configured prefix (:meth:`_rewrite_path`) before it ever reaches
``hook_trigger`` — so ``matcher: {path: <the path the operator wrote in
fs_watch.paths>}`` Just Works, and the ``file_changed`` event's ``path``
template var is exactly the configured path (not a resolved alias of it) for
any file under it. A path with no symlink component is unaffected
(rewrite is a no-op — ``resolved == configured`` and no entry is added).

Debounce (F7-3): editors emit event BURSTS for one logical change (temp files,
multiple writes, create-then-modify). Coalesced per-path on the watchdog
thread with a simple leading-edge scheme: :meth:`_FsEventHandler._maybe_fire`
tracks the last-fired monotonic time per path; an event within
``debounce_seconds`` of the previous fire for the SAME path is dropped
(coalesced), so one write-burst = one hook fire. A quiet path that fires again
after the window elapses is a NEW logical change and fires again. Per-path
state only — a burst on path A never suppresses path B.

SECURITY (F7-5, do not relitigate): watched paths are OPERATOR-DECLARED via
``fs_watch.paths`` in ``reyn.yaml``/``reyn.local.yaml`` (see
``reyn.config.infra.FsWatchConfig`` — OUT-set only, restart-only, never a
``.reyn/*.yaml`` hot-reload file). There is no op/tool verb anywhere that lets
an agent register or widen a watch — a filesystem-wide change-notification
feed is an info-gathering surface, same class of concern as sandbox policy,
so it gets the same OUT-set-only gate. :class:`FsWatcher` itself has no
"add a path" method; its watched set is FIXED at construction from the
config the session was started with.

Session-owned lifecycle (mirrors ``MCPConnectionService``): constructed
unconditionally by ``Session.__init__`` (cheap — the watchdog ``Observer`` is
only created inside :meth:`start`), started from ``Session.run()`` right after
the ``session_start`` hook dispatch (only if ``fs_watch.paths`` is non-empty),
and stopped in ``run()``'s ``finally`` alongside the ``session_end`` hook via
:meth:`aclose` (idempotent, ``finally``-guaranteed so a ``CancelledError``
during teardown can never orphan the drain task or the observer thread — see
:meth:`aclose`).

Watchdog-optional (graceful degrade): ``fs_watch.paths`` configured but the
``watchdog`` package not installed -> :meth:`start` logs a warning and returns
without starting anything (the feature is off; the rest of the session is
unaffected). ``fs_watch.paths`` empty (the default, no config) -> :meth:`start`
returns immediately without even importing ``watchdog`` — byte-identical to
pre-H4 behaviour for every existing build.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Bound on the thread->async bridge queue — mirrors H1's
# ``_HOOK_EVENT_QUEUE_MAXSIZE`` (MCPConnectionService). A burst of fs events
# beyond this is dropped (+logged), never queued unboundedly, never
# backpressured onto the watchdog thread.
_QUEUE_MAXSIZE = 32

HookTrigger = Callable[[str, dict], Awaitable[Any]]

# watchdog event class name -> our normalized event_type vocabulary.
_EVENT_TYPE_BY_WATCHDOG_ATTR = {
    "on_created": "created",
    "on_modified": "modified",
    "on_deleted": "deleted",
}


def _import_watchdog() -> Any:
    """Import and return the ``watchdog.events``/``watchdog.observers`` modules
    as a ``(events_mod, observers_mod)`` tuple, or ``None`` if ``watchdog`` is
    not installed. Isolated behind a function so :meth:`FsWatcher.start` can
    degrade gracefully (warn + no-op) rather than raising ``ImportError`` at
    session start."""
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None
    return FileSystemEventHandler, Observer


class FsWatcher:
    """Session-owned filesystem watcher — see module docstring for the full
    thread->async bridge, debounce, and security design.

    Usage (mirrors ``MCPConnectionService``)::

        watcher = FsWatcher(paths=["/repo/src"], hook_trigger=dispatcher.dispatch)
        await watcher.start()      # no-op if paths=[] or watchdog not installed
        ...
        await watcher.aclose()     # session teardown — idempotent
    """

    def __init__(
        self,
        *,
        paths: "list[str] | None" = None,
        hook_trigger: "HookTrigger | None" = None,
        debounce_seconds: float = 0.2,
    ) -> None:
        self._paths: list[str] = list(paths or [])
        self._hook_trigger = hook_trigger
        self._debounce_seconds = debounce_seconds
        self._observer: Any = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        self._queue: "asyncio.Queue[tuple[str, str]] | None" = None
        self._drain_task: "asyncio.Task | None" = None
        self._started = False
        # #2623: resolved-symlink-path -> operator-configured-path rewrites,
        # built in :meth:`start` — see module docstring "Path normalization".
        self._path_rewrites: "list[tuple[str, str]]" = []

    def is_started(self) -> bool:
        """Read-only introspection for callers/tests — mirrors
        ``MCPConnectionService.held_servers()``'s public-surface pattern."""
        return self._started

    async def start(self) -> None:
        """Start the watchdog observer + the loop-side drain task.

        No-op (returns immediately) when:
          - ``paths`` is empty (no ``fs_watch:`` config — the default), or
          - ``hook_trigger`` is None, or
          - ``watchdog`` is not installed (logs a warning once).

        Idempotent — a second call while already started is a no-op.
        """
        if self._started:
            return
        if not self._paths or self._hook_trigger is None:
            return
        imported = _import_watchdog()
        if imported is None:
            logger.warning(
                "FsWatcher: fs_watch.paths configured (%d path(s)) but the "
                "'watchdog' package is not installed — filesystem watching is "
                "DISABLED for this session. Install with `pip install "
                "reyn[fs-watch]` to enable file_changed hooks.",
                len(self._paths),
            )
            return
        FileSystemEventHandler, Observer = imported

        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._drain_task = asyncio.create_task(self._drain_events())

        # #2623: build the resolved->configured path rewrite table BEFORE
        # scheduling — the OS backend (fsevents on macOS, symlink-transparent
        # on Linux too) reports events at the REALPATH regardless of which
        # path string we hand ``observer.schedule``, so every reported event
        # path needs rewriting back onto what the operator actually wrote in
        # ``fs_watch.paths`` for a ``matcher: {path: <configured>}`` to match.
        self._path_rewrites = []
        for configured in self._paths:
            resolved = os.path.realpath(configured)
            normalized_configured = os.path.normpath(configured)
            if resolved != normalized_configured:
                self._path_rewrites.append((resolved, normalized_configured))

        handler = _build_handler(FileSystemEventHandler, self)
        observer = Observer()
        for path in self._paths:
            observer.schedule(handler, path, recursive=True)
        observer.start()
        self._observer = observer
        self._started = True

    def _rewrite_path(self, path: str) -> str:
        """#2623: rewrite a fired event's (possibly symlink-resolved) path back
        onto the operator's configured ``fs_watch.paths`` prefix, so
        ``matcher: {path: <configured>}`` matches regardless of a
        macOS-``/tmp``-style symlink in the watched path. A path with no
        matching resolved-prefix (no symlink was involved) passes through
        unchanged."""
        for resolved, configured in self._path_rewrites:
            if path == resolved:
                return configured
            if path.startswith(resolved + os.sep):
                return configured + path[len(resolved):]
        return path

    # ── thread->async bridge (called from the watchdog OS thread) ──────────

    def _on_fs_event(self, event_type: str, path: str) -> None:
        """Called synchronously ON THE WATCHDOG THREAD by ``_FsEventHandler``.
        Never touches ``asyncio`` state directly — schedules
        :meth:`_enqueue` onto the session's event loop via
        ``call_soon_threadsafe``, the one thread-safe entry point asyncio
        exposes for exactly this cross-thread handoff."""
        loop = self._loop
        if loop is None:
            return  # stopped/never started — defensive, should not happen mid-callback
        # #2623: rewrite the (possibly symlink-resolved) path back onto the
        # operator's configured prefix BEFORE it ever reaches hook_trigger —
        # _path_rewrites is built once in start() (loop thread) before the
        # observer thread is started, so this read-only lookup from the
        # watchdog thread is race-free (no further writes after start()).
        path = self._rewrite_path(path)
        try:
            loop.call_soon_threadsafe(self._enqueue, event_type, path)
        except RuntimeError:
            # Loop already closed (session tearing down concurrently with a
            # trailing fs event) — drop, never raise from the watchdog thread.
            logger.warning(
                "FsWatcher: dropped %r event for %r — event loop already closed",
                event_type, path,
            )

    def _enqueue(self, event_type: str, path: str) -> None:
        """Runs ON THE LOOP THREAD (scheduled via call_soon_threadsafe) — safe
        to touch ``self._queue`` here."""
        if self._queue is None:
            return
        try:
            self._queue.put_nowait((event_type, path))
        except asyncio.QueueFull:
            # Bounded by construction (mirrors H1): a burst faster than hooks
            # can be dispatched drops the newest event rather than growing the
            # queue unboundedly.
            logger.warning(
                "FsWatcher: file_changed hook queue full (maxsize=%d) — "
                "dropping %r event (path=%r)",
                _QUEUE_MAXSIZE, event_type, path,
            )

    async def _drain_events(self) -> None:
        """Runs on the session's event loop; the only place that ``await``s
        ``hook_trigger``. Per-event ``try/except`` mirrors H1's drain task —
        one bad dispatch must not kill the drain loop."""
        assert self._queue is not None
        assert self._hook_trigger is not None
        while True:
            event_type, path = await self._queue.get()
            try:
                await self._hook_trigger(
                    "file_changed",
                    {"point": "file_changed", "path": path, "event_type": event_type},
                )
            except Exception:  # noqa: BLE001 — one bad dispatch must not kill the drain task
                logger.warning(
                    "FsWatcher: hook_trigger failed for path=%r event_type=%r",
                    path, event_type, exc_info=True,
                )

    async def aclose(self) -> None:
        """Stop the observer thread + cancel the drain task. Idempotent — safe
        to call repeatedly (e.g. a session teardown seam that may run more
        than once) and safe to call even if :meth:`start` was never called or
        no-op'd (empty paths / no watchdog)."""
        try:
            if self._drain_task is not None and not self._drain_task.done():
                self._drain_task.cancel()
                try:
                    await self._drain_task
                except asyncio.CancelledError:
                    pass
        finally:
            self._drain_task = None

        observer = self._observer
        self._observer = None
        if observer is not None:
            # observer.stop() + join() are synchronous/blocking (thread join) —
            # run off the loop thread so a slow-to-exit watchdog thread never
            # stalls the session's event loop during teardown.
            loop = asyncio.get_running_loop()

            def _stop_and_join() -> None:
                observer.stop()
                observer.join(timeout=5.0)

            await loop.run_in_executor(None, _stop_and_join)
        self._started = False
        self._loop = None
        self._queue = None


def _build_handler(file_system_event_handler_cls: Any, watcher: FsWatcher) -> Any:
    """Build a ``watchdog.events.FileSystemEventHandler`` subclass instance
    bound to ``watcher``. Factored into a function (rather than a module-level
    class) because ``FileSystemEventHandler`` only exists when ``watchdog`` is
    importable — a module-level subclass would make the whole module
    import-fail without ``watchdog`` installed, defeating the graceful-degrade
    contract :meth:`FsWatcher.start` promises."""

    class _FsEventHandler(file_system_event_handler_cls):  # type: ignore[misc,valid-type]
        def __init__(self) -> None:
            super().__init__()
            # #2608 H4 debounce (F7-3): per-path last-fire monotonic time,
            # touched ONLY on the watchdog thread (this handler's callbacks all
            # run there) — no lock needed, single-writer/single-reader on one
            # thread.
            self._last_fire: dict[str, float] = {}

        def _maybe_fire(self, event_type: str, src_path: str) -> None:
            now = time.monotonic()
            last = self._last_fire.get(src_path)
            if last is not None and (now - last) < watcher._debounce_seconds:
                return  # coalesced: part of the same logical-change burst
            self._last_fire[src_path] = now
            watcher._on_fs_event(event_type, src_path)

        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                self._maybe_fire("created", str(event.src_path))

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                self._maybe_fire("modified", str(event.src_path))

        def on_deleted(self, event: Any) -> None:
            if not event.is_directory:
                self._maybe_fire("deleted", str(event.src_path))

    return _FsEventHandler()
