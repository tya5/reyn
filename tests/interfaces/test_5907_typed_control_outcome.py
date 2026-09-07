"""Tier 2: #5907 ② — a control POST's outcome is TYPED, and a refusal and a
non-delivery never read the same on any surface.

Before: ``post_control`` returned ``None`` for "the server said no" and for
"the server never answered", and every slash handler's ``if not ok:`` drew
one line for both — so an operator whose ``/session switch`` timed out
(the request possibly delivered) read the same words as one whose switch
was refused. Architect (#5907): a falsy carrying two facts is the fail-open
class; make the two unrepresentable together with a type, and render the
failure line in ONE place so the 27 handlers say the right thing unedited.

Everything here is real: real TCP listeners (one silent, one refusing with
a 403 body, one accepting), a real ``httpx.AsyncClient``, the real
``post_control`` with T supplied as an INPUT, a real ``AgUiTransport``
whose ``send`` IS ``post_control``, and the real client-side slash layer
(``maybe_dispatch_slash``) drawing its line through the real
``reply_error``. The line is read off the transport's own ``put_display``
(the slash layer's display seam), captured by a real subclass.

Strip (verified): making ``post_control`` return ``None`` for both failures
(the pre-#5907 shape) makes ``test_a_refusal_and_a_non_delivery_draw_
different_lines`` red — the two ``/session switch`` lines become identical.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx
import pytest

from reyn.interfaces.repl.remote_client import post_control
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.control_outcome import ControlOutcome, describe_control_failure

_T = 0.05  # the injected control timeout — the subject, not a wait


async def _silent_server(hold: asyncio.Event):
    async def handler(reader, writer) -> None:
        try:
            await reader.read(65536)
            await hold.wait()
        finally:
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


async def _answering_server(status_line: bytes, body: bytes):
    async def handler(reader, writer) -> None:
        try:
            await reader.read(65536)
            writer.write(
                status_line + b"\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n" + body
            )
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handler, "127.0.0.1", 0)


def _url(server) -> str:
    return f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/agui/chat/alpha"


# ── post_control: three outcomes, three kinds ──────────────────────────────


@pytest.mark.asyncio
async def test_post_control_names_the_three_outcomes():
    """Tier 2: delivered / refused / not_delivered are distinct, typed, and
    the outcome is truthy iff delivered (the old ``if accepted:`` contract)."""
    hold = asyncio.Event()
    silent = await _silent_server(hold)
    refusing = await _answering_server(b"HTTP/1.1 403 Forbidden", b'{"detail": "bad token"}')
    accepting = await _answering_server(b"HTTP/1.1 200 OK", b'{"msg_id": "m1"}')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            nd = await post_control(client, _url(silent), params={}, payload={"type": "x"}, timeout_s=_T)
            rf = await post_control(client, _url(refusing), params={}, payload={"type": "x"}, timeout_s=_T)
            ok = await post_control(client, _url(accepting), params={}, payload={"type": "x"}, timeout_s=_T)
    finally:
        hold.set()
        for s in (silent, refusing, accepting):
            s.close()
            await s.wait_closed()

    assert nd.kind == "not_delivered" and not nd and nd.timeout_s == _T and nd.cause, nd
    assert rf.kind == "refused" and not rf and rf.status == 403 and "bad token" in (rf.reason or ""), rf
    assert ok.kind == "delivered" and ok and ok.payload == {"msg_id": "m1"}, ok


def test_the_describer_never_words_a_refusal_like_a_non_delivery():
    """Tier 1: the ONE wording function — a refusal names the server's
    reason; a non-delivery says the request may not have reached it (the
    non-idempotent operator's question); delivered / untyped add nothing."""
    refused = describe_control_failure(ControlOutcome.refused(403, "bad token"))
    lost = describe_control_failure(ControlOutcome.not_delivered("ReadTimeout", 10.0))
    assert refused and "refused" in refused and "bad token" in refused
    assert lost and "may not have reached" in lost and "10" in lost
    assert refused != lost
    assert describe_control_failure(ControlOutcome.delivered({})) is None
    assert describe_control_failure(None) is None


# ── end to end: the slash layer's line, through the real renderer ──────────


class _DisplayCapturingTransport(AgUiTransport):
    """The real ``AgUiTransport`` with its display seam captured — the
    slash layer's ``reply_error`` lands here."""

    def __init__(self, send) -> None:
        super().__init__(_no_lines(), send)
        self.shown: "list[tuple[str, str]]" = []

    def put_display(self, msg) -> None:
        self.shown.append((msg.kind, msg.text))


async def _no_lines() -> AsyncIterator[str]:
    if False:  # pragma: no cover
        yield ""


def _sender(client, server):
    """The real ``post_control`` bound to one listener, with T supplied —
    the ``send`` callable ``remote_client.py`` hands ``AgUiTransport``."""
    async def send(payload: dict):
        return await post_control(client, _url(server), params={}, payload=payload, timeout_s=_T)
    return send


async def _switch_line_against(server, client) -> str:
    transport = _DisplayCapturingTransport(_sender(client, server))
    handled = await maybe_dispatch_slash(transport, "/session switch other", echo=False)
    assert handled, "setup: /session switch was not dispatched"
    errors = [t for k, t in transport.shown if k == "error"]
    assert errors, f"setup: no error line was drawn; shown={transport.shown!r}"
    return errors[-1]


@pytest.mark.asyncio
async def test_a_refusal_and_a_non_delivery_draw_different_lines():
    """Tier 2: the accept side, end to end. The SAME command, unedited
    handler, against a server that refuses and one that never answers:
    the two error lines differ, and each says which it was."""
    hold = asyncio.Event()
    silent = await _silent_server(hold)
    refusing = await _answering_server(b"HTTP/1.1 409 Conflict", b'{"detail": "no such session"}')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            lost_line = await _switch_line_against(silent, client)
            refused_line = await _switch_line_against(refusing, client)
    finally:
        hold.set()
        for s in (silent, refusing):
            s.close()
            await s.wait_closed()

    assert lost_line != refused_line, (
        f"a timeout and a refusal read the same: {lost_line!r}"
    )
    assert "may not have reached" in lost_line, lost_line
    assert "refused" in refused_line and "no such session" in refused_line, refused_line


@pytest.mark.asyncio
async def test_a_delivered_control_adds_nothing_to_a_later_error_line():
    """Tier 2: the discriminating control — after a DELIVERED POST, an
    unrelated error line (a typo) is drawn plain: the renderer decorates
    only a recorded failure, never the last success."""
    accepting = await _answering_server(b"HTTP/1.1 200 OK", b'{"switched": true}')
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:
            transport = _DisplayCapturingTransport(_sender(client, accepting))
            assert await transport.request_session_switch("s2") is True
            await maybe_dispatch_slash(transport, "/halp", echo=False)
            errors = [t for k, t in transport.shown if k == "error"]
    finally:
        accepting.close()
        await accepting.wait_closed()
    assert errors and "unknown command" in errors[-1]
    assert "refused" not in errors[-1] and "may not have reached" not in errors[-1], errors[-1]
