"""#4691 arc item ① (the final item) — the TURN Group: a turn's own
completion Groups nest under the turn's ``kind="user"`` row (owner's
structure: turn Group > completion Group > tool execution, #4691 issue
comment thread). Reuses the EXACT turn-boundary signals
``_handle_turn_started_event``/``_TURN_END_EVENT_TYPES`` already fire for
the sent-queue promotion and the #72 orphan sweeps — architect's own
finding, "no new surface needed — turn boundaries already exist on both
sides, already consumed elsewhere" — so this file drives those same real
events rather than inventing a new one.

Owner rulings pinned here:
  既定      turn Group = open / completion Group = hide — OPPOSITE
            defaults. #4781's own completion-level collapse-on-first-child
            must NOT also fire for the turn level, or the whole turn would
            fold away by default, which is the regression the ruling
            forbids.
  進行表示  the turn's own parent (the user row) stays RUNNING for the
            WHOLE turn, settling only at the turn's own end — never
            DERIVED incrementally from its completion-Group children the
            way a completion Group's own state is (that would flicker the
            turn parent to SUCCESS between calls, while the turn itself is
            still in flight).

Two pitfalls the co-vet review named for this item, both covered here:
  ①session-switch clearing — :attr:`_current_turn_parent` is a single
   ``Entry | None``, not a dict, so it cannot ride
   ``_PER_SESSION_DICT_STATE``'s uniform ``.clear()`` loop; it needs (and
   got) an explicit reset at the session-switch site
   (:meth:`TextualChatApp._handle_session_attached_event`) — the SAME
   omission class as #4776's own dict-shaped one, just a field this shape
   could never even have joined that tuple by inclusion.
  ②rows outside a turn stay flat — not a gate this file enforces (an
   owner-visual judgment call, named in the PR body, not decided here);
   the "no turn currently open" half of it falls out for free
   (:attr:`_current_turn_parent` is ``None`` whenever no turn is open) and
   is witnessed here as a boundary case.

Real ``TextualChatApp``/``FlowView``/``Event`` — no mocks, per the testing
policy; mirrors ``test_4691_phase_b_group_construction.py``'s own
collaborator choice for the call-level Group, and
``test_agent_delta_no_visible_garbage_3288.py``'s proven
``user_submitted``+``turn_started`` promote-witness sequence for driving a
turn open.
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
    call_id: str, *, text: str = "", dispatched_tool_calls: bool = True,
) -> OutboxMessage:
    """A completion Group's own placeholder row — the SAME shape
    ``test_4691_phase_b_group_construction.py``'s own ``_parent_row`` uses,
    kept local per that file's own convention (neither module exports its
    collaborators)."""
    return OutboxMessage(
        kind="agent",
        text=text,
        meta={
            "chain_id": "chain-test",
            "source": "router_tool_turn_text",
            "call_id": call_id,
            "finish_reason": "tool_calls" if dispatched_tool_calls else "stop",
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


def _user_submitted(*, msg_id: str, chain_id: str, text: str, seq: int) -> Event:
    return Event(
        type="user_submitted",
        data={"text": text, "chain_id": chain_id, "msg_id": msg_id, "seq": seq, "meta": {}},
    )


def _turn_started(*, chain_id: str, seq: int) -> Event:
    return Event(type="turn_started", data={"kind": "user", "chain_id": chain_id, "seq": seq})


def _turn_settled() -> Event:
    return Event(type="turn_settled", data={})


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from
    a queue — display OR event frames (same shape as
    ``test_4691_phase_b_group_construction.py``'s own ``QueueTransport``,
    kept local since neither test module exports its collaborator)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    async def push_event(self, event: Event) -> None:
        await self._queue.put(EventFrame(event))

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
    return list(app.query_one(FlowView).entries)


async def _open_turn(
    transport: QueueTransport, pilot, *, chain_id: str, text: str = "hi", seq_base: int = 1,
) -> None:
    """Drive a real ``user_submitted``+``turn_started`` pair to promotion —
    the proven promote sequence
    (``test_agent_delta_no_visible_garbage_3288.py``'s own arrival witness)."""
    await transport.push_event(
        _user_submitted(msg_id=f"m-{chain_id}", chain_id=chain_id, text=text, seq=seq_base)
    )
    await pilot.pause()
    await transport.push_event(_turn_started(chain_id=chain_id, seq=seq_base + 1))
    await pilot.pause()


@pytest.mark.asyncio
async def test_a_completion_group_nests_under_its_turns_user_row() -> None:
    """Tier 2b: #4691 arc item ① — a completion Group's own parent row (no
    ``call_id`` parent yet — it's the first agent row of its call) nests
    under the CURRENT turn's user row, once a turn is open, instead of
    landing flat at the top level the way it would with no turn open at all
    (mirrors ``test_a_row_with_no_call_id_still_lands_flat``'s baseline,
    inverted: here a turn IS open)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()

        # ``FlowView.entries`` is "currently laid out" — the turn parent
        # defaults OPEN (this item's own ruling), so its child IS laid out
        # even though that child (a completion Group) defaults COLLAPSED
        # itself; only a collapsed entry's OWN subtree is excluded. 2
        # entries here is "1 turn parent + its 1 laid-out child", not "2
        # unrelated top-level rows" — ``.parent``/``.depth`` below is what
        # actually proves the nesting.
        user_row, completion_row = _entries(app)
        assert user_row.item.kind == "user"
        assert user_row.item.text == "hi"
        assert completion_row.item.meta.get("call_id") == "resp-1"
        assert completion_row.parent is user_row
        assert completion_row.depth == 1
        assert user_row.children == (completion_row,)


@pytest.mark.asyncio
async def test_tool_rows_nest_three_levels_deep_under_turn_then_call() -> None:
    """Tier 2b: the full tree the owner specified — turn Group > completion
    Group > tool execution — built from the SAME two mechanisms
    (``_current_turn_parent`` and ``_call_parents``) composing without any
    extra code: the completion row registers as a ``_call_parents`` entry
    while ALSO landing as the turn parent's own child (this file's previous
    test), so a tool row for that SAME call_id nests two levels below the
    user row."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        user_row, completion_row = _entries(app)
        assert completion_row.parent is user_row
        completion_row.expand()  # the completion level itself defaults collapsed
        (tool_row,) = completion_row.children
        assert tool_row.item.kind == "tool_call_started"
        assert tool_row.depth == 2
        assert tool_row.parent is completion_row
        assert completion_row.parent is user_row


@pytest.mark.asyncio
async def test_the_turn_parent_defaults_open_while_its_completion_group_collapses() -> None:
    """Tier 2b: owner ruling — "既定は turn Group = open / completion Group
    = hide". The turn parent's OWN first child landing must never trip the
    first-child collapse #4781 wired for the completion level (the SAME
    code path, guarded by ``parent is call_parent`` in ``_ingest_frame``)
    — only the completion Group folds by default."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        # The completion Group's own collapse-on-first-child (#4781) fires
        # when ITS first CHILD lands, not when the completion row itself is
        # appended (it has no children yet at that point — nothing to
        # collapse) — a tool row is needed to trigger it, same setup as
        # ``test_4691_phase_b_group_construction.py::test_a_group_parent_defaults_collapsed``.
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()

        user_row, completion_row = _entries(app)
        assert user_row.collapsed is False, (
            "the turn Group must default OPEN — the conversation itself "
            "(the sequence of turns) must always be visible per the "
            "owner's own requirement"
        )
        assert completion_row.parent is user_row
        assert completion_row.collapsed is True, (
            "setup: the completion Group still defaults COLLAPSED (#4781) "
            "— only the turn level's default changed"
        )


@pytest.mark.asyncio
async def test_the_turn_parent_starts_running_immediately() -> None:
    """Tier 2b: owner ruling — the turn's own parent stays RUNNING for the
    WHOLE turn. Set the instant the user row promotes, BEFORE any
    completion Group has even landed (there is nothing to derive it FROM
    yet) — unlike a completion Group parent, whose RUNNING state is set at
    ITS OWN registration for a different, narrower reason (children about
    to arrive)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")

        (user_row,) = _entries(app)
        assert user_row.state is EntryState.RUNNING


@pytest.mark.asyncio
async def test_the_turn_parent_settles_success_at_turn_end() -> None:
    """Tier 2b: a normal turn — one completion Group, its tool settles
    clean — ends with the turn parent SUCCESS, derived from its own
    completion-Group children via :meth:`TextualChatApp._settle_turn_parent`
    reusing :meth:`_recompute_parent_state` one level up."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()
        await transport.push_display(_completed("op-1", call_id="resp-1"))
        await pilot.pause()

        user_row, _completion_row = _entries(app)
        assert user_row.state is EntryState.RUNNING, "setup: still open"

        await transport.push_event(_turn_settled())
        await pilot.pause()
        assert user_row.state is EntryState.SUCCESS


@pytest.mark.asyncio
async def test_one_error_child_taints_the_turn_parent_to_error_at_turn_end() -> None:
    """Tier 2b: a failed tool anywhere in the turn taints the turn parent
    to ERROR at turn end — the SAME "one failure taints the whole call"
    rule ``_recompute_parent_state`` already applies at the completion
    level, reused one level up."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()
        await transport.push_display(_started("op-1", call_id="resp-1"))
        await pilot.pause()
        await transport.push_display(_failed("op-1", call_id="resp-1"))
        await pilot.pause()

        await transport.push_event(_turn_settled())
        await pilot.pause()

        user_row, _completion_row = _entries(app)
        assert user_row.state is EntryState.ERROR


@pytest.mark.asyncio
async def test_a_turn_with_no_completions_settles_cancelled_not_stuck_running() -> None:
    """Tier 2b: a turn that ends before any completion Group ever landed
    under it (cancelled immediately, or every dispatch excluded
    pre-dispatch) has nothing to derive a verdict FROM — CANCELLED, the
    SAME #72 reasoning ``_sweep_orphaned_running_tools`` already applies to
    an orphaned call-parent with no settled child, never SUCCESS (nothing
    was observed to call a success) and never left spinning forever."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A")
        (user_row,) = _entries(app)
        assert user_row.state is EntryState.RUNNING, "setup: no completion yet"

        await transport.push_event(_turn_settled())
        await pilot.pause()
        assert user_row.state is EntryState.CANCELLED


@pytest.mark.asyncio
async def test_a_new_turn_never_nests_under_the_previous_turns_parent() -> None:
    """Tier 2b: the ordinary (non-switch) turn-boundary case — turn A ends,
    turn B starts; B's own completion Group must nest under B's OWN user
    row, never under A's (already-settled) one."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-A", text="turn A")
        await transport.push_display(_parent_row("resp-A"))
        await pilot.pause()
        await transport.push_event(_turn_settled())
        await pilot.pause()

        await _open_turn(transport, pilot, chain_id="chain-B", text="turn B", seq_base=10)
        await transport.push_display(_parent_row("resp-B"))
        await pilot.pause()

        user_a, completion_a, user_b, completion_b = _entries(app)
        assert user_a.item.text == "turn A"
        assert completion_a.parent is user_a
        assert user_b.item.text == "turn B"
        assert completion_b.parent is user_b
        assert completion_b.parent is not user_a


@pytest.mark.asyncio
async def test_a_session_switch_mid_turn_does_not_leak_the_stale_turn_parent() -> None:
    """Tier 2b: co-vet pitfall ① — a ``session_attached`` delta arriving
    WHILE a turn is still open (never settled — simulates the exact race
    the pitfall names) must not leave :attr:`_current_turn_parent` pointing
    into the tree ``conversation.clear()`` just dropped. Observed
    BEHAVIOURALLY, never via a private-state read: if the reset were
    missing, the NEW session's first completion Group would silently try
    to nest under the OLD, now-detached entry — invisible in the NEW
    FlowView — instead of under the NEW turn's own user row, and this
    test's final unpack would fail on a count mismatch (1 entry, not 2)
    rather than a wrong-parent assertion."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await _open_turn(transport, pilot, chain_id="chain-old", text="old turn")
        # "chain-old"'s turn is now open and deliberately never settled —
        # the mid-turn switch race the pitfall names.

        await transport.push_event(Event(type="session_attached", data={}))
        await pilot.pause()
        assert _entries(app) == [], "setup: the switch must have cleared the tree"

        await _open_turn(transport, pilot, chain_id="chain-new", text="new turn", seq_base=10)
        await transport.push_display(_parent_row("resp-new"))
        await pilot.pause()

        user_row, completion_row = _entries(app)
        assert user_row.item.kind == "user"
        assert user_row.item.text == "new turn"
        assert completion_row.parent is user_row, (
            "the new turn's completion Group must nest under the NEW "
            "user row — a leaked _current_turn_parent would have tried "
            "to nest it under the OLD (removed) entry instead"
        )


@pytest.mark.asyncio
async def test_a_row_with_no_open_turn_still_lands_flat() -> None:
    """Tier 2b: baseline/regression guard — a completion Group's own
    placeholder row arriving with NO turn currently open (the pre-item-①
    behaviour, and co-vet pitfall ②'s "outside a turn" half) lands
    top-level exactly as call-level Group construction already did,
    unaffected by this item."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1"))
        await pilot.pause()

        (only,) = _entries(app)
        assert only.item.meta.get("call_id") == "resp-1"
        assert only.parent is None
