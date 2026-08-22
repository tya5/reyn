"""#3624 — a click on the conversation cursor must NOT copy to the clipboard.

flowview 0.11.0 unified the keyboard highlight and mouse selection into one
``current`` cursor: a click now both MOVES and COMMITS it (``Selected``
fires on a click exactly like it fires on Enter/Space, with nothing in the
event to tell them apart). Reading ``Selected`` as "copy this entry" — reyn's
pre-0.11.0 intent, previously wired to the now-removed ``FlowView.Activated``
— would let one stray click silently overwrite whatever the user had copied
in a DIFFERENT application (possibly credentials). ``_CursorFlowView``
(``textual_chat/app.py``) intercepts the KEYBOARD-only ``action_activate``
call path instead of ``Selected`` — a click never runs ``action_activate``
(``FlowView.on_click`` calls ``self.activate()`` directly, bypassing the
action/binding system).

What this pins (real ``TextualChatApp`` + a real minimal ``ClientTransport``,
public surface only — a simulated click via ``pilot.click``, a real ``xclip``
stand-in, ``FlowView.current``): a click moves the cursor onto the entry and
does NOT write the clipboard; Enter afterwards still copies it (proving the
keyboard path survives the split intact, not merely that the click path was
disabled)."""
from __future__ import annotations

import asyncio
import os
import stat
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransportStub):
    def __init__(self) -> None:
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass

    async def deliver_pending_answer(self, text: str) -> bool:
        return False


@pytest.fixture()
def clipboard(tmp_path, monkeypatch):
    """A REAL ``xclip`` on ``PATH`` recording its stdin — the #3362/#3476⑤
    witness shape (environment arrangement, not a mock). See the identical
    fixture in ``test_conversation_cursor_3476.py`` for the full rationale
    (pyperclip's backend selection is platform-gated, so ``set_clipboard``
    pins it explicitly rather than relying on Darwin-only ``pbcopy``
    detection)."""
    import pyperclip

    original_copy, original_paste = pyperclip.copy, pyperclip.paste

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clipboard.txt"
    script = bindir / "xclip"
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    pyperclip.set_clipboard("xclip")

    def read():
        return sink.read_text() if sink.exists() else None

    try:
        yield read
    finally:
        pyperclip.copy, pyperclip.paste = original_copy, original_paste


@pytest.mark.asyncio
async def test_a_click_moves_the_cursor_but_does_not_copy(clipboard) -> None:
    """Tier 2b: clicking the entry moves ``FlowView.current`` onto it (proving
    the click actually landed and was processed, not merely a no-op) but
    leaves the clipboard untouched — a stray click must never overwrite what
    the user copied elsewhere."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply worth reading"))
        await pilot.pause()

        flow = app.query_one(FlowView)
        assert flow.current is None, "test setup: cursor already positioned"

        landed = await pilot.click(FlowView, offset=(5, 0))
        await pilot.pause()
        assert landed, "test setup: the click did not land inside FlowView"

        assert flow.current is not None and flow.current.item.text == "a reply worth reading", (
            f"click did not move the cursor onto the entry: {flow.current!r}"
        )
        # Give any (wrongly) scheduled copy coroutine a chance to run before
        # asserting its absence — a false negative here would hide the bug.
        for _ in range(5):
            await pilot.pause()
        assert clipboard() is None, (
            f"a click copied to the clipboard — got {clipboard()!r}; "
            "flowview 0.11.0+'s Selected fires on a click too and must not "
            "be read as a copy trigger (#3624)"
        )


@pytest.mark.asyncio
async def test_enter_after_a_click_still_copies(clipboard) -> None:
    """Tier 2b: the click path being inert does not mean the keyboard path
    broke too — Enter on the cursor (now sitting where the click left it)
    copies normally, proving the two commit paths were genuinely split
    rather than both disabled."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="a reply worth reading"))
        await pilot.pause()

        await pilot.click(FlowView, offset=(5, 0))
        await pilot.pause()
        assert clipboard() is None, "setup: click already copied (see the other test)"

        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if clipboard() is not None:
                break
        assert clipboard() == "a reply worth reading", (
            f"Enter after a click did not copy the cursor entry (got {clipboard()!r})"
        )
