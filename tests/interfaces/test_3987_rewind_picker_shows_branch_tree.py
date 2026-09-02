"""Tier 2: #3987 ② — the ``/rewind`` picker offers abandoned branches, not
just the active one.

ADR-0038 D8 landed forking (a rewind to a live-branch seq can strand later
history as a dead, still-recoverable branch) and the 2b tree layout
(``build_branch_tree_rows``) to render it — but nothing ever called either.
``list_rewind_points()`` (no ``include_abandoned``) and ``show_points`` (a flat
list, no branch concept) meant a fork actually happening was invisible to the
operator: it existed in the WAL, but there was no way back to it short of
knowing its seq by memory.

Owner decision (2026-09-02, chat, verbatim): "対応する方向で残して" — wire it up
rather than delete the unwired 2b code. Architect design (issue #3987, ruling
comment): no new abstraction — ``slash/rewind.py`` gathers ``list_branches()``
alongside ``list_rewind_points(include_abandoned=True)``; the picker gets a
tree renderer built from the SAME ``build_branch_tree_rows`` the 2b tests
already exercise; selecting a checkpoint on any branch (live or abandoned)
still goes through the ONE existing seam (``/rewind <seq>`` -> the unified
``AgentRegistry.checkout``) — no second action path, no new state.

Accept/deny split, per the ruling comment's own item 4:

- accept: a session with an abandoned branch shows BOTH branches as a tree
  (headers + indented checkpoints, the abandoned one marked), and picking an
  abandoned checkpoint reaches ``checkout`` exactly like a live one does.
- deny: a session with only the active branch renders EXACTLY as it did
  before this PR — the existing #3362/#4788/#4817 tests are the witnesses for
  that, unmodified and green; this file adds only what changed.

Real ``TextualChatApp`` + real ``Pilot`` + a real, minimal ``ClientTransport``
(the ``ScriptedTransport``/``_PickerReadModel`` pair
``test_textual_chat_copy_rewind_3362.py`` already established) — no mocks.
``AgentRegistry.checkout``'s own destructive behaviour is pinned by its own
existing tests; what is new here is that a picked ABANDONED row reaches the
SAME slash command a picked live row always did, which is the app-level
seam this file owns.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_BRANCHES = [
    {"branch_id": 0, "fork_point_seq": 0, "head_seq": 13, "parent_branch_id": None, "is_active": True},
    {"branch_id": 11, "fork_point_seq": 6, "head_seq": 10, "parent_branch_id": 0, "is_active": False},
]
_POINTS = [
    {"seq": 12, "ts": "t12", "kind": "turn", "anchor": "latest on the live branch", "branch_id": 0},
    {"seq": 6, "ts": "t6", "kind": "turn", "anchor": "fork point", "branch_id": 0},
    {"seq": 9, "ts": "t9", "kind": "turn", "anchor": "on the abandoned side", "branch_id": 11},
]
# Ascending by seq — the order the real ``AgentRegistry.list_rewind_points``
# returns and the order ``slash/rewind.py`` now forwards unreversed (#3987 ②:
# the tree builder does its own ordering, so pre-reversing would fight it).
# ``show_tree``'s single-branch fallback is what restores newest-first.
_SINGLE_BRANCH_POINTS = [
    {"seq": 2, "ts": "t2", "kind": "turn", "anchor": "earlier"},
    {"seq": 4, "ts": "t4", "kind": "turn", "anchor": "only ever one branch"},
]


class _PickerReadModel:
    """The same minimal read model ``test_textual_chat_copy_rewind_3362.py``
    uses — reproduced rather than imported so this file does not reach into
    that one's private test scaffolding."""

    def __init__(self, pending: "dict | None" = None) -> None:
        self._pending = pending

    def snapshot(self, config=None):
        return None

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return self._pending

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self):
        from pathlib import Path
        return Path("/tmp/reyn_3987_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class ScriptedTransport(ClientTransportStub):
    """Reproduced from ``test_textual_chat_copy_rewind_3362.py`` (same
    reason as the read model above)."""

    def __init__(self, messages: "list[OutboxMessage] | None" = None) -> None:
        self._messages = list(messages or [])
        self.commands: "list[str]" = []
        self.cleared = 0

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        pass

    async def run_slash_command(self, name: str, args: str) -> bool:
        self.commands.append(f"/{name} {args}".rstrip())
        return True

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: OutboxMessage) -> None:
        self._messages.append(msg)

    async def clear_pending_command_ui(self) -> None:
        self.cleared += 1

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


async def _settle(pilot) -> None:
    for _ in range(3):
        await pilot.pause()


# ---------------------------------------------------------------------------
# accept — an abandoned branch is offered, and reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_branches_render_as_a_tree_with_the_abandoned_one_marked() -> None:
    """Tier 2: accept ① — headers for both branches, checkpoints indented under
    each, the abandoned one visibly marked. Before this PR the command-UI
    request carried no ``branches`` key at all and the picker had no tree
    renderer, so this shape could not be produced no matter what the registry
    held."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS, "branches": _BRANCHES})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        assert picker.display, "the picker never appeared"
        options = picker.query_one("#rewind-picker-options")
        rendered = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        blob = "\n".join(rendered)

        assert "(abandoned)" in blob, f"no abandoned marker in:\n{blob}"
        # Two header rows (one per branch) plus three checkpoint rows.
        assert options.option_count == 5, f"expected 2 headers + 3 checkpoints:\n{blob}"
        assert transport.cleared == 1


@pytest.mark.asyncio
async def test_picking_an_abandoned_checkpoint_reaches_the_same_rewind_command() -> None:
    """Tier 2: accept ② — the ONE seam. Picking a checkpoint that sits on the
    abandoned branch posts the exact same ``/rewind <seq>`` the app has always
    routed for a live one; there is no second, branch-aware action path.
    ``AgentRegistry.checkout``'s own live-vs-abandoned dispatch is pinned by
    its own existing tests (not re-driven here) — what this test owns is that
    the app-level seam does not fork itself just because the data underneath
    now can."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS, "branches": _BRANCHES})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        options = picker.query_one("#rewind-picker-options")

        # Locate the row for seq 9 (the abandoned checkpoint) by its label —
        # never by a hardcoded index, which would silently stop meaning what
        # this test says it means the moment header placement changes.
        target = next(
            i for i in range(options.option_count)
            if "seq 9" in str(options.get_option_at_index(i).prompt)
        )
        options.highlighted = target
        await pilot.press("enter")
        await _settle(pilot)

        assert transport.commands == ["/rewind 9"], (
            f"picking the abandoned checkpoint must route through the ordinary "
            f"/rewind seam, unchanged: {transport.commands!r}"
        )


@pytest.mark.asyncio
async def test_a_branch_header_row_is_not_selectable() -> None:
    """Tier 2: accept ③ — a header occupies a row (so the tree reads as a
    tree) but Enter on it must never post a checkout.

    Measured, not assumed: forcing ``.highlighted`` directly onto a disabled
    option and pressing Enter does NOT reach the picker's own
    ``seq is None`` guard in this build — Textual's own OptionList already
    refuses to select a disabled option, so the guard is presently
    belt-with-no-braces-yet: correct, and not the layer doing the work.
    Kept anyway (defense a caller cannot see from outside should not depend
    on one library's internals), and this test's own strip-falsify
    (disabling the guard) does not turn it red for exactly that reason —
    disclosed rather than claimed as a witness of the guard itself."""
    read_model = _PickerReadModel({"kind": "rewind", "points": _POINTS, "branches": _BRANCHES})
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        options = picker.query_one("#rewind-picker-options")

        header_idx = next(
            i for i in range(options.option_count)
            if options.get_option_at_index(i).disabled
        )
        # Force the highlight onto the header directly — arrow-key navigation
        # would never land here (Textual itself skips disabled options), so
        # this bypasses that and drives the picker's OWN guard, not
        # Textual's.
        options.highlighted = header_idx
        await pilot.press("enter")
        await _settle(pilot)

        assert transport.commands == [], (
            f"Enter on a header row must submit nothing — got "
            f"{transport.commands!r}"
        )
        assert picker.display, "the picker must still be open, not dismissed"


# ---------------------------------------------------------------------------
# deny — a single-branch session is unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_single_branch_session_still_renders_the_flat_list() -> None:
    """Tier 2: deny — this PR's own acceptance criterion (architect ruling,
    item 4): an operator who never forked sees NO new furniture. No branch
    headers, same row count as before, same order.

    ``test_rewind_sentinel_opens_the_picker_with_the_checkpoints``
    (test_textual_chat_copy_rewind_3362.py) already pins this shape without a
    ``branches`` key in the request at all (the pre-#3987 caller shape) and
    stays green, unmodified. This test additionally pins it WITH a
    single-branch ``branches`` list present — the new code path a real
    registry now always sends, which that older test cannot exercise since it
    predates the key's existence."""
    single_branch = [
        {"branch_id": 0, "fork_point_seq": 0, "head_seq": 4, "parent_branch_id": None, "is_active": True},
    ]
    read_model = _PickerReadModel(
        {"kind": "rewind", "points": _SINGLE_BRANCH_POINTS, "branches": single_branch},
    )
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        options = picker.query_one("#rewind-picker-options")

        assert options.option_count == len(_SINGLE_BRANCH_POINTS), (
            "a single-branch session must show exactly one row per checkpoint "
            "— no header rows"
        )
        assert not any(
            options.get_option_at_index(i).disabled
            for i in range(options.option_count)
        ), "no row may be disabled when there is nothing to show a tree about"
        rendered = [
            str(options.get_option_at_index(i).prompt)
            for i in range(options.option_count)
        ]
        assert [r for r in rendered if "seq 4" in r][0] == rendered[0], (
            f"row order must stay newest-first, exactly as before this PR: "
            f"{rendered!r}"
        )
