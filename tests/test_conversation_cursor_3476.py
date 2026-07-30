"""#3476 ⑥ — the conversation pane's keyboard cursor (``cursor=True``).

The visual affordance #3470 deferred to this PR: FlowView is reachable via
Shift+Tab (established there), and this PR gives that focus state a keyboard
cursor — ↑/↓/PageUp/PageDown/Home/End move it (flowview's own built-in
bindings, not re-tested here), Enter/Space copies the cursor entry directly
to the clipboard, and ``r`` opens ``/rewind`` through the ordinary submit
seam. What these tests pin (real ``TextualChatApp`` + a real minimal
``ClientTransport``, public surface only — pressed keys, ``FlowView.cursor``,
a real ``pbcopy`` stand-in, the transport's own submitted-text log):

- Shift+Tab reaches the conversation pane with the cursor on the LAST entry
  (``cursor_last`` is flowview's own mount-time default once ``cursor=True``);
- Enter/Space copies the CURSOR entry's own text — any kind, not just an
  agent reply, and NOT through the ``/copy`` ring;
- ``r`` submits bare ``/rewind`` through the app's normal submit seam (the
  SAME path an ordinary composer-typed ``/rewind`` would take) — never a
  per-entry targeted jump (there is no chat-seq/WAL-seq correlation to make
  one; #3476 issue comment).
"""
from __future__ import annotations

import asyncio
import os
import stat
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransport):
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
    """A REAL ``pbcopy`` on ``PATH`` recording its stdin — the #3362/#3476⑤
    witness shape (environment arrangement, not a mock)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clipboard.txt"
    script = bindir / "pbcopy"
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    def read():
        return sink.read_text() if sink.exists() else None

    return read


async def _focus_flow(pilot, app) -> "FlowView":
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    flow = app.query_one(FlowView)
    assert app.focused is flow, f"setup: Shift+Tab did not focus FlowView: {app.focused!r}"
    return flow


@pytest.mark.asyncio
async def test_shift_tab_arms_the_cursor_on_the_last_entry() -> None:
    """Tier 2b: reaching FlowView via Shift+Tab starts the cursor on the
    newest (last) entry — flowview's own ``cursor=True`` mount default."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        for text in ("one", "two", "three"):
            app.conversation.append(OutboxMessage(kind="agent", text=text))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)
        assert flow.cursor is not None and flow.cursor.item.text == "three", (
            f"cursor did not start on the newest entry: {flow.cursor!r}"
        )


@pytest.mark.asyncio
async def test_enter_copies_the_cursor_entry_directly(clipboard) -> None:
    """Tier 2b: Enter (Activated) copies the CURSOR entry's own text — a
    TOOL-kind entry here, proving this bypasses the agent-only /copy ring
    entirely (the ring cannot address a tool row at all)."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="an agent reply"))
        app.conversation.append(OutboxMessage(kind="tool_call_completed", text="tool output XYZ"))
        await pilot.pause()
        flow = await _focus_flow(pilot, app)
        assert flow.cursor.item.text == "tool output XYZ", (
            "setup: cursor is not on the tool entry"
        )

        await pilot.press("enter")
        for _ in range(30):
            await pilot.pause()
            if clipboard() is not None:
                break
        assert clipboard() == "tool output XYZ", (
            f"Enter did not copy the cursor entry's own text (got {clipboard()!r})"
        )


@pytest.mark.asyncio
async def test_space_also_activates_the_cursor_entry(clipboard) -> None:
    """Tier 2b: Space is flowview's other built-in activate key — same effect
    as Enter, verified independently rather than assumed from Enter's test."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="reply text"))
        await pilot.pause()
        await _focus_flow(pilot, app)

        await pilot.press("space")
        for _ in range(30):
            await pilot.pause()
            if clipboard() is not None:
                break
        assert clipboard() == "reply text"


@pytest.mark.asyncio
async def test_r_opens_rewind_through_the_ordinary_submit_seam() -> None:
    """Tier 2b: 'r' with the cursor focused submits bare '/rewind' through
    the SAME seam an ordinary composer submission uses — not a targeted
    per-entry jump (there is no seq correlation to make one)."""
    transport = _Transport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.conversation.append(OutboxMessage(kind="agent", text="reply"))
        await pilot.pause()
        await _focus_flow(pilot, app)

        await pilot.press("r")
        await pilot.pause()
        assert transport.submitted == ["/rewind"], (
            f"'r' did not submit bare /rewind through the normal seam: "
            f"{transport.submitted!r}"
        )


@pytest.mark.asyncio
async def test_r_is_a_plain_character_everywhere_else() -> None:
    """Tier 2b: 'r' typed in the COMPOSER (the resting focus state) is an
    ordinary character, never intercepted — the shortcut is gated on the
    cursor actually having focus."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        composer = app.query_one(Composer)
        composer.focus()
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        assert composer.text == "r", (
            f"'r' was intercepted while the composer had focus: {composer.text!r}"
        )
