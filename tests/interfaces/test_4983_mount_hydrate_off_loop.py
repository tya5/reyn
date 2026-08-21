"""Tier 2: #4983 — ``on_mount()`` no longer makes its own synchronous
``history.jsonl`` disk read.

Root cause: ``TextualChatApp.on_mount()`` is not ``async def``, and used to
call ``self._hydrate_from_history()`` unconditionally, which synchronously
called ``self._read_model.conversation_history()`` — real disk I/O, directly
on the event loop, with no ``await`` anywhere in the chain. Found while
investigating #4834's CI `pump heartbeat +0` stalls; confirmed as a real
defect independent of that investigation's own open question (whether it
explains CI's specific 2-second symptom — it may not, given CI's own small
test fixtures — see #4834's own thread).

Architect's design (c), issue #4983: read the CURRENTLY-ATTACHED session's
history OFF the event loop, BEFORE the App exists at all (``run_textual_
chat``, via ``asyncio.to_thread``), then hand the result to the App at
construction — ``on_mount()`` applies it (pure in-memory projection, no I/O)
instead of reading it itself. First paint is UNCHANGED (still populated,
never blank-then-fills) because the read completes before ``run_async``
starts. Scope: MOUNT ONLY — the session-switch rehydrate path
(``_handle_session_attached_event``) is deliberately untouched here (a
separate, still-open UX question; see that method's own docstring).

Real ``TextualChatApp`` + a real ``ChatReadModel`` seam impl throughout
(mirrors ``tests/runtime/test_textual_chat_phase5_3273.py``'s own
``_HistoryReadModel``) — no mocks.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.app import run_textual_chat
from reyn.interfaces.repl.read_model import ChatReadModel
from reyn.runtime import stall_trace
from reyn.runtime.chat_message import ChatMessage
from tests._support.textual_chat_test_helpers import QueueTransport


class _CountingReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl (like
    ``test_textual_chat_phase5_3273.py``'s own ``_HistoryReadModel``) that
    additionally counts ``conversation_history`` calls — the observable
    this file's tests need (whether ``on_mount`` reads for itself)."""

    def __init__(self, messages: "list[ChatMessage]") -> None:
        self.messages = list(messages)
        self.call_count = 0

    def snapshot(self, config=None):
        return None

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        return Path("/tmp/reyn_4983_input_history")

    def conversation_history(self, *, limit=None, agent=None, session_id=None):
        self.call_count += 1
        return list(self.messages)

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


def _fixture_messages() -> "list[ChatMessage]":
    return [
        ChatMessage(role="user", content="hello", seq=1),
        ChatMessage(role="assistant", content="hi there", seq=2),
    ]


# ── mount with a pre-fetched history: no read, first paint still populated ──


@pytest.mark.asyncio
async def test_mount_with_prefetched_history_never_calls_conversation_history() -> None:
    """Tier 2: the measured defect's own falsifier — with ``initial_history_
    messages`` given, ``on_mount`` must not touch ``self._read_model`` at
    all. Reverting ``on_mount``'s own branch (always calling
    ``_hydrate_from_history()``) turns this red — confirmed by reading the
    diff, not asserted blind."""
    messages = _fixture_messages()
    read_model = _CountingReadModel(messages)
    app = TextualChatApp(
        transport=QueueTransport(),
        read_model=read_model,
        initial_history_messages=messages,
    )
    async with app.run_test(size=(80, 24)):
        pass

    assert read_model.call_count == 0, (
        "on_mount must use the pre-fetched history, not read it again itself"
    )


@pytest.mark.asyncio
async def test_mount_with_prefetched_history_populates_first_paint() -> None:
    """Tier 2: the UX invariant architect's design (c) exists to preserve —
    pre-fetched history must still reach the conversation model (no
    blank-then-fills regression from this refactor)."""
    messages = _fixture_messages()
    read_model = _CountingReadModel(messages)
    app = TextualChatApp(
        transport=QueueTransport(),
        read_model=read_model,
        initial_history_messages=messages,
    )
    async with app.run_test(size=(80, 24)):
        assert len(app.conversation) > 0, (
            "pre-fetched history must be applied to the retained model"
        )


# ── backward compat: no pre-fetch given → old synchronous path still works ──


@pytest.mark.asyncio
async def test_mount_without_prefetch_falls_back_to_the_synchronous_read() -> None:
    """Tier 2: accept-side — a caller that constructs ``TextualChatApp``
    directly without ``initial_history_messages`` (every pre-#4983 call
    site, most existing tests) must still hydrate correctly: ``None`` is
    read as "read it yourself," not "there is nothing to restore.\""""
    read_model = _CountingReadModel(_fixture_messages())
    app = TextualChatApp(transport=QueueTransport(), read_model=read_model)
    async with app.run_test(size=(80, 24)):
        assert read_model.call_count == 1, (
            "no initial_history_messages given: on_mount must still read once, "
            "exactly like before this issue"
        )
        assert len(app.conversation) > 0


# ── run_textual_chat: the actual pre-fetch wiring ───────────────────────────


@pytest.mark.asyncio
async def test_run_textual_chat_prefetches_off_thread_before_construction(
    monkeypatch,
) -> None:
    """Tier 2: ``run_textual_chat`` itself must call ``conversation_history``
    BEFORE constructing the App (mirrors ``test_3671_stall_trace_startup_
    wiring.py``'s own technique — stub ``TextualChatApp.run_async`` to raise
    immediately, so only pre-construction wiring is observed).

    Also confirms the read runs OFF the calling task via a real
    ``asyncio.to_thread``: records the thread identity ``conversation_
    history`` was called from and asserts it differs from the main
    event-loop thread's own identity."""
    main_thread = threading.current_thread()
    call_threads: "list[threading.Thread]" = []
    messages = _fixture_messages()
    read_model = _CountingReadModel(messages)
    real_conversation_history = read_model.conversation_history

    def _recording(*args, **kwargs):
        call_threads.append(threading.current_thread())
        return real_conversation_history(*args, **kwargs)

    monkeypatch.setattr(read_model, "conversation_history", _recording)

    construct_order: "list[str]" = []
    real_init = TextualChatApp.__init__

    def _recording_init(self, *args, **kwargs):
        construct_order.append("construct")
        assert kwargs.get("initial_history_messages") == messages, (
            "the App must be constructed WITH the pre-fetched history"
        )
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(TextualChatApp, "__init__", _recording_init)

    async def _raising_run_async(self, *args, **kwargs):
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)
    monkeypatch.setattr(stall_trace, "arm", lambda seconds: None)
    monkeypatch.setattr(stall_trace, "disarm", lambda: None)

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport(), read_model=read_model)

    assert read_model.call_count == 1
    assert construct_order == ["construct"], "the App must have been constructed"
    assert call_threads, "conversation_history must have been called"
    assert call_threads[0] is not main_thread, (
        "the read must run off the event-loop thread (asyncio.to_thread), "
        "not inline on the same thread run_textual_chat itself runs on"
    )


@pytest.mark.asyncio
async def test_run_textual_chat_with_no_read_model_prefetches_nothing(monkeypatch) -> None:
    """Tier 2: accept-side — ``read_model=None`` (a caller with nothing to
    restore) must not attempt a read at all, and the App must still
    construct (with ``initial_history_messages=None``, on_mount's own
    fallback path — covered by the mount-side tests above)."""
    async def _raising_run_async(self, *args, **kwargs):
        raise RuntimeError("simulated: app never reached first frame")

    monkeypatch.setattr(TextualChatApp, "run_async", _raising_run_async)

    with pytest.raises(RuntimeError, match="simulated"):
        await run_textual_chat(transport=QueueTransport(), read_model=None)
