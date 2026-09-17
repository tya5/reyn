"""Tier 2: #6198's own structural fix — ``_call_parents``' Group-parent
key moves from ``call_id`` (litellm's own response ``id``, a THIRD
PARTY's identifier) to ``turn:{chain_id}/round:{round_index}`` (reyn's
own facts — ``RouterLoop._delta_round_index``, threaded through
dispatcher.py/lifecycle_forwarder.py the SAME explicit-parameter path
``call_id`` already used).

## The defect this closes (#6198's own investigation, 0 observed
## instances at filing; #6215 landed a detection-only interim stage
## first, kept as a general borrowed-key watchdog by this PR)

The OLD design keyed ``_call_parents`` by ``call_id`` alone. If a
provider ever reused a response ``id`` across two DIFFERENT rounds of
the SAME turn (neither OpenAI's nor litellm's own documentation states
a uniqueness scope — ``_response_call_id``'s own docstring, ``llm.py``),
the second round's own tool rows would nest under the FIRST round's
parent — "無関係な行が1つのgroupに束ねられる", later measured to be
even deeper ("無関係な2つのroundが親子関係を持った1つの木に見える",
#6198's own issue thread).

## The fix

`round_index` (reyn's own, reset to 0 once per ``run_loop()`` entry,
incremented before each round) joins ``chain_id`` (also reyn's own) as
the key. Both are threaded down the SAME explicit-parameter path
``call_id`` already used (``dispatcher.py``'s ``DispatchContext.
round_index``, ``lifecycle_forwarder.py``'s meta, ``router_loop.py``'s
own ``put_outbox`` meta) — never read from a stored ``self`` field at
dispatch time (#4734's own lesson, cited at every new call site).
``call_id`` stays on every row's own ``meta`` (provider/debug tracking)
but is never dict-key material again.

## Uniqueness scope (see ``app.py``'s own ``_call_parent_key`` docstring
## for the full disclosure, at the definition site per lead-coder
## review — not just here)

This key is unique within "message that reached the display", not
within reyn's internal state: an INTERRUPTED overflow-retry lap of
``RouterLoop._delta_round_index`` resetting mid-turn never reaches the
outbox at all (litellm's own ``acompletion()`` raises before any content
chunk or terminal row is produced for that lap) — verified
STRUCTURALLY against openai-shaped litellm, not run-verified, and not
traced through every provider adapter.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import _call_parent_key
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT


def _agent_row(
    *, call_id: "str | None", chain_id: "str | None", round_index: "int | None",
    text: str = "", dispatched_tool_calls: "bool | int" = 2,
) -> OutboxMessage:
    """Every axis explicit, no defaults — this file's whole subject is
    which of these actually drives the key, so nothing may be implicit."""
    return OutboxMessage(
        kind="agent",
        text=text,
        meta={
            "chain_id": chain_id,
            "source": "router_tool_turn_text",
            "call_id": call_id,
            "round_index": round_index,
            "finish_reason": "tool_calls",
            "dispatched_tool_calls": dispatched_tool_calls,
        },
    )


def _tool_started(
    *, call_id: "str | None", chain_id: "str | None", round_index: "int | None",
    op_id: str, tool: str = "grep",
) -> OutboxMessage:
    return OutboxMessage(
        kind="tool_call_started",
        text=tool,
        meta={
            "tool": tool, "op_id": op_id, "dispatch_id": op_id, "args": {},
            "call_id": call_id, "chain_id": chain_id, "round_index": round_index,
        },
    )


class QueueTransport(ClientTransportStub):
    """Real, minimal transport fed one frame at a time — same shape as
    the sibling #4691/#6198 test files' own local copy (neither module
    exports its collaborator)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:  # pragma: no cover
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


def _entries(app: TextualChatApp) -> list:
    return list(app.query_one(FlowView).entries)


# ---------------------------------------------------------------------------
# _call_parent_key — pure unit level
# ---------------------------------------------------------------------------


def test_key_is_built_from_chain_id_and_round_index_never_call_id():
    """Tier 2: the key's own value contains no trace of call_id — accept③
    ("鍵の値がすべてreynの物"). Two DIFFERENT call_ids, same chain_id/
    round_index, produce the IDENTICAL key."""
    key_a = _call_parent_key({"call_id": "resp-1", "chain_id": "c1", "round_index": 1})
    key_b = _call_parent_key({"call_id": "resp-2", "chain_id": "c1", "round_index": 1})
    assert key_a == key_b == "turn:c1/round:1"


def test_key_is_none_without_round_index_even_with_a_real_call_id():
    """Tier 2: call_id alone is never enough — the legacy/pre-#6198 wire
    shape (a producer that never threaded round_index through) must not
    silently fall back to call_id-keying."""
    assert _call_parent_key({"call_id": "resp-1", "chain_id": "c1"}) is None
    assert _call_parent_key({"call_id": "resp-1", "chain_id": "c1", "round_index": 0}) is None


def test_key_is_none_without_chain_id():
    """Tier 2: the other half of "both facts required" — a real
    round_index alone is not enough either, symmetric with the
    call_id-without-round_index case above."""
    assert _call_parent_key({"chain_id": None, "round_index": 1}) is None


def test_different_round_index_same_chain_id_produces_different_keys():
    """Tier 2: LOAD-BEARING — the exact structural property the fix
    exists to establish. strip: revert ``_call_parent_key`` to read
    ``meta.get("call_id")`` instead -- both keys below would collapse
    to the SAME value despite different rounds, reproducing #6198's own
    named defect."""
    key_round_1 = _call_parent_key({"call_id": "resp-1", "chain_id": "c1", "round_index": 1})
    key_round_2 = _call_parent_key({"call_id": "resp-1", "chain_id": "c1", "round_index": 2})
    assert key_round_1 != key_round_2


# ---------------------------------------------------------------------------
# End to end — a real TextualChatApp, real frames, real FlowView tree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reused_call_id_across_two_rounds_no_longer_merges_the_groups():
    """Tier 2: LOAD-BEARING end-to-end — the exact #6198 repro. Two
    ``kind="agent"`` parent rows sharing the SAME ``call_id`` (the shape
    a provider id reuse would produce) but DIFFERENT ``round_index``
    must land as TWO SEPARATE top-level Groups, never one row nested
    under the other. Strip-falsify witness: reverting ``_call_parent_
    key`` to read ``call_id`` (dropping ``round_index``) merges them —
    reproducing #6198's own named symptom ("無関係な2つのroundが親子
    関係を持った1つの木に見える")."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_agent_row(
            call_id="resp-1", chain_id="c1", round_index=1, text="first",
        ))
        await pilot.pause()
        await transport.push_display(_agent_row(
            call_id="resp-1", chain_id="c1", round_index=2, text="second",
        ))
        await pilot.pause()

        first, second = _entries(app)
        assert first.item.text == "first"
        assert second.item.text == "second"
        assert second.parent is None, (
            "the second round must NOT nest under the first -- they "
            "share a call_id but belong to different rounds"
        )
        assert first.children == ()


@pytest.mark.asyncio
async def test_tool_rows_nest_under_the_matching_round_not_the_matching_call_id():
    """Tier 2: accept① — a tool row must find ITS OWN round's parent,
    even when an EARLIER round shares the same (reused) call_id. Real
    #6198 shape: two rounds, same call_id, each with its own tool
    call — the SECOND round's tool row must nest under the SECOND
    parent, never the first."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_agent_row(
            call_id="resp-1", chain_id="c1", round_index=1, text="first",
        ))
        await transport.push_display(_tool_started(
            call_id="resp-1", chain_id="c1", round_index=1, op_id="op-1",
        ))
        await pilot.pause()
        await transport.push_display(_agent_row(
            call_id="resp-1", chain_id="c1", round_index=2, text="second",
        ))
        await transport.push_display(_tool_started(
            call_id="resp-1", chain_id="c1", round_index=2, op_id="op-2",
        ))
        await pilot.pause()

        first_parent = next(e for e in _entries(app) if e.item.text == "first")
        second_parent = next(e for e in _entries(app) if e.item.text == "second")
        (first_child,) = first_parent.children
        (second_child,) = second_parent.children
        assert first_child.item.meta.get("op_id") == "op-1"
        assert second_child.item.meta.get("op_id") == "op-2", (
            "the round-2 tool row must nest under the round-2 parent, "
            "not fall back to whichever call_id-matching parent came "
            "first"
        )


@pytest.mark.asyncio
async def test_no_call_id_at_all_still_registers_and_nests_by_round_alone():
    """Tier 2: accept① — call_id is genuinely optional now. A round
    whose agent row and tool row BOTH carry no call_id at all (an
    op-loop caller, #6198's own "call_id: null events" population)
    still nests correctly, keyed purely on chain_id/round_index."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_agent_row(
            call_id=None, chain_id="c1", round_index=1, text="reply",
        ))
        await transport.push_display(_tool_started(
            call_id=None, chain_id="c1", round_index=1, op_id="op-1",
        ))
        await pilot.pause()

        (parent,) = [e for e in _entries(app) if e.item.text == "reply"]
        (child,) = parent.children
        assert child.item.kind == "tool_call_started"


def test_no_src_site_reads_call_id_as_the_call_parents_dict_key():
    """Tier 2: grep witness (accept③, "鍵の値がすべてreynの物") — no
    ``src/`` file builds a ``_call_parents``-style lookup off a bare
    ``meta.get("call_id")`` any more. Reworded to avoid self-matching
    this docstring's own words (the same false-positive #6213's own
    equivalent witness hit and documented)."""
    app_py = (
        REPO_ROOT / "src" / "reyn" / "interfaces" / "inline" / "textual_chat" / "app.py"
    )
    src = app_py.read_text()
    assert 'self._call_parents.get(call_id)' not in src
    assert 'self._call_parents[call_id]' not in src
