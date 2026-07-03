"""Tier 2: web_fetch emits a terminal web_fetch_failed event on failure (#2110).

A FAILED web_fetch previously emitted only ``web_fetch_started`` with no terminal
event — leaving the fetch stuck in a perpetual "started" state in the TUI events
tab. web_search already emits ``web_search_failed`` on failure; web_fetch now
mirrors that pattern.

Falsification: without the fix (the ``ctx.events.emit("web_fetch_failed", ...)``
calls in the two failure branches of ``handle_web_fetch``), the assertions below
that ``web_fetch_failed`` was emitted would go RED — only ``web_fetch_started``
would appear in the event log.

Real components:
- Real ``EventLog`` (public ``.all()`` API — not private state).
- Real ``OpContext`` wired with real ``PermissionResolver`` (permission_resolver=None
  skips the per-host gate; the handler reaches the httpx layer and the fake
  transport raises the expected exception).
- Real ``WebFetchIROp``.
- Fake HTTP transport: a real class that raises ``httpx.TimeoutException`` or
  ``httpx.RequestError`` — same technique as ``test_web_fetch_download_cap_1913.py``
  (``monkeypatch.setattr(httpx, "AsyncClient", ...)``, allowed per testing policy
  §3.4: "monkeypatch.setattr with a real callable").

No ``MagicMock``, ``AsyncMock``, or ``patch`` used.
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp


def _make_ctx(events: EventLog) -> Any:
    """Build a real OpContext wired with the given EventLog.

    permission_resolver=None skips the per-host permission gate so the
    test reaches the httpx transport layer (where the fake raises).
    """
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    return OpContext(
        workspace=type("W", (), {})(),  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
    )


class _TimeoutClient:
    """Real fake AsyncClient that raises httpx.TimeoutException on stream()."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_TimeoutClient":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def stream(self, method: str, url: str) -> "_TimeoutStreamCtx":
        return _TimeoutStreamCtx()


class _TimeoutStreamCtx:
    async def __aenter__(self) -> None:
        raise httpx.TimeoutException("simulated timeout")

    async def __aexit__(self, *a: object) -> None:
        return None


class _RequestErrorClient:
    """Real fake AsyncClient that raises httpx.RequestError on stream()."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_RequestErrorClient":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    def stream(self, method: str, url: str) -> "_RequestErrorStreamCtx":
        return _RequestErrorStreamCtx()


class _RequestErrorStreamCtx:
    async def __aenter__(self) -> None:
        raise httpx.RequestError("simulated connection refused")

    async def __aexit__(self, *a: object) -> None:
        return None


# ── Timeout path ──────────────────────────────────────────────────────────────


def test_failed_web_fetch_timeout_emits_web_fetch_failed(monkeypatch) -> None:
    """Tier 2: a timed-out web_fetch emits a terminal web_fetch_failed event.

    Falsification: without the fix, EventLog.all() would contain only
    ``web_fetch_started`` and NO ``web_fetch_failed`` — the assertion on
    the failed-event types list would be empty → RED.
    """
    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    events = EventLog()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(events)))

    # Return shape: status=timeout
    assert result["status"] == "timeout"
    assert result["kind"] == "web_fetch"

    emitted_types = [e.type for e in events.all()]

    # started MUST be present (regression guard)
    assert "web_fetch_started" in emitted_types

    # terminal failed event MUST be present — this is the new invariant
    failed_events = [e for e in events.all() if e.type == "web_fetch_failed"]
    assert failed_events, (
        "Expected a web_fetch_failed terminal event but none was emitted. "
        f"Emitted: {emitted_types}"
    )

    # Payload shape check: url and status fields must be present
    ev = failed_events[0]
    assert ev.data.get("url") == "https://example.com"
    assert ev.data.get("status") == "timeout"
    assert "timed out" in ev.data.get("error", "")


def test_failed_web_fetch_timeout_no_completed_event(monkeypatch) -> None:
    """Tier 2: a timed-out web_fetch does NOT emit web_fetch_completed.

    Completing and failing are mutually exclusive. Guards against a
    regression where both events fire.
    """
    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    events = EventLog()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(events)))

    emitted_types = [e.type for e in events.all()]
    assert "web_fetch_completed" not in emitted_types, (
        f"web_fetch_completed must not fire on timeout. Emitted: {emitted_types}"
    )


# ── RequestError path ─────────────────────────────────────────────────────────


def test_failed_web_fetch_request_error_emits_web_fetch_failed(monkeypatch) -> None:
    """Tier 2: a web_fetch that raises httpx.RequestError emits web_fetch_failed.

    Falsification: without the fix, only ``web_fetch_started`` would appear
    in the event log — the ``failed_events`` assertion would be empty → RED.
    """
    monkeypatch.setattr(httpx, "AsyncClient", _RequestErrorClient)

    events = EventLog()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    result = asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(events)))

    # Return shape: status=error
    assert result["status"] == "error"
    assert result["kind"] == "web_fetch"

    emitted_types = [e.type for e in events.all()]
    assert "web_fetch_started" in emitted_types

    failed_events = [e for e in events.all() if e.type == "web_fetch_failed"]
    assert failed_events, (
        "Expected a web_fetch_failed terminal event but none was emitted. "
        f"Emitted: {emitted_types}"
    )

    ev = failed_events[0]
    assert ev.data.get("url") == "https://example.com"
    assert ev.data.get("status") == "error"
    assert "connection refused" in ev.data.get("error", "")


def test_failed_web_fetch_request_error_no_completed_event(monkeypatch) -> None:
    """Tier 2: a web_fetch that raises RequestError does NOT emit web_fetch_completed."""
    monkeypatch.setattr(httpx, "AsyncClient", _RequestErrorClient)

    events = EventLog()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(events)))

    emitted_types = [e.type for e in events.all()]
    assert "web_fetch_completed" not in emitted_types, (
        f"web_fetch_completed must not fire on RequestError. Emitted: {emitted_types}"
    )


# ── Parity check: mirror web_search's pattern ─────────────────────────────────


def test_web_fetch_started_precedes_web_fetch_failed(monkeypatch) -> None:
    """Tier 2: web_fetch_started is emitted BEFORE web_fetch_failed (ordering).

    Mirrors web_search's invariant where ``web_search_started`` always
    precedes ``web_search_failed``.
    """
    monkeypatch.setattr(httpx, "AsyncClient", _TimeoutClient)

    events = EventLog()
    op = WebFetchIROp(kind="web_fetch", url="https://example.com")
    asyncio.run(handle_web_fetch(op=op, ctx=_make_ctx(events)))

    all_events = events.all()
    types = [e.type for e in all_events]

    assert "web_fetch_started" in types
    assert "web_fetch_failed" in types

    started_idx = types.index("web_fetch_started")
    failed_idx = types.index("web_fetch_failed")
    assert started_idx < failed_idx, (
        f"web_fetch_started ({started_idx}) must precede web_fetch_failed ({failed_idx})"
    )
