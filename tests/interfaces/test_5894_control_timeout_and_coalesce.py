"""Tier 2: #5894 ①-1 / ①-3 — a control POST is a BOUNDED round-trip
(its own timeout, separate from the SSE stream's), and a pending
``cancel_inflight`` coalesces a second one.

Owner-hit: the remote server sat in CPU-bound LLM request assembly and
answered nothing; the client's control POSTs (Ctrl-C's ``cancel_inflight``
among them) went through the SAME ``httpx`` client as the SSE stream, whose
``read=None`` is right for a live stream and wrong for a round-trip — so
the cancel waited forever, and the TUI's pump waited with it (the pump half
is ``test_5894_pump_never_awaits_wire.py``).

Architect ruling ①-1: T is ONE constant in ONE place (``remote_client.
_CONTROL_TIMEOUT_S``) and the stream keeps ``read=None``. ``post_control``
takes T as a parameter, so here T is an INPUT this test supplies — the
duration is the subject, not a wait the test sits out. The server on the
other end is a real TCP listener that accepts and never answers; nothing
about it is faked or timed.

Ruling ①-3: while one ``cancel_inflight`` POST is pending, a second call
is a no-op ("cancel already requested") and sends nothing — once the pump
no longer awaits the round-trip, a held Ctrl-C would otherwise open one
POST per key repeat against a server that is, by hypothesis, not
answering. And a non-delivery (``send`` returned ``None``) reads back as
the EMPTY summary the ABC reserves for it, never as "cancel requested".
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
import pytest

from reyn.interfaces.repl.remote_client import post_control
from reyn.interfaces.transport.agui.client import AgUiTransport

_T = 0.05  # the injected control timeout — the subject, not a wait


async def _silent_server(hold: asyncio.Event):
    """A real listener that reads the request and then holds the connection
    open, writing nothing, until ``hold`` is set (teardown)."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)
            await hold.wait()
        finally:
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


async def _answering_server(body: bytes):
    """A real listener that answers every request with a 200 JSON body."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(65536)
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


def _url(server) -> str:
    port = server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}/agui/chat/alpha"


@pytest.mark.asyncio
async def test_a_control_post_against_a_silent_server_is_a_non_delivery_after_t():
    """Tier 2: the accept side of ①-1. With T supplied, a POST the server
    never answers returns ``None`` (non-delivery) instead of waiting
    forever — and the client it went through still carries the stream's
    ``read=None``, so the stream's policy was not the one that ended it."""
    hold = asyncio.Event()
    server = await _silent_server(hold)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            result = await post_control(
                client, _url(server), params={}, payload={"type": "cancel_inflight"},
                timeout_s=_T,
            )
            assert result is None, f"a silent server was read as an accept: {result!r}"
            assert client.timeout.read is None, (
                "the control timeout leaked into the client's default — the SSE "
                f"stream would now time out too: {client.timeout!r}"
            )
    finally:
        hold.set()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_a_control_post_the_server_answers_is_an_accept():
    """Tier 2: the discriminating control — the same call against a server
    that answers returns the parsed body, so the ``None`` above is the
    timeout's doing and not ``post_control`` returning ``None`` for
    everything."""
    server = await _answering_server(b'{"msg_id": "m1"}')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            result = await post_control(
                client, _url(server), params={}, payload={"type": "cancel_inflight"},
                timeout_s=_T,
            )
            assert result == {"msg_id": "m1"}
    finally:
        server.close()
        await server.wait_closed()


async def _no_lines() -> AsyncIterator[str]:
    """An SSE source that never yields — the transport's pump is never
    started here, so nothing reads it."""
    if False:  # pragma: no cover
        yield ""


@pytest.mark.asyncio
async def test_a_second_cancel_while_one_is_pending_sends_nothing():
    """Tier 2: ①-3. Two ``cancel_inflight`` calls while the first's POST is
    still in flight → exactly ONE payload on the wire, the second answered
    "already requested"; once the first returns, a later call sends
    again (the coalesce is per-pending-POST, not forever)."""
    sent: list[dict] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(payload: dict) -> "dict | None":
        sent.append(payload)
        entered.set()
        await release.wait()
        return {"status": "ok"}

    transport = AgUiTransport(_no_lines(), send)
    first = asyncio.ensure_future(transport.cancel_inflight())
    await entered.wait()

    second = await transport.cancel_inflight()
    assert second == "cancel already requested"
    assert [p["type"] for p in sent] == ["cancel_inflight"], sent

    release.set()
    assert await first == "cancel requested"

    third = await transport.cancel_inflight()
    assert third == "cancel requested"
    assert [p["type"] for p in sent] == ["cancel_inflight", "cancel_inflight"], (
        f"the coalesce outlived the pending POST — the third call sent nothing: {sent!r}"
    )


@pytest.mark.asyncio
async def test_a_cancel_the_wire_did_not_deliver_reads_as_the_empty_summary():
    """Tier 2: the link between ①-1 and the TUI's "not responding" row —
    ``send`` returning ``None`` (what ``post_control`` does after T) must
    NOT come back as "cancel requested"; the ABC reserves "" for it."""
    async def send(payload: dict) -> "dict | None":
        return None

    transport = AgUiTransport(_no_lines(), send)
    assert await transport.cancel_inflight() == ""
