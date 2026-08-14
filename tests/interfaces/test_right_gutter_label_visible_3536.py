"""#3536 — the right gutter's labels let the TERMINAL pick their colour.

Owner report: the elapsed / token labels are unreadable. The text was being
drawn; it was painted in a fixed truecolor mid-grey (`_CC_DIM = "#6b7280"`),
which has whatever contrast the user's background happens to give it — and on a
transparent terminal background, that is not a decision reyn is in a position to
make.

The fix is scoped, and the scope is the interesting part. `_CC_DIM` cannot
simply become an attribute everywhere: `prompt_toolkit` rejects `fg:dim`
outright, and on a row carrying a fixed dark TINT a terminal-default foreground
would be dark-on-dark for a light-terminal user — a pairing #3367's contrast
gate cannot even see, because it skips segments whose foreground is not
concrete. So the ambient attribute is used only where no tint is in play.

The last test here pins that premise rather than the fix: if an agent or tool
row ever starts carrying a tint, these labels would become exactly the
unmeasurable pairing described above, and no assertion about the label itself
would notice.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from rich.console import Console
from rich.text import Text
from textual_flowview import FlowModel

from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter
from reyn.interfaces.repl.renderer import _CC_AMBIENT, _CC_DIM
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


def _render(style: str) -> str:
    """What a label styled ``style`` actually puts on the wire."""
    buffer = Console(file=None, width=24, force_terminal=True, color_system="truecolor")
    with buffer.capture() as captured:
        buffer.print(Text("↑12k ↓2k", style=style), end="")
    return captured.get()


def test_the_ambient_label_style_forces_no_colour() -> None:
    """Tier 1: the ambient style emits an ATTRIBUTE, never a colour.

    Asserted on the emitted escape rather than on the constant's spelling: what
    makes the label legible on someone else's background is that no `38;2;`
    truecolor foreground reaches the wire, and only the rendered output can say
    that.
    """
    emitted = _render(_CC_AMBIENT)

    assert "38;2;" not in emitted, (
        f"the ambient label pinned a truecolor foreground: {emitted!r}"
    )
    assert "\x1b[2m" in emitted, (
        f"the ambient label is not dimmed, so it reads as ordinary text: {emitted!r}"
    )


def test_the_dim_colour_constant_stays_a_colour() -> None:
    """Tier 1: `_CC_DIM` is NOT converted along with it.

    Two callers structurally require a colour value — `prompt_toolkit`
    (`fg:dim` raises `ValueError: Wrong color format`) and any foreground that
    lands on a fixed dark row tint, which needs to remain measurable by #3367's
    contrast gate. A well-meaning sweep that "finishes the job" would break both.
    """
    assert _CC_DIM.startswith("#"), (
        f"_CC_DIM stopped being a colour ({_CC_DIM!r}) — prompt_toolkit's "
        "`fg:` and the tinted-row contrast gate both require one"
    )
    assert "38;2;" in _render(_CC_DIM), (
        "_CC_DIM no longer emits a concrete foreground, so the contrast gate "
        "silently stops inspecting the pairings it exists to measure"
    )


def test_prompt_toolkit_rejects_the_ambient_style_as_a_colour() -> None:
    """Tier 1: the reason for the split, as an executable fact.

    This is the constraint that makes the two-constant split necessary rather
    than stylistic — recorded as a test so it does not have to be rediscovered
    by whoever next proposes collapsing them.
    """
    from prompt_toolkit.styles import Style

    Style.from_dict({"ok": f"fg:{_CC_DIM}"})  # the colour form is accepted

    with pytest.raises(ValueError):
        Style.from_dict({"bad": f"fg:{_CC_AMBIENT}"})


@pytest.mark.asyncio
async def test_the_rows_carrying_these_labels_have_no_tint() -> None:
    """Tier 2b: the PREMISE that makes a terminal-coloured label safe here.

    A terminal-default foreground is only safe over a terminal-default
    background. The right gutter's labels ride on agent rows (token split) and
    started-tool rows (elapsed) — neither of which the presenter tints. If that
    ever changes, these labels become fixed-dark-under-unknown-ink and #3367's
    gate cannot see the pairing, so the failure has to be caught here.
    """
    presenter = ReynPresenter(clock=lambda: 0.0)

    for message in (
        OutboxMessage(kind="agent", text="a reply"),
        OutboxMessage(
            kind="tool_call_started", text="read_file", meta={"tool": "read_file"}
        ),
    ):
        presentation = await presenter.present(FlowModel().append(message), 80)
        assert presentation.background is None, (
            f"a {message.kind!r} row now carries the tint "
            f"{presentation.background!r} — the right gutter's label sits on it "
            "with a terminal-chosen colour, which no contrast gate can measure"
        )
