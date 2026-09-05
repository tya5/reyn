"""Tier 2: #5769 stage 3 ④ — the rewind picker states its default SCOPE
(session-local vs global) BEFORE the operator picks a row, not only in the
after-the-fact summary reply.

Architect scope (verbatim, relayed by lead-coder): 「これは global か
session-local か」が操作の前に見える形. ADR-0047 decision 3 (owner ruling
2026-09-05 「規定はローカルがよいな」): the default is session-local — the
INVOKING session's own ``(agent_name, session_id)``.

The command-UI request (``set_pending_command_ui``) now carries a
``default_scope`` field alongside ``points``/``branches``; the App relays it
to ``RewindPicker.show_tree``/``show_points``, which updates the picker's own
title Static — the one thing on screen before Enter is ever pressed.

Real ``TextualChatApp`` + real ``Pilot`` + the same minimal
``ClientTransport``/read-model pair ``test_3987_rewind_picker_shows_branch_tree.py``
established (reproduced here rather than imported, per that file's own
convention) — no mocks.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.rewind_picker import RewindPicker
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

_BRANCHES = [
    {"branch_id": 0, "fork_point_seq": 0, "head_seq": 4, "parent_branch_id": None, "is_active": True},
]
_POINTS = [
    {"seq": 2, "ts": "t2", "kind": "turn", "anchor": "", "branch_id": 0,
     "name": "alpha", "sid": "main"},
    {"seq": 4, "ts": "t4", "kind": "turn", "anchor": "", "branch_id": 0,
     "name": "alpha", "sid": "sub-7"},
]


class _PickerReadModel(ChatReadModel):
    """Reproduced from ``test_3987_rewind_picker_shows_branch_tree.py`` (that
    file's own comment explains why: not imported across test files)."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

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
        return Path("/tmp/reyn_5769_stage3_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class ScriptedTransport(ClientTransportStub):
    """Reproduced from ``test_3987_rewind_picker_shows_branch_tree.py``."""

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


@pytest.mark.asyncio
async def test_picker_title_states_the_default_scope_before_a_row_is_picked() -> None:
    """Tier 2: accept — the command-UI request's ``default_scope`` lands on
    screen, in the picker's OWN title, before Enter is ever pressed."""
    read_model = _PickerReadModel({
        "kind": "rewind", "points": _POINTS, "branches": _BRANCHES,
        "default_scope": {"agent": "alpha", "sid": "main"},
    })
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        assert picker.display, "the picker never appeared"
        title = str(picker.query_one("#rewind-picker-title").content)
        assert "session-local" in title
        assert "alpha/main" in title
        assert "global" in title, "the escape hatch to a global rewind must be named too"


@pytest.mark.asyncio
async def test_picker_marks_a_row_owned_by_a_different_session() -> None:
    """Tier 2: accept — a checkpoint NOT owned by the default scope is named
    in its own row, so picking it does not read as "obviously mine"."""
    read_model = _PickerReadModel({
        "kind": "rewind", "points": _POINTS, "branches": _BRANCHES,
        "default_scope": {"agent": "alpha", "sid": "main"},
    })
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        options = picker.query_one("#rewind-picker-options")
        rendered = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
        joined = "\n".join(rendered)
        assert "sub-7" in joined, f"the non-default-scope row must be marked; got {rendered!r}"


@pytest.mark.asyncio
async def test_picker_deny_no_default_scope_keeps_the_original_title() -> None:
    """Tier 2: deny — a request with no ``default_scope`` (defensive; the
    slash handler always supplies one today) renders EXACTLY the pre-#5769-
    stage-3-④ title, unchanged."""
    read_model = _PickerReadModel({
        "kind": "rewind", "points": _POINTS, "branches": _BRANCHES,
        "default_scope": None,
    })
    transport = ScriptedTransport([
        OutboxMessage(kind="__rewind_list__", text="rewind to a checkpoint…"),
    ])
    app = TextualChatApp(transport=transport, read_model=read_model)
    async with app.run_test() as pilot:
        await _settle(pilot)
        picker = app.query_one(RewindPicker)
        title = str(picker.query_one("#rewind-picker-title").content)
        assert title == "rewind to a checkpoint (enter to check out · esc to cancel)"
