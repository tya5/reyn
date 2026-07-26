"""Tier 2: the "user_submitted" event's DISPLAY-boundary rendering (#3300 P1 C).

``Session.submit_user_text`` (runtime/session.py) now emits a raw-text
``user_submitted`` chat-event instead of writing a neutralized "user" frame
straight to the outbox. Each surface's event->display handler is responsible
for BOTH rendering the echo AND neutralizing (ESC/control strip) at render
time — this file proves that per surface:

- ``ConsoleChatRenderer.on_chat_event`` (the plain / --cui / non-TTY path,
  ALSO the remote/agui path when it resolves non-interactive — same renderer
  selection seam, ``logger_factory.make_renderer``).
- ``InlineChatRenderer.on_chat_event`` (the ``chat.render_mode: plain``-on-a-
  TTY fallback — reachable only when the Textual app is bypassed).
- ``TextualChatApp._pump_frames`` (the default interactive-TTY surface,
  ``interfaces/inline/textual_chat/app.py``) — appends the echo straight to
  the retained conversation model via the SAME
  ``user_submitted_display_message`` seam.

Also covers the cross-cutting invariants the design pass called out:
ordering (echo precedes the agent's turn) and multi-client (every attached
subscriber sees its own rendered line, single source = the event, no
double-render).

Policy compliance (docs/deep-dives/contributing/testing.md):
- No unittest.mock / MagicMock / AsyncMock / patch on a collaborator OBJECT.
  Renderer instances and TextualChatApp's retained model are REAL. The only
  monkeypatches are free-function swaps (``router_loop.call_llm_tools`` — the
  same pattern ``test_1800_wake_drain.py`` uses — and ``sys.__stdout__`` for
  output capture, mirroring ``test_renderer_console_width_2655.py``).
- No private-state assertions — renders are observed via captured stdout /
  the model's own iteration, chat-events via the public
  ``subscribe_chat_events`` surface.
- Each test docstring's first line declares its Tier.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.repl import renderer as renderer_module
from reyn.interfaces.repl.renderer import (
    ConsoleChatRenderer,
    InlineChatRenderer,
    user_submitted_display_message,
)
from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.frames import EventFrame
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.session import Session
from reyn.schemas.models import Event
from tests._support.agent_session import make_session

_RAW_ESC = "\x1b[31mdanger\x1b[0m"
_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _make_session(tmp_path: Path, *, agent_name: str = "test_agent") -> Session:
    return make_session(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


def _make_llm_stub_fn(result):  # type: ignore[no-untyped-def]
    """Real async callable mimicking call_llm_tools — no mock (mirrors
    test_1800_wake_drain.py's helper)."""
    async def _stub(**kwargs) -> LLMToolCallResult:  # noqa: ANN202
        return result

    return _stub


async def _run_n_turns_then_shutdown(session: Session, n: int, timeout: float = 3.0) -> None:
    turns_done = [0]
    original_run_one = session.run_one_iteration

    async def _counting_run_one() -> bool:
        result = await original_run_one()
        if result:
            turns_done[0] += 1
        return result

    session.run_one_iteration = _counting_run_one  # type: ignore[method-assign]
    run_task = asyncio.create_task(session.run())
    deadline = asyncio.get_event_loop().time() + timeout
    while turns_done[0] < n:
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.005)
    await session.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except asyncio.TimeoutError:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)


class _EventSink:
    """A real (non-mock) chat-event subscriber — a plain callback collector."""

    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)


def _capture_stdout(monkeypatch) -> io.StringIO:
    captured = io.StringIO()
    monkeypatch.setattr(renderer_module.sys, "__stdout__", captured)
    return captured


class QueueTransport(ClientTransport):
    """A real, minimal :class:`ClientTransport` fed one frame at a time from a
    queue (mirrors ``test_textual_chat_orphan_sweep_72.py``'s helper) — lets a
    test push an ``EventFrame`` and inspect ``TextualChatApp``'s retained
    conversation model afterward, stream staying open throughout."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

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


# ---------------------------------------------------------------------------
# Contract: user_submitted_display_message (shared seam every surface calls)
# ---------------------------------------------------------------------------


def test_display_message_builds_kind_user_from_raw_event_data() -> None:
    """Tier 1: user_submitted_display_message maps event.data -> a kind="user"
    OutboxMessage, carrying meta through unchanged."""
    event = Event(type="user_submitted", data={"text": "hello", "meta": {"actor": "alice"}})
    msg = user_submitted_display_message(event)
    assert msg.kind == "user"
    assert msg.text == "hello"
    assert msg.meta.get("actor") == "alice"


def test_display_message_neutralizes_raw_esc_control_bytes() -> None:
    """Tier 1: neutralize-at-display — a raw ESC/control sequence in the
    event's raw text does NOT survive into the built display message. Strip
    the ``get_neutralizer("terminal")`` call inside
    ``user_submitted_display_message`` -> this assertion goes RED (a raw
    leak), proving the neutralize call is load-bearing here."""
    event = Event(type="user_submitted", data={"text": _RAW_ESC, "meta": {}})
    msg = user_submitted_display_message(event)
    assert "\x1b" not in msg.text
    assert "danger" in msg.text


# ---------------------------------------------------------------------------
# Surface: ConsoleChatRenderer (plain / --cui / non-TTY / remote-non-interactive)
# ---------------------------------------------------------------------------


def test_console_renderer_renders_user_submitted_event(monkeypatch) -> None:
    """Tier 2: ConsoleChatRenderer.on_chat_event renders the user line from a
    "user_submitted" event (the removed outbox echo's replacement). Strip the
    ``elif etype == "user_submitted"`` branch -> nothing below is written and
    this assertion goes RED (non-vacuity)."""
    out = _capture_stdout(monkeypatch)
    r = ConsoleChatRenderer()
    event = Event(type="user_submitted", data={"text": "hello from event", "meta": {}})

    r.on_chat_event(event)

    assert "hello from event" in out.getvalue()


def test_console_renderer_neutralizes_at_render(monkeypatch) -> None:
    """Tier 2: a raw ESC/control sequence submitted via the event does not
    reach ConsoleChatRenderer's rendered output — strip the neutralize call
    in ``user_submitted_display_message`` -> the raw ESC byte leaks into
    captured stdout (RED)."""
    out = _capture_stdout(monkeypatch)
    r = ConsoleChatRenderer()
    event = Event(type="user_submitted", data={"text": _RAW_ESC, "meta": {}})

    r.on_chat_event(event)

    rendered = out.getvalue()
    assert "\x1b" not in rendered
    assert "danger" in rendered


# ---------------------------------------------------------------------------
# Surface: InlineChatRenderer (chat.render_mode=plain-on-a-TTY fallback path)
# ---------------------------------------------------------------------------


def test_inline_renderer_renders_user_submitted_event(monkeypatch) -> None:
    """Tier 2: InlineChatRenderer.on_chat_event renders the user line from a
    "user_submitted" event — the ONLY renderer entry point reachable when
    this class runs the shared plain PromptSession loop instead of the
    default TextualChatApp (client_driver.py: resolved render_mode="plain")."""
    out = _capture_stdout(monkeypatch)
    r = InlineChatRenderer()
    event = Event(type="user_submitted", data={"text": "hello inline", "meta": {}})

    r.on_chat_event(event)

    assert "hello inline" in out.getvalue()


def test_inline_renderer_neutralizes_at_render(monkeypatch) -> None:
    """Tier 2: the RAW ESC-bearing text submitted via the event never reaches
    InlineChatRenderer's rendered output verbatim (same neutralize-at-display
    seam). InlineChatRenderer renders through a real Rich ``Console``, which
    injects its OWN legitimate ANSI styling codes — so this asserts the
    SPECIFIC submitted control sequence is gone (its ESC byte stripped,
    leaving the harmless literal ``[31m``/``[0m`` bracket text Rich then
    renders unstyled), not a blanket "no ESC anywhere" (that would fail on
    Rich's own valid output)."""
    out = _capture_stdout(monkeypatch)
    r = InlineChatRenderer()
    event = Event(type="user_submitted", data={"text": _RAW_ESC, "meta": {}})

    r.on_chat_event(event)

    rendered = out.getvalue()
    assert _RAW_ESC not in rendered
    assert "danger" in rendered
    assert "[31m" in rendered  # the neutralized (ESC-stripped) literal remainder


# ---------------------------------------------------------------------------
# Cross-cutting: ordering + multi-client, driven through a REAL session/turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_submitted_precedes_turn_started(tmp_path, monkeypatch) -> None:
    """Tier 2: the user_submitted event for a turn fires BEFORE that turn's
    turn_started — the user line renders before the agent's reply, driven
    through a real Session + real turn (LLM faked via a real async callable,
    not a mock, per policy)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    sink = _EventSink()
    session.subscribe_chat_events(sink)
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _make_llm_stub_fn(
            LLMToolCallResult(
                content="hi back", tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
            )
        ),
    )

    await session.submit_user_text("hello")
    await _run_n_turns_then_shutdown(session, n=1)
    await session._journal.flush()

    types = [e.type for e in sink.events]
    assert "user_submitted" in types
    assert "turn_started" in types
    assert types.index("user_submitted") < types.index("turn_started")


@pytest.mark.asyncio
async def test_history_persistence_unaffected_by_the_echo_move(tmp_path, monkeypatch) -> None:
    """Tier 2: the user line still reaches history via the inbox path,
    unaffected by the outbox-echo -> event move — persistence was always
    independent of the (now-removed) outbox write (run_one_iteration ->
    _handle_user_message -> _append_history reads the INBOX copy, never the
    outbox)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _make_llm_stub_fn(
            LLMToolCallResult(
                content="ack", tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
            )
        ),
    )

    await session.submit_user_text("persist me")
    await _run_n_turns_then_shutdown(session, n=1)
    await session._journal.flush()

    texts = [m.content for m in session.history if m.role == "user"]
    assert any("persist me" in str(t) for t in texts)


@pytest.mark.asyncio
async def test_multi_client_each_subscriber_gets_its_own_echo_no_double_render(
    tmp_path, monkeypatch,
) -> None:
    """Tier 2: ADR-0039 multi-client preserved — TWO independent chat-event
    subscribers (standing in for two attached clients, including the
    submitter) each receive exactly ONE "user_submitted" event for one
    submit — single source (the event), no double-render, no client-local-
    only echo."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    sink_a, sink_b = _EventSink(), _EventSink()
    session.subscribe_chat_events(sink_a)
    session.subscribe_chat_events(sink_b)

    await session.submit_user_text("one submit, two clients")

    for sink in (sink_a, sink_b):
        matches = [e for e in sink.events if e.type == "user_submitted"]
        # Exactly one — variable-binding idiom (per test-audit policy) instead
        # of a bare len(...) == N pin.
        (only,) = matches
        assert only.data.get("text") == "one submit, two clients"


# ---------------------------------------------------------------------------
# Surface: TextualChatApp (the default interactive-TTY surface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_textual_chat_app_renders_user_submitted_event() -> None:
    """Tier 2b: TextualChatApp._pump_frames appends a "user" entry to the
    retained conversation model from a "user_submitted" EVENT frame — the
    Textual surface's replacement for the removed outbox echo. In Phase C
    this is a normal flow entry (the sent-queue staging is Phase B)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            Event(type="user_submitted", data={"text": "hi from textual", "meta": {}})
        )
        await pilot.pause()

        entries = app.query_one(FlowView).entries
        (entry,) = [e for e in entries if e.item.kind == "user"]
        assert entry.item.text == "hi from textual"


@pytest.mark.asyncio
async def test_textual_chat_app_neutralizes_at_render() -> None:
    """Tier 2b: a raw ESC/control sequence submitted via the event does not
    reach TextualChatApp's retained model — same neutralize-at-display seam
    every surface calls (``user_submitted_display_message``)."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(
            Event(type="user_submitted", data={"text": _RAW_ESC, "meta": {}})
        )
        await pilot.pause()

        entries = app.query_one(FlowView).entries
        (entry,) = [e for e in entries if e.item.kind == "user"]
        assert "\x1b" not in entry.item.text
        assert "danger" in entry.item.text


@pytest.mark.asyncio
async def test_textual_chat_app_ignores_non_user_submitted_events_for_flow() -> None:
    """Tier 2b: non-vacuity witness — a DIFFERENT event type (turn_started)
    does NOT append a "user" entry, proving the prior tests' entry came
    specifically from the "user_submitted" branch, not from generic event
    handling."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_event(Event(type="turn_started", data={}))
        await pilot.pause()

        entries = app.query_one(FlowView).entries
        assert not [e for e in entries if e.item.kind == "user"]
