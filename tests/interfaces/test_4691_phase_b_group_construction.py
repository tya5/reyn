"""#4691 Phase B (B1): the litellm-call TREE — a tool-calls round's own
``agent`` row becomes the flowview Group PARENT its ``tool_call_started``/
``completed``/``failed`` rows nest under, found by ``call_id`` KEY LOOKUP
(never dispatch order — owner ruling B, #4691, via #4734's review of an
earlier rejected pointer-based design). ③ (the parent placeholder always
emits, even content-less) and ④ (the parent's own RUNNING→settled spinner,
aggregated from its children) are this Group construction's direct
consumers — the reason both were deferred to Phase B in the first place.

Default stays fully EXPANDED (owner ruling, #4691) — B1 itself never calls
``.collapse()``, so this file also pins that a freshly built Group is never
auto-folded. Two fold paths reach a Group parent: flowview's own vim
z-prefix (``za``, no reyn-side binding needed) and, since #4775 (owner-
reported: Space was silently inert on a Group parent — the documented/
expected trigger, not the z-prefix sequence a typical user wouldn't know
about), Space itself.

Registration (and therefore the whole Group construction firing at all)
is provider-independent since #4777 — keyed on ``dispatched_tool_calls``
(the LLM result's own ``tool_calls`` list), never a provider's
self-reported ``finish_reason`` string.

All use a real, mounted :class:`TextualChatApp`, real
:class:`~reyn.runtime.outbox.OutboxMessage` / EventFrame, and a real
:class:`~reyn.schemas.models.Event` — no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import EntryState, FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.chrome import Composer
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
from reyn.runtime.outbox import OutboxMessage
from reyn.schemas.models import Event


def _parent_row(
    call_id: str,
    *,
    text: str = "",
    finish_reason: "str | None" = "tool_calls",
    dispatched_tool_calls: bool = True,
) -> OutboxMessage:
    """The tool-turn-text row (#4691 ③'s own placeholder) — a real call
    boundary, carrying the SAME call_id/finish_reason/dispatched_tool_calls
    shape router_loop.py stamps (#4691 Phase 1 ①②, #4777). Defaults match a
    provider that DOES report ``finish_reason == "tool_calls"`` correctly;
    ``finish_reason`` is overridable so a test can pin #4777's own claim —
    registration/spinner keyed on ``dispatched_tool_calls`` (a REYN-OBSERVED
    fact) survives a provider that never reports it (#4777, owner-observed:
    ``finish_reason`` stayed "stop" on every call, provider-side)."""
    return OutboxMessage(
        kind="agent",
        text=text,
        meta={
            "chain_id": "chain-test",
            "source": "router_tool_turn_text",
            "call_id": call_id,
            "finish_reason": finish_reason,
            "dispatched_tool_calls": dispatched_tool_calls,
            "prompt_tokens": 100,
            "completion_tokens": 5,
        },
    )


def _started(op_id: str, call_id: "str | None", tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started",
        text=tool,
        meta={"tool": tool, "op_id": op_id, "args": {}, "call_id": call_id},
    )


def _completed(op_id: str, call_id: "str | None", tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_completed",
        text="",
        meta={
            "tool": tool, "op_id": op_id, "call_id": call_id,
            "result": {"op": tool, "count": 3},
        },
    )


def _failed(op_id: str, call_id: "str | None", tool: str = "grep") -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_failed",
        text=tool,
        meta={
            "tool": tool, "op_id": op_id, "call_id": call_id,
            "error_kind": "Boom", "error_message": "it broke",
        },
    )


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue — display OR event frames — so a test can push frames one at a
    time and inspect the tree between each (same shape as #72's own
    ``QueueTransport``, kept local to this file since neither test module
    exports its collaborator)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def push_event(self, event_type: str) -> None:
        await self._queue.put(EventFrame(Event(type=event_type)))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str) -> None:  # pragma: no cover
        pass

    async def answer_intervention_text(self, text: str) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:  # pragma: no cover
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


def _entries(app: TextualChatApp):
    return app.query_one(FlowView).entries


def _painted(flow: FlowView) -> str:
    return "\n".join(
        "".join(seg.text for seg in flow.render_line(y))
        for y in range(flow.size.height)
    )


@pytest.mark.asyncio
async def test_tool_rows_nest_under_the_matching_call_id_parent() -> None:
    """Tier 2b: a tool_call_started/completed pair carrying the SAME call_id
    as an already-landed parent row nests as that parent's CHILD (Entry.parent
    is the parent row, Entry.children holds it) — not a second top-level row."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        # FlowView.entries is the full document-order traversal (each
        # parent immediately followed by its subtree, per flowview's own
        # CHANGELOG) — but only while EXPANDED; a collapsed entry's
        # subtree is excluded from the flat traversal entirely, and the
        # parent is collapsed by default now (arc item 3) from its first
        # child onward. Expand it first so 2 entries here means "1 parent
        # + its 1 child", not "2 unrelated top-level rows" or "1 entry,
        # subtree hidden". depth/parent below is what actually proves the
        # nesting, once both are actually visible to unpack.
        # Unpacking (not a bare len() check) pins "exactly 1 parent + 1
        # nested child" the same way a count would, but reads as a
        # structural shape check.
        (parent,) = _entries(app)
        assert parent.collapsed is True, "setup: defaults collapsed (arc item 3)"
        parent.expand()
        parent, child = _entries(app)
        assert parent.item.meta.get("call_id") == "resp-1"
        assert parent.depth == 0
        assert child.item.kind == "tool_call_started"
        assert child.depth == 1
        assert child.parent is parent
        assert parent.children == (child,)


@pytest.mark.asyncio
async def test_a_row_with_no_call_id_still_lands_flat() -> None:
    """Tier 2b: regression guard — a tool row carrying NO call_id (a legacy/
    restored frame, or an op-loop caller that never threaded one through)
    lands top-level exactly as it did before B1, never mis-nested under an
    unrelated parent."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id=None))
        await pilot.pause()

        # Unpacking pins "exactly 2 unrelated top-level rows" — the
        # structural shape a bare count would only approximate.
        first_row, second_row = _entries(app)
        assert second_row.item.kind == "tool_call_started"
        assert second_row.parent is None


@pytest.mark.asyncio
async def test_a_tool_row_with_an_unregistered_call_id_lands_flat() -> None:
    """Tier 2b: a call_id that matches NO registered parent (the parent row
    hasn't arrived yet, or never will) falls through to the flat top-level
    append — never raises, never silently drops the row."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-never-registered"))
        await pilot.pause()

        top_level = _entries(app)
        (only,) = top_level
        assert only.item.kind == "tool_call_started"
        assert only.parent is None


@pytest.mark.asyncio
async def test_parent_starts_running_and_settles_success_once_children_complete() -> None:
    """Tier 2b: #4691 Phase B ④ — the parent goes RUNNING the instant it
    lands (children about to arrive), stays RUNNING while any child is
    still in flight, and settles to SUCCESS only once every child has."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        parent = _entries(app)[0]
        assert parent.state is EntryState.RUNNING

        await transport.push_display(_started("op-1", call_id="resp-1"))
        await transport.push_display(_started("op-2", call_id="resp-1"))
        await pilot.pause()
        assert parent.state is EntryState.RUNNING, (
            "still RUNNING — both children are still in flight"
        )

        await transport.push_display(_completed("op-1", call_id="resp-1"))
        await pilot.pause()
        assert parent.state is EntryState.RUNNING, (
            "still RUNNING — one child (op-2) has not settled yet"
        )

        await transport.push_display(_completed("op-2", call_id="resp-1"))
        await pilot.pause()
        assert parent.state is EntryState.SUCCESS, (
            "every child settled clean — the parent must settle too"
        )


@pytest.mark.asyncio
async def test_one_failed_child_taints_the_parent_to_error() -> None:
    """Tier 2b: #4691 Phase B ④ — a single failed child taints the WHOLE
    parent to ERROR, even when its sibling succeeded — a reader scanning
    collapsed parent rows must see the failure without expanding."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        parent = _entries(app)[0]

        await transport.push_display(_started("op-1", call_id="resp-1"))
        await transport.push_display(_started("op-2", call_id="resp-1"))
        await pilot.pause()
        await transport.push_display(_completed("op-1", call_id="resp-1"))
        await pilot.pause()
        await transport.push_display(_failed("op-2", call_id="resp-1"))
        await pilot.pause()

        assert parent.state is EntryState.ERROR


@pytest.mark.asyncio
async def test_an_orphaned_call_parent_settles_cancelled_not_stuck_running() -> None:
    """Tier 2b: #4691 Phase B ④ — a parent whose children never arrived at
    all (every tool_call excluded pre-dispatch, or the turn cancelled before
    any started frame reached the TUI) must not spin RUNNING forever once
    the turn ends — same CANCELLED verdict #72 gives an orphaned tool
    itself, applied here at parent granularity."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        parent = _entries(app)[0]
        assert parent.state is EntryState.RUNNING

        await transport.push_event("turn_settled")
        await pilot.pause()

        assert parent.state is EntryState.CANCELLED


@pytest.mark.asyncio
async def test_the_parent_has_no_reactive_watcher_on_its_children() -> None:
    """Tier 2b: #4748 review (lead-coder) — this is NOT ④'s non-vacuity
    witness (that is the neighboring
    ``test_parent_starts_running_and_settles_success_once_children_complete``,
    which genuinely fails if ``_recompute_parent_state`` is removed — this
    test does not, since it never calls the real settle path at all, so its
    own earlier docstring calling it one was wrong).

    What this DOES pin: ``_recompute_parent_state`` is called explicitly
    from app's own settle paths (``_coalesce_tool_result``, the #72 orphan
    sweep) — the parent is NOT a reactive/observed property that
    re-evaluates itself whenever ANY child's state changes by whatever
    means. A child mutated entirely outside app's own machinery (as this
    test does, directly on the Entry) leaves the parent exactly as it was
    — no polling, no watcher, no hidden recomputation trigger other than
    the two call sites #4691 Phase B ④ actually wired."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        parent = _entries(app)[0]

        # Directly settle the child WITHOUT going through the real
        # coalesce path (which calls _recompute_parent_state) — a child
        # created and settled entirely OUTSIDE app's own machinery.
        child = parent.append_child(_started("op-1", call_id="resp-1"))
        child.set_state(EntryState.SUCCESS)
        await pilot.pause()

        assert parent.state is EntryState.RUNNING, (
            "the parent must not have re-evaluated itself — recompute only "
            "runs from the two explicit call sites, never as a side effect "
            "of a child's state changing by any other means"
        )


@pytest.mark.asyncio
async def test_a_group_parent_defaults_collapsed() -> None:
    """Tier 2b: owner ruling (#4691 arc item 3) — a completion Group starts
    COLLAPSED, not expanded-then-folded. Collapse is asserted at
    REGISTRATION time — while the parent is still a leaf, no child has
    landed yet — since textual-flowview 0.22.0's #14 fix: ``.collapse()``
    now records the state on any live entry (leaf included) rather than
    silently discarding it, and a child appended later "walks its
    ancestors and is born folded" (0.22.0 release notes). Before 0.22.0
    this was a documented no-op on a leaf, which forced reyn to watch its
    own ``append_child`` and re-assert collapse there, guarded to fire
    exactly once — that workaround is gone; upstream now does the whole
    job from one call. A CHILD's own detail-expand state (the #3508/#4697
    Space-toggle axis, unrelated to Group fold) is untouched — only the
    top-level Group fold defaults to collapsed."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        parent = _entries(app)[0]
        assert parent.collapsed is True, (
            "collapse is asserted at registration — before 0.22.0 this "
            "had to wait for the first child (a leaf-collapse no-op); "
            "now it fires immediately"
        )

        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()
        assert parent.collapsed is True, (
            "still collapsed once the first child actually lands"
        )
        (child,) = parent.children
        assert child.collapsed is False, (
            "the CHILD's own fold state is untouched — only the parent "
            "Group defaults to collapsed"
        )

        # A reader who opens it by hand (za/Space) must not be re-folded
        # when a SECOND child lands — reyn's own collapse call fires
        # exactly once, at registration, and never again.
        parent.expand()
        await transport.push_display(_started("op-2", call_id="resp-1"))
        await pilot.pause()
        assert parent.collapsed is False, (
            "a manually-reopened parent must stay open when a later "
            "child arrives — reyn never re-asserts collapse after "
            "registration"
        )


@pytest.mark.asyncio
async def test_a_terminal_reply_never_starts_collapsed() -> None:
    """Tier 2b: accept-side pair — a call that dispatches no tools
    (dispatched_tool_calls False, #4777) registers (harmless) but is never
    collapsed either — there is nothing behind it to hide, and collapsing an
    ordinary conversational reply would be a visible regression no owner
    ruling asked for."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _parent_row("resp-1", finish_reason="stop", dispatched_tool_calls=False)
        )
        await pilot.pause()

        (entry,) = _entries(app)
        assert entry.collapsed is False


@pytest.mark.asyncio
async def test_a_collapsed_parent_shows_its_child_count() -> None:
    """Tier 2b: dogfood finding (#4691, post-#4748 merge) — a collapsed
    Group parent must show its child COUNT, or folding hides both the
    children AND any sign that anything was hidden at all (worse than not
    being able to fold — a reader cannot tell "nothing here" from "3 rows
    folded away"). Deliberately minimal: pins that the count is present
    and correct, not any particular wording/symbol (design is #4691 Phase
    B B2's own call, per lead-coder's explicit review scope)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await transport.push_display(_started("op-2", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        flow = app.query_one(FlowView)

        parent.collapse()
        await pilot.pause()
        collapsed_text = _painted(flow)
        assert "2" in collapsed_text, (
            f"a collapsed parent with 2 children must show the count "
            f"somewhere on its own row, got: {collapsed_text!r}"
        )


@pytest.mark.asyncio
async def test_an_expanded_parent_shows_no_count_line() -> None:
    """Tier 2b: accept-side pair — the count line is collapsed-only. A
    Group parent now defaults collapsed (arc item 3, #4691), so this
    explicitly EXPANDS it first (the owner's own za/Space, #4775) to prove
    the count line disappears once opened — B1's own "zero visual
    regression on the expanded body" promise still holds for that state,
    it's just no longer the default one lands on."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", text="checking"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        assert parent.collapsed is True, "setup: defaults collapsed now (arc item 3)"
        parent.expand()
        assert parent.collapsed is False
        pres = await app._presenter.present(parent, 80)
        from rich.console import Console

        # no_color=True: an expanded parent recedes (#4691 arc item ⑤) via
        # a real ANSI colour escape now — a colour-enabled capture would
        # embed digits from the escape sequence itself (e.g. the "1" in
        # "38;2;107;114;128") into the plain-text substring check below,
        # a false positive unrelated to this test's own subject.
        console = Console(width=80, no_color=True)
        with console.capture() as cap:
            console.print(pres.renderable)
        text = cap.get()
        assert "folded" not in text and "1" not in text


@pytest.mark.asyncio
async def test_group_construction_survives_a_provider_that_never_reports_tool_calls() -> None:
    """Tier 2b: #4777 (owner-reported, provider-dependence bug) — the OWNER's
    own live turns never had ``finish_reason == "tool_calls"`` on the parent
    row (their provider reports every response as ``"stop"``, tool calls or
    not), so B1's entire Group construction was silently inert on their
    screen despite being green here — every existing test in this file used
    a provider shape (``finish_reason == "tool_calls"``) that happened to
    always be true. This is the ONE test in the suite that pins the
    provider-INDEPENDENT claim directly: registration and the RUNNING
    spinner both survive ``finish_reason="stop"`` as long as
    ``dispatched_tool_calls`` (the REYN-OBSERVED fact, from the LLM result's
    own ``tool_calls`` list, not the provider's self-reported string) is
    true."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _parent_row("resp-1", finish_reason="stop", dispatched_tool_calls=True)
        )
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        # Read nesting off Entry.children/.parent directly, not
        # FlowView.entries's flat traversal — the parent is collapsed by
        # default from its first child onward (arc item 3), and a
        # collapsed entry's subtree is excluded from that traversal.
        (parent,) = _entries(app)
        (child,) = parent.children
        assert child.parent is parent, (
            "a call whose OWN parent row was never labeled "
            '\'finish_reason == "tool_calls"\' by the provider must still '
            "nest its children — the registration fact is reyn's own "
            "observation (dispatched_tool_calls), never the provider string"
        )
        assert parent.state is EntryState.RUNNING, (
            "the spinner must still start — gated on dispatched_tool_calls, "
            "not on finish_reason"
        )


@pytest.mark.asyncio
async def test_a_terminal_reply_with_a_call_id_registers_but_never_spins() -> None:
    """Tier 2b: accept-side pair — #4777's registration is now UNCONDITIONAL
    for any call_id-bearing agent row (harmless per the code's own comment:
    nothing ever looks up a call_id belonging to a call that dispatched no
    tools), but the spinner stays gated on dispatched_tool_calls. An ordinary
    terminal reply (no tools dispatched) must register without ever
    spinning RUNNING — the accept side of the same fix.

    Registration is proved through its only PUBLIC effect (a later same-
    call_id tool row actually nesting as a child), not by reading the
    private ``_call_parents`` dict directly (testing.md Tier 4)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(
            _parent_row("resp-1", finish_reason="stop", dispatched_tool_calls=False)
        )
        await pilot.pause()

        (entry,) = _entries(app)
        assert entry.state is not EntryState.RUNNING, (
            "a call that dispatched no tools must never show a spinner"
        )

        # A same-call_id tool row would never nest at all if registration
        # were skipped — this is unrealistic in production (a terminal
        # reply's call never actually dispatches a tool), but it is the
        # only way to observe "did registration happen" through the
        # public surface rather than the private dict.
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()
        # Entry.children directly, not FlowView.entries's flat traversal —
        # collapse-on-first-child (arc item 3) fires here too, hiding the
        # subtree from that traversal.
        (child,) = entry.children
        assert child.parent is entry, (
            "registration is unconditional now — a terminal reply's "
            "call_id still registers (harmless, nothing ever looks it "
            "up in production; a real caller with unused entries pays "
            "only dict growth, tracked separately by #4776)"
        )


async def _focus_flow(pilot, app: TextualChatApp) -> FlowView:
    app.query_one(Composer).focus()
    await pilot.pause()
    await pilot.press("shift+tab")
    await pilot.pause()
    return app.query_one(FlowView)


@pytest.mark.asyncio
async def test_space_folds_a_group_parent() -> None:
    """Tier 2b: #4775 (owner-reported, live TUI) — Space, the owner's
    documented/expected fold trigger, used to be silently inert on a Group
    parent: ``on_flow_view_toggle_fold_requested`` early-returns for any
    entry whose meta lacks ``_RESULT_KIND_KEY``, which a Group parent's
    placeholder row (#4691 ③) always does. flowview's own ``za`` z-prefix
    key sequence already reached ``toggle_fold()`` (a SEPARATE key path,
    #4691's own doc), so a route existed — just not the one the owner
    actually pressed. This drives the REAL Space key through the app
    (not a direct ``.collapse()`` call, which #4750's own test already
    covers as a presentation-only concern) to falsify the wiring gap
    itself, not just the presenter's rendering of an already-collapsed
    state."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await transport.push_display(_started("op-2", call_id="resp-1"))
        await pilot.pause()

        # The parent defaults collapsed from its first child onward (arc
        # item 3) — it is therefore the ONLY entry FlowView.entries's flat
        # traversal exposes (children are hidden while folded), so arrival
        # highlight lands directly on it; no up/down navigation needed.
        (parent,) = _entries(app)
        assert parent.collapsed is True, "setup: defaults collapsed (arc item 3)"
        flow = await _focus_flow(pilot, app)
        assert flow.current is parent, "setup: highlight must be on the parent"

        await pilot.press("space")
        while parent.collapsed is True:
            await pilot.pause()
        assert parent.collapsed is False, (
            "Space on a highlighted Group parent must unfold it — the "
            "owner's own reported, expected trigger"
        )

        # Toggle: pressing Space again folds it back.
        await pilot.press("space")
        while parent.collapsed is False:
            await pilot.pause()
        assert parent.collapsed is True


@pytest.mark.asyncio
async def test_an_expanded_group_parent_recedes() -> None:
    """Tier 2b: #4691 arc item ⑤ (owner ruling — "B で良いよ" + "親に弱い印
    もそうだね") — a Group parent's own line dims while EXPANDED (children
    visible), so the reader's eye lands on the tool rows carrying the
    actual content. Value comes from ``palette.TOKENS["@recede@"]``
    (``"dim"``, an SGR attribute with its own measured justification,
    #3522/#3528) — CLAUDE.md's TUI colour policy requires every value here
    resolve through a ``palette.py`` token, never a literal."""
    from rich.styled import Styled

    from reyn.interfaces.palette import TOKENS

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        parent.expand()
        assert parent.collapsed is False, "setup: expanded"
        assert parent.children, "setup: has a child"

        pres = await app._presenter.present(parent, 80)
        assert isinstance(pres.renderable, Styled), (
            "an expanded Group parent's body must be wrapped to recede"
        )
        assert pres.renderable.style == TOKENS["@recede@"]


@pytest.mark.asyncio
async def test_a_collapsed_group_parent_does_not_also_recede() -> None:
    """Tier 2b: accept-side pair — a COLLAPSED parent already recedes via a
    different mechanism (the "(N folded)" count line, #4750) and is
    excluded from item ⑤'s own Styled-wrap branch — the two are distinct
    reasons to dim, not doubled up on the same row."""
    from rich.styled import Styled

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        assert parent.collapsed is True, "setup: defaults collapsed (arc item 3)"

        pres = await app._presenter.present(parent, 80)
        assert not isinstance(pres.renderable, Styled), (
            "a collapsed parent's body is a Group (body + count line), "
            "not item ⑤'s Styled-wrap — the two recede-reasons are "
            "mutually exclusive by construction"
        )


@pytest.mark.asyncio
async def test_a_leaf_row_never_recedes() -> None:
    """Tier 2b: accept-side pair — a row with no children at all (an
    ordinary tool row, or a Group parent before its first child lands) is
    untouched by item ⑤'s Styled-wrap — receding is a Group-parent-only
    concept, never applied to a leaf."""
    from rich.styled import Styled

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        parent.expand()
        (child,) = parent.children

        pres = await app._presenter.present(child, 80)
        assert not isinstance(pres.renderable, Styled)
