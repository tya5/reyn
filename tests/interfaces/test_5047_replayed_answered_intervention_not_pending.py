"""Tier 2: #5047/#5057 — a REPLAYED, already-answered intervention frame must
not be registered as pending.

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

**Two guards were tried and retired before the settled mechanism below**:

1. A first draft guarded on ``meta["_answer_label"]`` being truthy — but a
   restored frame's ``_answer_label`` can itself be the EMPTY STRING
   (``restore.py``'s own ``meta.get(INTERVENTION_ANSWER_META_KEY, "")``), so
   a falsy-VALUE check let an empty-label restored frame slip through and
   register as pending anyway (the #4996-family "the value's own absence
   doesn't distinguish two different reasons" conflation, here on
   emptiness rather than None).
2. #5056/#5060 fixed that by checking marker PRESENCE instead
   (``RESTORED_META_KEY`` / ``meta.get("intervention_id")``) — correct for
   the producer population known at the time, but producer-specific: #5057
   found TWO more producers (``stream_client.py``'s and `app.py`'s own
   ``/rewind`` text-list fallback) that reused ``kind="intervention"`` with
   neither marker, which the guard never covered, AND #5057 also measured
   that once axis A (below) requires every ``kind="intervention"`` frame to
   carry a real ``intervention_id``, ``restore.py``'s answered projection
   would ALSO come to carry one (``deliver_answer_to`` already stamps it on
   the history entry) — silently reopening the exact hole #5056 closed
   (issuecomment-5378183009, found independently from the panel side —
   ``InterventionPanel.add_pending`` has zero ``_answer_label`` awareness —
   while architect found the same collision from the ingest side).

**The settled mechanism (axis A + axis B, architect's confirmed design,
same PR)**: axis A (``OutboxMessage.__post_init__`` / ``.from_wire``,
``outbox.py``) makes ``meta["intervention_id"]`` a genuine constructor-time
requirement for ``kind="intervention"`` — a producer with no real identity
(the two ``/rewind`` fallbacks) can no longer build that kind at all, so
they build ``kind="system"`` instead. Axis B gives an ALREADY-ANSWERED
frame its OWN sibling kind, ``"intervention_resolved"`` — NOT in axis A's
identity-required family (a resolved frame is never answered again, so it
needs no correlation anchor) — so ``restore.py``'s projection (and the two
live-answer fold sites, ``TextualChatApp._resolve_intervention`` /
``_handle_intervention_answer_event``) build/fold to THAT kind instead of
``"intervention"``. ``app.py``'s ``_ingest_frame`` registration guard is
then a bare ``kind == "intervention"`` check with NO meta inspected at all
— structurally unable to register a resolved frame, because a resolved
frame is never that kind. This is not a marker anyone could add a THIRD
producer without (axis A/B are enforced at ``OutboxMessage`` construction
itself, not at the ingest call site), closing the class of bug rather than
its 3 known instances.

This test's own witness is the fake-pending REGISTRATION itself (the panel
never opening), not the rendered text (the presenter's RESOLVED branch
renders identically for both kinds it recognizes — see
``ReynPresenter._present_intervention_pending``'s own docstring for what
changed inside it).

Real ``TextualChatApp`` + a real, minimal ``ClientTransport`` (no mocks).
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

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


def _restored_answered_frame(
    *, answer_label: str, intervention_id: "str | None" = "iv-restored",
) -> OutboxMessage:
    """Shaped EXACTLY like ``restore.py``'s ``project_restored_frames`` own
    projection of an already-answered history entry (#5057 axis B):
    ``kind="intervention_resolved"``, ``RESTORED_META_KEY: True``.
    ``intervention_id`` defaults to a real value (the common case —
    ``deliver_answer_to`` stamps it on the history entry) but can be
    ``None`` too (a record from before that stamping existed) — axis B's
    whole point is that BOTH shapes are equally excluded from pending
    registration, since neither is ``kind="intervention"``."""
    return OutboxMessage(
        kind="intervention_resolved",
        text="Allow fetching from 'news.ycombinator.com'?",
        meta={
            RESTORED_META_KEY: True,
            "prompt": "Allow fetching from 'news.ycombinator.com'?",
            "detail": None,
            "_answer_label": answer_label,
            "intervention_id": intervention_id,
        },
    )


def _genuine_pending_frame() -> OutboxMessage:
    """A real LIVE pending intervention — ``kind="intervention"`` (axis A
    requires the real ``intervention_id`` below at construction time), no
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
    answered intervention must never open the panel — reverting
    ``restore.py``'s projection (or the two live-answer fold sites) back to
    ``kind="intervention"`` instead of ``"intervention_resolved"`` turns
    this red (``panel.display`` becomes True for a frame that was never
    actually pending), because ``_ingest_frame``'s bare ``kind ==
    "intervention"`` guard would then register it."""
    transport = _ReplayTransport(
        [_restored_answered_frame(answer_label="Always")]
    )
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
    """Tier 2: the falsifying edge case that originally moved the
    discriminator away from ``_answer_label`` truthiness, now covered
    structurally instead of by a meta check. An empty-string
    ``_answer_label`` (a real shape ``restore.py`` can produce —
    ``meta.get(INTERVENTION_ANSWER_META_KEY, "")``) must still be excluded
    from pending registration — the guard no longer inspects
    ``_answer_label`` at all (kind alone decides), so this can no longer
    regress the way a falsy-VALUE meta check once did."""
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


@pytest.mark.asyncio
async def test_replayed_answered_intervention_with_no_id_still_excluded():
    """Tier 2: a history record from BEFORE ``deliver_answer_to`` started
    stamping ``intervention_id`` (or any other reason the id is absent)
    projects with ``intervention_id: None`` — axis B does not require
    identity for ``kind="intervention_resolved"`` (it is never answered
    again), so this must be excluded from pending registration exactly
    like the identified case above. Strip-falsifier for axis B's own
    "identity not required" half: a regression that made
    ``intervention_resolved`` construction REQUIRE an id would make this
    fixture itself raise, not merely fail the assertion."""
    transport = _ReplayTransport(
        [_restored_answered_frame(answer_label="Always", intervention_id=None)]
    )
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        panel = app.query_one(InterventionPanel)
        assert panel.display is False
        assert panel.has_pending() is False


@pytest.mark.asyncio
async def test_wire_decoded_idless_intervention_frame_is_demoted_never_pending():
    """Tier 2: witness ④ — axis A's WIRE-side defense (block on PR #5082,
    docs-maintainer TESTS-READ + lead-coder: "the wire-side demotion has no
    witness anywhere in this diff" — `test_agui_mapping_completeness.py`'s
    own comment pointed at a test that does not cover this, and
    `test_outbox_vocabulary.py` only covers the UNRELATED ignore-unknown
    path).

    ``in-process`` construction is guarded by ``OutboxMessage.__post_init__``
    (axis A) — but a REMOTE reconnect-backlog frame is UNTRUSTED wire data
    (``agui/protocol.py:decode_event``'s ``"messages"``/``"display"``
    branches, both routed through ``OutboxMessage.from_wire``, which cannot
    fail-close) — the EXACT path #5047's own real-environment bug rode
    through. This test decodes a wire event shaped exactly like a remote
    server's reconnect-backlog entry for a KNOWN intervention-family kind
    with NO ``intervention_id`` (a malicious or simply out-of-date peer),
    through the REAL ``decode_event`` entry point (not calling
    ``from_wire`` directly), and proves BOTH halves of the "never
    fail-close, never register as pending" contract: the frame is DRAWN
    (never silently dropped — "no exception was raised" is not the claim
    here), AND the panel never opens for it.

    Strip-falsifier: removing the demotion clause in
    ``OutboxMessage.from_wire`` (verified locally) turns this red twice
    over — the frame's kind reverts to the raw wire value ``"intervention"``
    (no longer demoted) and ``panel.has_pending()`` becomes ``True``
    (registers as a fake-pending entry with no real id, #5047's own
    original bug, reintroduced via the wire path this test drives)."""
    from reyn.interfaces.transport.agui.protocol import decode_event

    decoded = decode_event(
        "CUSTOM",
        {
            "_reyn": {
                "frame": "display",
                "kind": "intervention",
                "text": "Allow fetching from 'evil.example'?",
                "meta": {},  # no intervention_id -- the untrusted-peer shape
            },
        },
    )
    assert decoded is not None, "a known intervention-family kind must decode, not ignore-unknown"
    wire_msg = decoded.message
    # The demotion itself: a KNOWN kind with no identity is never allowed to
    # reach the app as "intervention" — draws as ordinary chrome instead.
    assert wire_msg.kind == "system", (
        f"expected the wire decode to demote an id-less 'intervention' frame "
        f"to 'system'; got kind={wire_msg.kind!r}"
    )

    transport = _ReplayTransport([wire_msg])
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # Drawn, not dropped: the frame reached the flow at all.
        flow_kinds = [e.item.kind for e in app.query_one(FlowView).entries]
        assert "system" in flow_kinds, (
            f"the demoted frame was never rendered at all — expected it to "
            f"draw as ordinary chrome, got flow kinds={flow_kinds!r}"
        )

        panel = app.query_one(InterventionPanel)
        assert panel.has_pending() is False, (
            "a wire-decoded, id-less intervention-family frame registered as "
            "pending — the exact remote path #5047's own bug rode through"
        )


def test_gutter_reads_the_resolved_kind_as_resolved_not_pending():
    """Tier 2: strip-falsifier for the sibling ``gutter.py`` fix (#5057
    axis B). A restored/resolved intervention — ``kind=
    "intervention_resolved"``, regardless of its ``_answer_label`` value —
    must get the RESOLVED (dim-glyph, ``_CC_DONE``-toned) gutter render,
    never the PENDING one — reverting ``ReynGutter``'s branch back to
    reading ``_answer_label`` meta (rather than dispatching on kind) is
    exactly the class of regression this test would catch if that read
    were reintroduced with the old falsy-VALUE bug: an empty
    ``_answer_label`` NO LONGER matters here at all — the kind alone
    settles it.

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
        OutboxMessage(
            kind="intervention_resolved", text="…", meta={"_answer_label": ""},
        ),
    )
    pending = model.append(
        OutboxMessage(
            kind="intervention", text="…", meta={"intervention_id": "iv-live"},
        ),
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
        "a resolved-kind intervention rendered as pending"
    )


@pytest.mark.asyncio
async def test_genuine_pending_intervention_still_opens_the_panel():
    """Tier 2: accept-side — a genuinely LIVE pending intervention
    (``kind="intervention"``) still registers and opens the panel exactly
    as before this fix; an "always skip registration" implementation would
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
