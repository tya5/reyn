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
entrypoints (`reyn chat`, `reyn web`, `reyn cron run`, `reyn dogfood`, the
chainlit server) and not all of them have a single active session at the
point an unhandled exception surfaces (the web server and chainlit app can
have zero-to-many concurrently attached sessions sharing one loop) -- a
uniform, session-independent sink avoids having to special-case each
entrypoint's session lifecycle just to find "the" EventLog to write to.

Call this once per real loop-owning entrypoint, right after the loop is
obtained/created and before the main work starts. Calling it more than once
on the same loop is harmless (``loop.set_exception_handler`` just overwrites
with an equivalent handler).
"""
from __future__ import annotations

import asyncio
import traceback
from typing import Any

_EVENT_KIND = "asyncio_unhandled_exception"


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Install the durable-capture asyncio exception handler on *loop*.

    *loop* must already be the running loop of the entrypoint that owns it
    (e.g. obtained via ``asyncio.get_running_loop()`` from inside the
    entrypoint's top-level coroutine, or the loop just created by
    ``asyncio.new_event_loop()``).
    """
    loop.set_exception_handler(_make_handler())


def _make_handler():
    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        # ALWAYS defer to the loop's own default handler first -- this
        # handler only ADDS durable capture, it never replaces or suppresses
        # the existing stderr/logging behavior an operator already relies on.
        loop.default_exception_handler(context)
        _durably_capture(context)

    return _handler


def _durably_capture(context: dict[str, Any]) -> None:
    # Local import: keeps this module import-cheap for entrypoints that
    # install the handler before the rest of reyn's config/event machinery
    # is set up, and avoids import-cycle risk (events.py is a low-level
    # module several higher layers import).
    from reyn.core.events.events import emit_cli_event

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
        emit_cli_event(
            _EVENT_KIND,
            exception_type=exception_type,
            exception_message=exception_message,
            traceback=tb,
            context_message=context.get("message", ""),
            task_repr=repr(task) if task is not None else "",
        )
    except Exception:  # noqa: BLE001 -- durable-capture must never crash the loop
        pass
