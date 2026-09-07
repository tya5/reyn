"""Global asyncio unhandled-exception -> durable P6 event capture.

reyn installs no custom asyncio exception handler anywhere by default. That
means a fire-and-forget background task (``asyncio.create_task(...)`` /
``asyncio.ensure_future(...)`` whose result nobody awaits or checks) that
raises is caught ONLY by Python's own
``asyncio.BaseEventLoop.default_exception_handler`` -- which logs
"Unhandled exception in event loop" (sometimes with ``exception: None`` when
asyncio only has a *message*, no exception object) to stderr/logging and is
then GONE. Nothing durable survives past that point, so an operator who
notices the message days later has no way to investigate after the fact.

``install_asyncio_exception_handler`` closes that gap: it installs a handler
on the given (already-running) loop that ALWAYS defers to the loop's own
default handler first (byte-identical existing stderr/log visibility), then
durably emits an ``asyncio_unhandled_exception`` P6 event via
``emit_cli_event`` -- the existing session-independent "no active Session in
this process" durable-emit path (routes to
``.reyn/events/direct/cli/<date>.jsonl``, found by walking up from
``Path.cwd()``). Using ``emit_cli_event`` rather than a per-session
``EventLog`` is a deliberate choice: reyn has several distinct loop-owning
entrypoints (`reyn chat`, `reyn web`, `reyn cron run`, `reyn dogfood`) and
not all of them have a single active session at the point an unhandled
exception surfaces (the web server can have zero-to-many concurrently
attached sessions sharing one loop) -- a
uniform, session-independent sink avoids having to special-case each
entrypoint's session lifecycle just to find "the" EventLog to write to.

Call this once per real loop-owning entrypoint, right after the loop is
obtained/created and before the main work starts. Calling it more than once
on the same loop is harmless (``loop.set_exception_handler`` just overwrites
with an equivalent handler).
"""
from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from typing import Any, Callable

_EVENT_KIND = "asyncio_unhandled_exception"

# #5951 (P0, owner-hit): resolved ONCE, on first successful use, and cached
# here -- never re-imported on every call. The exception handler this backs
# fires from asyncio's OWN teardown machinery (`Task was destroyed but it is
# pending!` -- exactly the shutdown-only event this module exists to durably
# capture), which can run AFTER `sys.meta_path` has been set to `None`
# (CPython's own interpreter-shutdown signal) -- at that point ANY fresh
# `import` unconditionally raises `ImportError: sys.meta_path is None,
# Python is likely shutting down`, regardless of whether the target module
# was already importable seconds earlier. A function-local import (the
# previous shape) re-attempted this EVERY call, so a process that had
# emitted successfully a hundred times could still hit this on the
# hundred-and-first call, specifically the shutdown one -- the exact call
# this module's whole docstring says must not fail. Caching the FIRST
# success means every later call (shutdown included) reuses the already-
# bound reference and touches `sys.meta_path` not at all.
_emit_cli_event: "Callable[..., None] | None" = None


def _resolve_emit_cli_event() -> "Callable[..., None] | None":
    """Returns the cached ``emit_cli_event`` once resolved; on the FIRST
    call, attempts the import once and caches success. If that first
    attempt itself lands during shutdown (a process that raised its very
    first unhandled exception AFTER `sys.meta_path` went `None` -- no prior
    successful emit to have cached), the ``ImportError`` is swallowed here
    and every call returns ``None`` for the rest of the process: this
    diagnostic gives up quietly rather than crash the loop or bury the
    original exception context in its OWN traceback (the failure mode
    #5951 reports)."""
    global _emit_cli_event
    if _emit_cli_event is not None:
        return _emit_cli_event
    try:
        from reyn.core.events.events import emit_cli_event
    except ImportError:
        return None
    _emit_cli_event = emit_cli_event
    return _emit_cli_event


# #5951: `_surface_while_app_running`'s two prompt_toolkit imports were
# already exception-guarded (never the bug this issue reports), but the
# SAME shape -- a function-local import re-attempted on every call, when a
# process that resolved it once will keep resolving it the same way for
# the rest of its life -- applies equally: cache each on first success, the
# same as `_resolve_emit_cli_event` above (lead-coder review note, #5951).
_get_app_or_none: "Callable[[], object | None] | None" = None
_run_in_terminal: "Callable[..., Any] | None" = None


def _resolve_get_app_or_none() -> "Callable[[], object | None] | None":
    """Cached ``prompt_toolkit.application.current.get_app_or_none``, or
    ``None`` once-and-for-all for a process where prompt_toolkit is not
    installed / not importable (optional dependency of this diagnostic
    path) or the import landed during shutdown with nothing cached yet."""
    global _get_app_or_none
    if _get_app_or_none is not None:
        return _get_app_or_none
    try:
        from prompt_toolkit.application.current import get_app_or_none
    except Exception:  # noqa: BLE001 -- optional at import time, never fatal
        return None
    _get_app_or_none = get_app_or_none
    return _get_app_or_none


def _resolve_run_in_terminal() -> "Callable[..., Any] | None":
    """Cached ``prompt_toolkit.application.run_in_terminal``, same shape as
    :func:`_resolve_get_app_or_none` above."""
    global _run_in_terminal
    if _run_in_terminal is not None:
        return _run_in_terminal
    try:
        from prompt_toolkit.application import run_in_terminal
    except Exception:  # noqa: BLE001 -- optional at import time, never fatal
        return None
    _run_in_terminal = run_in_terminal
    return _run_in_terminal


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Install the durable-capture asyncio exception handler on *loop*.

    *loop* must already be the running loop of the entrypoint that owns it
    (e.g. obtained via ``asyncio.get_running_loop()`` from inside the
    entrypoint's top-level coroutine, or the loop just created by
    ``asyncio.new_event_loop()``).

    #5952 BLOCKING (lead-coder, measured): resolves ``emit_cli_event`` HERE,
    once, at install time -- not left to warm on the handler's own first
    call. Measured: ``_resolve_emit_cli_event`` was reachable ONLY from
    inside the handler itself, so a process whose FIRST-EVER unhandled
    exception is a genuinely shutdown-only one (``Task was destroyed but
    it is pending!`` -- #5951's own reported symptom, fired from asyncio's
    OWN teardown machinery, never from ordinary running code) would still
    lose it: nothing earlier ever warmed the cache, so the handler's first
    (and only) call resolves for the first time AFTER `sys.meta_path` is
    already `None`, hits the exact `ImportError` #5951 reports, and gives
    up quietly -- #5951's own event, unrecorded, the fix's whole point
    defeated for the single-failure process it was written for. Installing
    the handler happens far ahead of any shutdown (this function's own
    docstring: called once per real loop-owning entrypoint, right after
    the loop is obtained, before the main work starts), so warming here
    means the cache is already populated before ANY exception -- shutdown
    or not -- can ever reach the handler. Grep-confirmed no import cycle
    (`events.py` does not import `asyncio_diagnostics`; both import
    cleanly together) -- the original deferred-import's real benefit is
    only "an entrypoint that never installs this handler pays nothing",
    which a call here (only reached BY an installer) still preserves.
    """
    _resolve_emit_cli_event()
    loop.set_exception_handler(_make_handler())


def _make_handler():
    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # ALWAYS defer to the loop's own default handler first -- this
        # handler only ADDS durable capture, it never replaces or suppresses
        # the existing stderr/logging behavior an operator already relies on.
        loop.default_exception_handler(context)
        _durably_capture(context)
        _surface_while_app_running(context)

    return _handler


def _durably_capture(context: dict[str, Any]) -> None:
    # #5951: resolved via the cached, once-only import above -- never a
    # fresh function-local import on every call (see _resolve_emit_cli_event's
    # own docstring for why that shape was the bug). `emit` is `None` only
    # when this process has never once successfully resolved it (including
    # "the very first call landed during shutdown") -- give up quietly
    # rather than let an ImportError propagate and bury the diagnostic this
    # function exists to preserve.
    emit = _resolve_emit_cli_event()
    if emit is None:
        return

    exc = context.get("exception")
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        exception_type = type(exc).__name__
        exception_message = str(exc)
    else:
        # asyncio sometimes reports a message-only context (no exception
        # object) -- this is the exact class an operator sees as
        # "exception: None" and cannot investigate after the fact. Still
        # durably captured: context_message is always present.
        tb = ""
        exception_type = ""
        exception_message = ""
    task = context.get("task") or context.get("future")

    try:
        emit(
            _EVENT_KIND,
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=tb,
            context_message=context.get("message", ""),
            task_repr=repr(task) if task is not None else "",
        )
    except Exception:  # noqa: BLE001 -- durable-capture must never crash the loop
        pass


def _asyncio_log_reaches_console() -> bool:
    """Whether an ERROR record on the ``asyncio`` logger reaches the console.

    ``loop.default_exception_handler`` logs via the ``asyncio`` logger. If the
    effective handler chain includes a ``StreamHandler`` pointed at the real
    console (``sys.stdout`` / ``sys.stderr``) -- or NO handler at all, in which
    case ``logging``'s ``lastResort`` handler writes to stderr -- the message
    is already on screen. Only when the interactive-CUI redirect
    (``_setup_interactive_logging`` in interfaces/cli/commands/chat.py) has
    replaced those with a ``FileHandler`` does no console handler remain.

    Walks the logger→parent chain honoring ``propagate``, mirroring
    ``logging.Logger.callHandlers``. ``FileHandler`` is a ``StreamHandler``
    subclass, so it is excluded explicitly.
    """
    logger: logging.Logger | None = logging.getLogger("asyncio")
    saw_handler = False
    while logger is not None:
        for handler in logger.handlers:
            saw_handler = True
            if isinstance(handler, logging.StreamHandler) and not isinstance(
                handler, logging.FileHandler
            ):
                if getattr(handler, "stream", None) in (sys.stdout, sys.stderr):
                    return True
        if not logger.propagate:
            break
        logger = logger.parent
    # No handler anywhere in the chain → logging.lastResort (stderr) fires.
    return not saw_handler


def _surface_while_app_running(context: dict[str, Any]) -> None:
    """Print the context's message on screen while a prompt_toolkit
    Application owns the terminal AND the ``asyncio`` logger's output has
    been redirected away from the console (#2786 polish).

    ``loop.default_exception_handler`` (called before this, in ``_handler``)
    already logs ``context["message"]`` via the ``asyncio`` logger --
    including the message-only case (no ``exception`` key) this module's
    docstring describes, where the message is the ONLY diagnostic available.
    That log line reaches the console for every entrypoint EXCEPT reyn's own
    interactive chat CUI: `_setup_interactive_logging`
    (interfaces/cli/commands/chat.py) redirects the root logger to
    `.reyn/logs/reyn.log` for the whole duration of that session, so the
    message would otherwise never reach the screen there -- reproducing
    exactly the "Exception None" blank-diagnostics symptom #2786 reports,
    even after the loop's exception handler is no longer masked.

    The ``_asyncio_log_reaches_console()`` guard is what keeps this from
    DOUBLE-printing on paths where the default handler already reached the
    console -- e.g. the ``--cui`` PromptSession path (a prompt_toolkit
    Application IS running there, but logging is NOT redirected, so stderr
    already showed the message). Surfacing there would print it twice.

    A bare ``print`` would also corrupt whichever prompt_toolkit
    Application currently owns the terminal (the inline CUI's rule-bar
    Application), so this goes through ``run_in_terminal`` -- the same
    mechanism prompt_toolkit's own ``Application._handle_exception`` and
    reyn's REPL output loop (``interfaces/repl/repl.py``) already use to
    interleave ad-hoc output with a live render.

    No-op when no Application is running (headless entrypoints -- web
    server, cron, dogfood -- are unaffected; their unredirected
    logging already surfaces the message via the call above).
    """
    get_app_or_none = _resolve_get_app_or_none()
    if get_app_or_none is None:
        return
    if get_app_or_none() is None:
        return
    if _asyncio_log_reaches_console():
        # The default handler already put the message on screen; a second
        # print via run_in_terminal would double it. Only surface when the
        # log has been redirected off-console (interactive-CUI session).
        return

    exc = context.get("exception")
    message = context.get("message") or "Unhandled exception in event loop"
    tb_text = (
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        if exc is not None else ""
    )

    def _emit() -> None:
        print(f"\nUnhandled exception in event loop: {message}")
        if tb_text:
            print(tb_text)

    run_in_terminal = _resolve_run_in_terminal()
    if run_in_terminal is None:
        return
    try:
        run_in_terminal(_emit)
    except Exception:  # noqa: BLE001 -- surfacing must never crash the loop
        pass
