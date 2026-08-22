"""Tier 2: #5047 — a REPLAYED, already-answered intervention frame must not
be registered as pending.

Real chain of causes (lead-coder's own trace, `issuecomment-5377193342`,
architect's ruling `issuecomment-5377199266`): on every (re)connect,
``restore.py``'s ``project_restored_frames`` projects a history entry that
WAS already answered into the SAME ``kind="intervention"`` shape a genuine
LIVE pending one uses. `app.py`'s ``_present_intervention`` call site had
no guard distinguishing the two, so this replayed, already-resolved frame
was ALSO registered into ``self._pending_ivs`` / ``InterventionPanel.
add_pending`` — a fake pending entry sitting in the same registry a genuine
one lives in.

**The real harm is not visual** (architect's own point): ``answer_oldest_
intervention_text``/``_choice`` deliver to the registry's OLDEST pending —
a phantom already-answered entry sorting ahead of a real pending one
misdirects the NEXT genuine answer to whichever real entry happens to sort
after the phantom, not the one the user actually meant.

**The discriminator, corrected mid-implementation** (lead-coder, measured
against ``restore.py:95-104``, not guessed): the first draft of this fix
guarded on ``meta["_answer_label"]`` being truthy — but a restored frame's
``_answer_label`` can itself be the EMPTY STRING (``restore.py``'s own
``meta.get(INTERVENTION_ANSWER_META_KEY, "")``), so a falsy-VALUE check
lets an empty-label restored frame slip through and register as pending
anyway — the #4996-family "the value's own absence doesn't distinguish two
different reasons" conflation, here on emptiness rather than None. Fixed
to check ``RESTORED_META_KEY`` (fixed ``True``/absent) instead — every
restored ``kind="intervention"`` frame is, by construction, always
already-answered (an intervention never answered has no history trace to
restore from at all), so this marker is both necessary and sufficient.
``gutter.py``'s own sibling check (:228) had the identical value-truthiness
trap (found in the same investigation) and is fixed alongside — presence
of the ``_answer_label`` KEY, not its value.

This test's own witness is the fake-pending REGISTRATION itself (the panel
never opening), not the rendered text (which was already correct before
this fix — the presenter's RESOLVED branch pre-dates #5047 and checks
``answer is not None``, never truthiness).

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` (no mocks).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.intervention_panel import InterventionPanel
from reyn.interfaces.inline.textual_chat.restore import RESTORED_META_KEY
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_GUTTER_WIDTH = 2


class _ReplayTransport(ClientTransport):
    """A real, minimal ``ClientTransport`` that replays a fixed frame list —
    mirrors ``test_textual_chat_intervention_panel_3299.py``'s own
    ``RecordingTransport`` shape, not imported from it (that class lives in
    a sibling test module, and this repo's own tests don't cross-import
    each other's private fixtures)."""

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


def _restored_answered_frame(*, answer_label: str) -> OutboxMessage:
    """Shaped EXACTLY like ``restore.py``'s ``project_restored_frames`` own
    projection of an already-answered history entry (:305-314): ``kind=
    "intervention"``, ``RESTORED_META_KEY: True``, no ``intervention_id``
    (a restored entry carries none — this is itself part of #5047's own
    misdelivery mechanism, not something this test needs to exercise
    further)."""
    return OutboxMessage(
        kind="intervention",
        text="Allow fetching from 'news.ycombinator.com'?",
        meta={
            RESTORED_META_KEY: True,
            "prompt": "Allow fetching from 'news.ycombinator.com'?",
            "detail": None,
            "_answer_label": answer_label,
        },
    )


def _genuine_pending_frame() -> OutboxMessage:
    """A real LIVE pending intervention — no ``RESTORED_META_KEY``, no
    ``_answer_label`` yet."""
    return OutboxMessage(
        kind="intervention",
        text="Allow write to /etc/hosts?",
        meta={
            "intervention_id": "iv-live",
            "prompt": "Allow write to /etc/hosts?",
            "choices": [
                {"id": "yes", "label": "Yes", "hotkey": "y"},
                {"id": "no", "label": "No", "hotkey": "n"},
            ],
        },
    )


@pytest.mark.asyncio
async def test_replayed_answered_intervention_does_not_open_the_pending_panel():
    """Tier 2: strip-falsifier. A backlog replay carrying ONLY an already-
    answered intervention must never open the panel — reverting the
    ``RESTORED_META_KEY`` guard in ``app.py`` (registering it as pending
    again) turns this red (``panel.display`` becomes True for a frame that
    was never actually pending)."""
    transport = _ReplayTransport([_restored_answered_frame(answer_label="Always")])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is False, (
            "an already-answered replayed intervention must not register "
            "as pending — the panel should never open for it"
        )
        assert panel.has_pending() is False, (
            "a phantom entry was registered as pending (public "
            "has_pending() surface, not private app state)"
        )


@pytest.mark.asyncio
async def test_replayed_intervention_with_an_empty_answer_label_still_excluded():
    """Tier 2: the falsifying edge case that moved the discriminator from
    ``_answer_label`` to ``RESTORED_META_KEY`` mid-implementation. An empty-
    string ``_answer_label`` (a real shape ``restore.py`` can produce —
    ``meta.get(INTERVENTION_ANSWER_META_KEY, "")``) must still be excluded
    from pending registration. A guard checking ``not meta.get(
    "_answer_label")`` (falsy-VALUE, the first-draft mistake) would treat
    the empty string the same as "key absent" and let this slip through —
    this test pins that the ACTUAL guard (key presence) does not have that
    gap."""
    transport = _ReplayTransport([_restored_answered_frame(answer_label="")])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is False, (
            "an empty _answer_label must not be mistaken for a genuinely "
            "pending intervention"
        )
        assert panel.has_pending() is False


def test_gutter_reads_an_empty_answer_label_as_resolved_not_pending():
    """Tier 2: strip-falsifier for the sibling ``gutter.py`` fix (same
    investigation, same emptiness-vs-absence trap). A restored intervention
    with an empty ``_answer_label`` must get the RESOLVED (dim, not the
    amber "needs you") gutter glyph, not the PENDING one — reverting
    ``ReynGutter``'s "intervention" branch back to ``not (msg.meta or {}).get(
    "_answer_label")`` (falsy-VALUE) turns this red: an empty string is
    falsy, so it would render as still-PENDING.

    Uses the PUBLIC ``ReynGutter.decorate`` surface over a real
    ``FlowModel``/``Entry`` pair (``textual_flowview`` — no mount needed,
    ``FlowModel`` is a plain, UI-less collection) rather than importing the
    private ``_gutter_glyph_color`` helper directly (lead-coder's TESTS-READY
    finding on the first draft — CLAUDE.md's own testing policy: use the
    public surface, and its absence is itself a finding when there is
    none — here there IS one, ``decorate``, already exercised by
    ``test_textual_chat_intervention_panel_3299.py``'s own
    ``test_resolved_intervention_gutter_is_not_the_needs_you_amber``)."""
    from textual_flowview import FlowModel

    from reyn.interfaces.inline.textual_chat.gutter import ReynGutter

    gutter = ReynGutter()
    model: "FlowModel[OutboxMessage]" = FlowModel()

    resolved_empty = model.append(
        OutboxMessage(kind="intervention", text="…", meta={"_answer_label": ""}),
    )
    pending = model.append(
        OutboxMessage(kind="intervention", text="…", meta={}),
    )

    rendered_resolved = gutter.decorate(resolved_empty, width=_GUTTER_WIDTH, height=1)
    rendered_pending = gutter.decorate(pending, width=_GUTTER_WIDTH, height=1)

    assert (
        rendered_resolved.plain != rendered_pending.plain
        or rendered_resolved.style != rendered_pending.style
    ), (
        f"an empty-label resolved intervention must render distinctly from "
        f"a genuinely pending one; both got "
        f"({rendered_resolved.plain!r}, {rendered_resolved.style!r})"
    )
    assert "⋯" in rendered_pending.plain, rendered_pending.plain
    assert "⋯" not in rendered_resolved.plain, (
        "empty _answer_label read as pending — the emptiness-vs-absence "
        "trap this test exists to catch"
    )


@pytest.mark.asyncio
async def test_genuine_pending_intervention_still_opens_the_panel():
    """Tier 2: accept-side — a genuinely LIVE pending intervention (no
    ``RESTORED_META_KEY``) still registers and opens the panel exactly as
    before this fix; an "always skip registration" implementation would
    pass the strip-falsifiers above vacuously without this."""
    transport = _ReplayTransport([_genuine_pending_frame()])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is True, (
            "a genuine pending intervention must still register and open "
            "the panel"
        )
        assert panel.has_pending() is True
