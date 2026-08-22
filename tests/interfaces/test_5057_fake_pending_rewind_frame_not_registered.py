"""Tier 2: #5057 — a `/rewind` text-list fallback must not register as a
pending intervention either (the SAME hole #5047 closed for restored
frames, reopened by a different producer).

Real chain of causes: `app.py`'s own `/rewind` fallback (`_handle_rewind_
request`, reached when no structured command-UI request is available —
the REMOTE case, `pending_command_ui()` is always None there by design)
reuses `kind="intervention"` purely for its PERSISTENT-render property (a
`kind="status"` row would be transient and vanish) — `replace(msg,
kind="intervention")` on the ORIGINAL `__rewind_list__` sentinel, which
carries no `intervention_id`. `stream_client.py`'s plain-client sibling
does the identical thing (`OutboxMessage(kind="intervention", text=
msg.text)`, no meta at all).

#5047's own fix guarded on `RESTORED_META_KEY` — a marker naming ONE
producer (`restore.py`), not the property that actually decides whether an
answer can be routed safely. This rewind-list frame carries neither
`RESTORED_META_KEY` NOR a real `intervention_id`, so it slipped straight
through that guard and registered as fake-pending exactly like #5047's own
case did — the "a fix keyed on which producer wrote this always misses the
next producer" failure #5057 measured directly (`issuecomment-5377340171`
census: 4 producers, only `announce` sets `intervention_id`).

The fix (this PR) re-keys the guard on `intervention_id` PRESENCE instead
— the value that decides, downstream, whether ``answer_intervention_by_id``
(safe) or ``answer_oldest_*`` (misdeliverable) gets used. This test is
that fix's own strip-falsifier for the rewind-frame instance specifically
(the #5047 file's own 4 tests already re-verify the restored-frame
instance still passes under the new discriminator).

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` (no mocks) —
mirrors `test_5047_replayed_answered_intervention_not_pending.py`'s own
`_ReplayTransport` shape (not imported from it — this repo's own tests
don't cross-import each other's private fixtures).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _ReplayTransport(ClientTransportStub):
    """A real, minimal ``ClientTransport`` that replays a fixed frame list."""

    def __init__(self, messages: "list[OutboxMessage]") -> None:
        self._messages = list(messages)

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> str:
        return ""

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        return None


def _rewind_list_frame() -> OutboxMessage:
    """Shaped exactly like the real ``__rewind_list__`` sentinel a REMOTE
    server sends when no structured command-UI request is on the wire
    (``pending_command_ui()`` is None there by design, read_model.py) — no
    ``intervention_id``, no ``RESTORED_META_KEY``; `app.py`'s own
    `_handle_rewind_request` re-tags this SAME frame's ``kind`` to
    ``"intervention"`` when it arrives (the code path this test drives)."""
    return OutboxMessage(
        kind="__rewind_list__",
        text="Rewind points:\n  1. abc123 - fix bug\n  2. def456 - add feature",
    )


@pytest.mark.asyncio
async def test_rewind_list_fallback_does_not_open_the_pending_panel():
    """Tier 2: strip-falsifier. A `__rewind_list__` sentinel (no structured
    command-UI request pending, forcing the text fallback) must never open
    the intervention panel — reverting the guard in `app.py`'s
    `_ingest_frame` to unconditional (`if kind == "intervention":`, no
    `intervention_id` check at all) turns this RED: the rewind-list frame
    then registers as pending (confirmed by actually running the strip,
    not asserted from reading the diff).

    architect's non-block finding (PR #5060 TESTS-READ,
    issuecomment-5377534130): the 2 assertions below are both negative
    (panel stays closed) — `_ingest_frame`'s own `except Exception:
    logger.exception` swallows a processing exception silently, so an
    exception midway would ALSO leave the panel closed and pass these
    2 asserts for the WRONG reason (six-questions Q4). Closed with a
    POSITIVE assertion: the rewind list's own text must actually have
    reached the conversation flow (proving the frame was processed at
    all, not silently dropped)."""
    transport = _ReplayTransport([_rewind_list_frame()])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        from textual_flowview import FlowView

        flow = app.query_one(FlowView)
        rendered_texts = [e.item.text for e in flow.entries]
        assert any("Rewind points" in t for t in rendered_texts), (
            "the rewind-list frame must actually land in the conversation "
            "(proves _ingest_frame processed it, rather than an exception "
            "being silently swallowed and coincidentally also leaving the "
            f"panel closed) — got entries: {rendered_texts!r}"
        )

        panel = app.query_one(InterventionPanel)
        assert panel.display is False, (
            "a /rewind text-list fallback must not register as pending — "
            "the panel should never open for it"
        )
        assert panel.has_pending() is False, (
            "a phantom entry was registered as pending (public "
            "has_pending() surface, not private app state)"
        )
