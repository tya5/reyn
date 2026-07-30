"""#3507 — flowview 0.7.0's copy mode, reachable from the conversation pane.

0.6.x had an ENTRY-granular keyboard cursor and nothing finer; "can the cursor
move inside an entry" was a real upstream gap. 0.7.0 fills it with **copy
mode** — a per-character text cursor with vim motions — and renames the old
entry cursor to *highlight* to free the word.

What these tests pin is reyn's WIRING, deliberately not flowview's motions:
entering copy mode, that it starts on the highlighted entry, and that the
addressed-row rail is not disturbed by it. The motions (hjkl w b e 0 $ ^ gg G
v V y …) are flowview's own defaults and its own tests' business — re-asserting
them here would pin someone else's contract and would have to be rewritten
every time upstream tunes a key (owner direction: keep flowview's default
keymap, so reyn declares no motion bindings at all).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.inline.textual_chat.gutter import _MARK_RAIL
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


class _Transport(ClientTransport):
    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        await asyncio.Event().wait()
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

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


def _railed_rows(flow: FlowView) -> "list[str]":
    return [
        row
        for row in (
            "".join(seg.text for seg in flow.render_line(y))
            for y in range(flow.size.height)
        )
        if _MARK_RAIL in row
    ]


async def _seeded(pilot, app, texts=("older reply", "newest reply")):
    for text in texts:
        app.conversation.append(OutboxMessage(kind="agent", text=text))
    await pilot.pause()
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    return app.query_one(FlowView)


@pytest.mark.asyncio
async def test_c_enters_copy_mode_on_the_highlighted_entry() -> None:
    """Tier 2b: ``c`` from the conversation pane enters copy mode, and it starts
    on the entry the highlight is already on — so the text cursor appears where
    the user was looking rather than at the top of the log."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)
        assert not flow.copy_mode, "test setup: already in copy mode"
        started_on = flow.highlighted
        assert started_on is not None and started_on.item.text == "newest reply"

        await pilot.press("c")
        await pilot.pause()
        assert flow.copy_mode, "'c' did not enter copy mode"
        assert flow.highlighted is started_on, (
            "entering copy mode moved the highlight off the entry the user was on"
        )


@pytest.mark.asyncio
async def test_copy_mode_leaves_the_addressed_row_rail_alone() -> None:
    """Tier 2b: the gutter rail (#3490) still marks the addressed row while copy
    mode is active, and still marks the SAME row.

    This is the interaction worth pinning rather than the motions: flowview
    holds the highlight fixed during copy mode and posts no ``Highlighted``
    while the text cursor moves, which is exactly what the rail depends on —
    it is re-derived from ``Highlighted`` plus focus changes. If upstream ever
    moved the highlight per motion, the rail would chase the text cursor and
    this goes red."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)
        before = _railed_rows(flow)
        assert any("newest reply" in row for row in before), (
            f"test setup: the addressed row is not railed: {before!r}"
        )

        await pilot.press("c")
        await pilot.pause()
        after = _railed_rows(flow)
        assert any("newest reply" in row for row in after), (
            f"the rail left the addressed row on entering copy mode: {after!r}"
        )
        assert not any("older reply" in row for row in after), (
            f"a second row became railed in copy mode: {after!r}"
        )


@pytest.mark.asyncio
async def test_reyn_declares_no_copy_mode_motion_bindings() -> None:
    """Tier 2: reyn adds NO motion binding of its own — the keymap inside copy
    mode is flowview's (owner direction). A binding added here later would
    silently shadow an upstream key and drift from it, so the absence is the
    thing worth asserting; ``c`` (entry) is reyn's one and only addition."""
    keys = {
        (b[0] if isinstance(b, tuple) else b.key) for b in TextualChatApp.BINDINGS
    }
    motions = {"h", "j", "k", "l", "w", "b", "e", "0", "$", "^", "g", "v", "V", "y",
               "n", "N", "*", "[", "]", "z"}
    assert not (keys & motions), (
        f"reyn declared copy-mode motion keys of its own: {sorted(keys & motions)} — "
        "the motions belong to flowview's defaults"
    )
    assert "c" in keys, "the copy-mode entry key is missing from the app bindings"


@pytest.mark.asyncio
async def test_copy_mode_yank_writes_through_reyns_local_clipboard(
    tmp_path, monkeypatch
) -> None:
    """Tier 2b: copy mode's clipboard sink is reyn's local tool, and its result
    is observable.

    flowview's default sink is ``App.copy_to_clipboard`` — OSC 52, which
    Textual's own docstring says does not work on macOS Terminal, which tmux/ssh
    can swallow, and which cannot be acknowledged (so it optimistically reports
    success). reyn passes ``clipboard=`` instead, so ``y`` lands where ``/copy``
    and Enter-on-an-entry already land, and a FAILED yank is distinguishable
    from a successful one.

    Exercised through the public ``write_clipboard`` seam rather than by driving
    motions: the motions are flowview's contract, the sink is reyn's. The tool
    is a REAL ``pbcopy`` on PATH (environment arrangement, not a mock)."""
    import os
    import stat

    bindir = tmp_path / "bin"
    bindir.mkdir()
    sink = tmp_path / "clip.txt"
    script = bindir / "pbcopy"
    script.write_text(
        "#!/bin/sh\n/bin/cat > " + str(sink) + ".part\n"
        "/bin/mv " + str(sink) + ".part " + str(sink) + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)
        wrote = flow.write_clipboard("yanked from copy mode")
        assert wrote is True, (
            "the sink reported failure — reyn's clipboard tool did not accept the "
            "text, or the default OSC 52 path is still in use"
        )
        for _ in range(60):
            await pilot.pause()
            if sink.exists():
                break
        assert sink.exists() and sink.read_text() == "yanked from copy mode", (
            "copy mode's yank did not reach reyn's local clipboard tool"
        )


@pytest.mark.asyncio
async def test_the_chrome_sees_both_copy_mode_edges() -> None:
    """Tier 2b: reyn is told when copy mode is entered AND when it is left
    (flowview 0.8.0's ``CopyModeChanged``, #8).

    The exit edge is the one that matters: entry is reyn's own action, but
    leaving happens on ``Esc`` INSIDE the widget. Without the message there was
    no way to observe it short of polling, so a "copy mode" hint would have
    stayed up after the user left — which is precisely the state a modal keymap
    must not be in. Asserted through the conversation's own status rows, not a
    private flag."""
    app = TextualChatApp(transport=_Transport())
    async with app.run_test(size=(80, 20)) as pilot:
        flow = await _seeded(pilot, app)

        await pilot.press("c")
        await pilot.pause()
        assert flow.copy_mode, "setup: 'c' did not enter copy mode"
        texts = [e.item.text for e in app.conversation]
        assert any("copy mode" in t and "Esc leave" in t for t in texts), (
            f"entering copy mode told the user nothing; pane holds: {texts!r}"
        )

        flow.exit_copy_mode()
        await pilot.pause()
        assert not flow.copy_mode, "setup: copy mode did not exit"
        texts = [e.item.text for e in app.conversation]
        assert any("copy mode off" in t for t in texts), (
            f"the exit edge was not observed; pane holds: {texts!r}"
        )
