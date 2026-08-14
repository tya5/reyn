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
auto-folded. flowview's own vim z-prefix (``za``, no reyn-side binding
needed) reaches a Group parent's fold; Space does NOT yet (owner-reported,
#4775 — separately scoped, its own test lives in that PR, not this file
until #4775 lands).

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
from reyn.interfaces.transport.client_transport import ClientTransport
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


class QueueTransport(ClientTransport):
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
        # CHANGELOG) — 2 entries here means "1 parent + its 1 child", not
        # "2 unrelated top-level rows". depth/parent below is what actually
        # proves the nesting.
        # Unpacking (not a bare len() check) pins "exactly 1 parent + 1
        # nested child" the same way a count would, but reads as a
        # structural shape check.
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
async def test_default_stays_fully_expanded() -> None:
    """Tier 2b: owner ruling (#4691) — B1 wires the fold keys but the
    DEFAULT is unchanged: neither the parent nor its children are ever
    auto-collapsed by construction."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        assert parent.collapsed is False
        (child,) = parent.children
        assert child.collapsed is False


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
    """Tier 2b: accept-side pair — the count line is collapsed-only; an
    ordinary EXPANDED parent (today's default, and B1's own "zero visual
    regression" promise) renders exactly as it did before this fix."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", text="checking"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        parent = _entries(app)[0]
        assert parent.collapsed is False
        pres = await app._presenter.present(parent, 80)
        from rich.console import Console

        console = Console(width=80)
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

        parent, child = _entries(app)
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
        _, child = _entries(app)
        assert child.parent is entry, (
            "registration is unconditional now — a terminal reply's "
            "call_id still registers (harmless, nothing ever looks it "
            "up in production; a real caller with unused entries pays "
            "only dict growth, tracked separately by #4776)"
        )
