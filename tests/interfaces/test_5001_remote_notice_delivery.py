"""Tier 2: #5001 — two client-authored notices that used to vanish on a
REAL remote (AG-UI) connection.

Both ``TextualChatApp._notify_blocked_on_attach`` (the "still connecting" /
"attach failed" detail sentence shown when Enter is blocked) and
``TextualChatApp._submit``'s exception path (the "input could not be
submitted: ..." error) used to route through ``self._transport.
put_display(...)``. ``AgUiTransport.put_display`` is a CORRECT no-op — a
remote client cannot inject into the server's outbox (#4996③'s own
falsified design attempt tried to declare this as a "capability" and was
rejected: nobody read the declaration, since both call sites discarded
their own outcome already). The real defect was the CALL, not the no-op:
both notices are entirely CLIENT-SIDE — about THIS client's own composer/
submit attempt — and never needed the server's outbox at all. The fix
(architect, #5001) routes them through ``_ingest_frame`` instead, the SAME
local-model append both an in-process and a remote client already use for
other purely client-side rows — one path, no transport branch.

**Why the header's own "connecting"/"failed" STATE is not enough to catch
this bug** (architect's own framing): the header reads ``has_session()``/
``attach_failed()`` directly, independent of ``put_display`` — so it kept
rendering FINE on a remote client throughout this bug's whole lifetime,
masking the DETAIL sentence's disappearance. A witness that only asserts
the header's state (or the STATE half of these two notices) would stay
green with the bug still present — the DETAIL TEXT itself must be
asserted, over a REAL ``AgUiTransport``, not a hand-rolled stand-in whose
own ``put_display`` isn't the no-op in question.

Real ``AgUiTransport`` (constructed directly with a real, minimal
``sse_lines``/``send`` pair — the same shape ``test_3310_n3_remote_switch_
parity.py`` already uses) + the real mounted ``TextualChatApp`` — no mocks.
Both witnesses are strip-falsified below (reverting the fix in ``app.py``
turns them red for the stated reason: the detail text goes missing while
the app otherwise runs normally).
"""
from __future__ import annotations

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.agui.client import AgUiTransport


async def _empty_sse_lines():
    return
    yield  # pragma: no cover - makes this an async generator, never reached


async def _noop_send(_payload):
    return None


def _flow_texts(app: TextualChatApp) -> "list[str]":
    return [e.item.text or "" for e in app.query_one(FlowView).entries]


@pytest.mark.asyncio
async def test_blocked_on_attach_detail_reaches_a_real_remote_transport() -> None:
    """Tier 2: #5001's own falsifier, path ①. A REAL ``AgUiTransport`` with
    ``connected=False`` (still attaching) — pressing Enter must show the
    "still connecting" DETAIL sentence in the flow, not just leave the
    header's own state rendering while this specific notice silently
    vanishes into ``AgUiTransport.put_display``'s no-op."""
    transport = AgUiTransport(_empty_sse_lines(), _noop_send, connected=False)
    app = TextualChatApp(transport=transport, read_model=None)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "hello, are you there"
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("hello, are you there"))
        await pilot.pause()

        rows = _flow_texts(app)
        assert any("still connecting" in t.lower() for t in rows), (
            f"the connecting-detail sentence must reach the flow over a real "
            f"AgUiTransport; rows={rows!r}"
        )
        # The composer text itself is untouched by this fix (decision 4B,
        # #3671 P3) — re-asserted here as a sanity check that this test's
        # own real transport didn't accidentally let the submission through.
        assert composer.text == "hello, are you there"


@pytest.mark.asyncio
async def test_submit_failure_detail_reaches_a_real_remote_transport() -> None:
    """Tier 2: #5001's own falsifier, path ②. A REAL ``AgUiTransport`` whose
    ``send`` callable raises for real (its own documented extension point,
    not a patched internal — ``AgUiTransport.submit_user_text`` has no
    try/except of its own, so this propagates exactly like a genuine bug
    would) — the resulting error notice must reach the flow, not vanish
    into the same no-op."""
    async def _raising_send(_payload):
        raise RuntimeError("boom")

    transport = AgUiTransport(_empty_sse_lines(), _raising_send, connected=True)
    app = TextualChatApp(transport=transport, read_model=None)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.text = "go ahead"
        await pilot.pause()
        await app.on_composer_submitted(Composer.Submitted("go ahead"))
        await pilot.pause()

        rows = _flow_texts(app)
        assert any("input could not be submitted" in t.lower() for t in rows), (
            f"the submit-failure detail must reach the flow over a real "
            f"AgUiTransport; rows={rows!r}"
        )
