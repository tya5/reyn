"""Tier 2: #5107, architect ruling B (issuecomment-5379950484) —
``AgUiTransport.put_display`` renders a client-authored slash reply
locally (through the SAME queue :meth:`AgUiTransport.frames` yields
from), instead of the pre-#5107 no-op that silently dropped it.

lead-coder's contract-first correction (issuecomment-5379955824):
``ClientTransport.put_display``'s own docstring only ever asked for
"show it on this client's own face" — the pre-#5107 no-op's stated
justification ("a remote client cannot inject into the server's
outbox") answered a DIFFERENT, stronger claim the docstring never
actually made. Fixed contract-first (this module's own sibling,
``client_transport.py``), then the implementation.

Two witnesses, per lead-coder's own discriminator: ① alone (a
success-shaped reply, ``/help``) would stay green for an implementation
wired ONLY for the happy path; ② (``/attach`` to a name the server
rejects) exercises the FAILURE branch too. Both strip-falsified below:
reverting :meth:`AgUiTransport.put_display` to the pre-#5107 no-op
turns BOTH red.

Real ``AgUiTransport`` (constructed directly, a real, minimal
``sse_lines``/``send`` pair — the same shape ``test_5001_remote_notice_
delivery.py`` and ``test_3310_n3_remote_switch_parity.py`` already use)
+ the real ``maybe_dispatch_slash``/slash registry — no mocks.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.frames import DisplayFrame


async def _empty_sse_lines():
    return
    yield  # pragma: no cover - makes this an async generator, never reached


async def _drain_display_texts(transport: AgUiTransport) -> "list[str]":
    """Every ``DisplayFrame`` text ``frames()`` yields before the (empty,
    test-only) SSE source naturally exhausts and the generator returns."""
    out: list[str] = []
    async for frame in transport.frames():
        if isinstance(frame, DisplayFrame):
            out.append(frame.message.text)
    return out


@pytest.mark.asyncio
async def test_help_reply_reaches_a_real_remote_transport() -> None:
    """Tier 2: #5107 witness ① — ``/help`` (client-locus) is dispatched
    BEFORE ``frames()`` is ever iterated, so its ``put_display`` calls are
    already sitting in the transport's own display queue (FIFO, ahead of
    the empty SSE source's own eventual end-of-stream marker) by the time
    ``frames()`` starts draining — deterministic, no race against the
    empty test source's own near-instant exhaustion."""
    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_empty_sse_lines(), _noop_send, connected=True)

    consumed = await maybe_dispatch_slash(transport, "/help", echo=False)
    assert consumed is True

    texts = await _drain_display_texts(transport)
    assert any(texts), (
        "‥/help must produce at least one non-empty display line over a "
        f"real AgUiTransport; got {texts!r}"
    )
    assert any("attach" in t.lower() or "session" in t.lower() for t in texts), (
        f"expected the /help catalog to name at least one other command; got {texts!r}"
    )


@pytest.mark.asyncio
async def test_attach_failure_reply_reaches_a_real_remote_transport() -> None:
    """Tier 2: #5107 witness ② — the FAILURE branch, not just the happy
    path (lead-coder's own discriminator). ``request_attach``'s typed op
    reports the target was rejected (a real, minimal ``send`` returning
    ``{"attached": False}`` — the exact shape ``AgUiTransport.
    request_attach`` reads), so ``attach_cmd``'s own False branch
    (#5096 ②) is the one exercised — its "could not confirm" reply must
    still reach this client, not just a successful attach's."""
    async def _reject_send(payload):
        if payload.get("type") == "attach_request":
            return {"status": "ok", "attached": False}
        return None

    transport = AgUiTransport(_empty_sse_lines(), _reject_send, connected=True)

    consumed = await maybe_dispatch_slash(transport, "/attach ghost-agent", echo=False)
    assert consumed is True

    texts = await _drain_display_texts(transport)
    assert any("ghost-agent" in t for t in texts), (
        f"the failed-attach reply must name the target agent; got {texts!r}"
    )
    assert any("could not confirm" in t.lower() for t in texts), (
        f"expected attach_cmd's own False-branch wording; got {texts!r}"
    )
