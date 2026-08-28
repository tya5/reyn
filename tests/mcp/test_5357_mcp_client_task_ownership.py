"""Tests for #5357 — MCPClient's open/close task-identity discipline.

Chronic real-world ``RuntimeError: Attempted to exit cancel scope in a
different task than it was entered in`` (60/72 occurrences over 15 days in
reyn-self's own logs — always the same traceback shape: an anyio task-group
``__aexit__``, inside Python's async-generator GC finalizer closing
``streamable_http_client``, ``task_repr`` always EMPTY). Architect's root
cause: nobody explicitly closed the streamable-http client's context manager;
when the reference dropped, the EVENT LOOP's own async-generator finalizer
closed it in NO task at all (not literally "a different task"), and anyio
correctly complained.

Real instances only, per the testing policy: no ``mock.patch`` / ``MagicMock``
on the transport, session, or asyncio task machinery — the whole defect is
about REAL task identity, so these tests build a real local HTTP MCP server
(reusing ``test_mcp_client.py``'s own ``_HttpEchoServer`` — a real uvicorn
server, not a fake) and drive real ``asyncio`` tasks against it.

A note on HOW witness #1's "drop the reference" is driven: the abandoned
client and its owner task hold a genuine reference CYCLE (the owner task's
own coroutine closure holds the client), so an organic ``del client;
gc.collect()`` depends on CPython's cyclic-collector *timing* to actually
reclaim it — measured directly against this exact fix: sometimes one
``gc.collect()`` call was enough, sometimes it took much longer under a full
pytest process, with no bound. That is exactly the "a duration/timing detail
stands in for an observation nobody exposed" hazard the testing policy
warns about (never pin third-party GC timing). So these tests instead drive
the abandonment DETERMINISTICALLY — cancelling the client's own owner task
directly (``MCPClient`` exposes no public "abandon me" hook; per the testing
policy's own carve-out, that absence is itself the finding, not a reason to
wait on GC) — which exercises the identical code path
(``_own_lifecycle``'s ``except asyncio.CancelledError`` branch) that organic
GC-driven abandonment would hit, without depending on when or whether the
cyclic collector gets to it.
"""
from __future__ import annotations

import asyncio

from reyn.mcp.client import MCPClient
from tests.mcp.test_mcp_client import _HttpEchoServer


def test_abandoned_owner_task_closes_in_same_task_and_reports_leak() -> None:
    """Tier 1: #5357 witnesses 1+2 — a streamable-http ``MCPClient`` whose owner
    task is torn down WITHOUT ``close()``/``__aexit__`` ever being called (the
    "reference just gets dropped" shape architect specified — see the module
    docstring for why this drives it via a direct cancel rather than organic GC)
    used to crash with ``RuntimeError: Attempted to exit cancel scope in a
    different task than it was entered in`` pre-#5357 (reproduced directly
    against this real transport before this fix landed — see the PR
    description). ``initialize()`` now pins the open and the eventual close to
    ONE dedicated owner task for the client's whole lifetime, so tearing that
    task down without a close request still closes in the SAME task — no
    exception reaches the loop's exception handler — and the abandoned close
    (nobody ever asked for it) is reported as exactly one
    ``mcp_client_close_leaked`` audit-event (the leak is real: the connection
    HAD to be torn down without a close request; only the crash is gone)."""
    errors: list[dict] = []
    leaked_events: list[dict] = []

    async def _run_it() -> None:
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda loop, context: errors.append(context))

        def _emit(kind: str, **fields: object) -> None:
            if kind == "mcp_client_close_leaked":
                leaked_events.append(fields)

        async with _HttpEchoServer() as server:
            cfg = {"type": "streamable-http", "url": server.url}
            client = MCPClient(cfg, emit_event=_emit, server_name="leak-probe")
            await client.__aenter__()
            owner_task = client._owner_task
            assert owner_task is not None
            # Deterministically drive the exact "abandoned, close never
            # requested" condition this fix closes — see the module docstring
            # for why this replaces waiting on organic cyclic GC.
            owner_task.cancel()
            await owner_task

    asyncio.run(_run_it())

    assert errors == [], (
        f"the abandoned owner task crossed a task boundary: {errors}"
    )
    # Exactly one leak event, carrying this client's own identity — not merely
    # "at least one" (a leak detector that double-reports is itself a bug) and
    # not merely a count (the payload identifies the abandoned connection).
    assert leaked_events == [{"server": "leak-probe", "transport": "streamable-http"}]


def test_normal_close_never_reports_a_leak() -> None:
    """Tier 1: #5357 witness 3 — the noise guard. A client that goes through the
    normal ``async with MCPClient(...) as client:`` open/close path — the SAME
    owner-task machinery witness 1's test exercises — never fires
    ``mcp_client_close_leaked``. A leak detector that fires on every ordinary close
    would be useless."""
    leaked_events: list[dict] = []

    def _emit(kind: str, **fields: object) -> None:
        if kind == "mcp_client_close_leaked":
            leaked_events.append(fields)

    async def _run_it() -> None:
        async with _HttpEchoServer() as server:
            cfg = {"type": "streamable-http", "url": server.url}
            async with MCPClient(cfg, emit_event=_emit, server_name="normal-probe") as client:
                result = await client.call_tool("echo", {"text": "hi"})
                assert result["isError"] is False

    asyncio.run(_run_it())

    assert leaked_events == []


def test_close_from_a_different_task_than_initialize_still_succeeds() -> None:
    """Tier 1: #5357 general-form check — ``initialize()`` in one asyncio Task and
    ``close()`` from a DIFFERENT one (the exact shape ``connection_service.py``'s
    ``_ensure_open``/``_reconnect`` split across separate calls) must not raise: the
    actual anyio-sensitive teardown now always runs in the client's own owner task,
    never in whichever task happens to call ``close()``."""

    async def _run_it() -> None:
        async with _HttpEchoServer() as server:
            cfg = {"type": "streamable-http", "url": server.url}
            client = MCPClient(cfg, server_name="cross-task-probe")

            async def _open() -> None:
                await client.__aenter__()

            async def _close() -> None:
                await client.__aexit__(None, None, None)

            await asyncio.create_task(_open())
            assert client.is_initialized() is True
            await asyncio.create_task(_close())
            assert client.is_initialized() is False

    asyncio.run(_run_it())
