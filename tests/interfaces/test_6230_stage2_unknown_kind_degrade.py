"""Tier 1/2: #6230 stage 2 — an unrecognized display-frame ``kind`` must
render a readable line (never blank, structurally, even when BOTH ``text``
and ``kind`` are empty), and its arrival must leave a durable, bounded
witness (a ``display_frame_unknown_kind`` audit-event) that names where the
frame arrived from and where it was detected — a degrade must not also
remove the last trace that a routing defect occurred (the #6234
investigation this arc is a direct response to dead-ended on a record that
had an exception TYPE but no POSITION).

Four groups, in issue-thread acceptance order:

1. ``legible_degrade_text`` (pure function) — the 3-tier "structurally
   cannot be empty" contract, including the DEGENERATE case (``text=""``
   AND ``kind=""``) the issue thread singled out by name.
2. The same guarantee END TO END through the two live render paths that
   used to write a blank line for it: the TUI presenter's generic fallback
   (``_body_and_background``) and the plain ``--cui`` renderer
   (``ConsoleChatRenderer``/``RichChatRenderer``).
3. ``is_unknown_kind`` / ``UnknownKindStats`` / ``record_unknown_kind_frame``
   — the witness mechanism, unit level then a real ``emit_cli_event`` +
   ``.reyn/events`` read-back (mirrors
   ``test_5732_pump_swallow_visibility.py``'s own idiom).
4. A real ``TextualChatApp`` + ``run_test()`` pilot, fed a genuinely
   unrecognized kind through the real pump (``QueueTransport``) — proves
   the wiring in ``TextualChatApp._ingest_frame``, not just each piece in
   isolation.

No mocks anywhere: real ``OutboxMessage.from_wire`` (the actual untrusted-
wire construction path an unrecognized kind arrives through), real
renderers, real ``TextualChatApp``, real audit-event emission + read-back.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import AsyncIterator

import pytest
from rich.console import Console

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.inline.textual_chat.presenter import _body_and_background
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.repl.renderer import (
    ConsoleChatRenderer,
    RichChatRenderer,
    UnknownKindStats,
    is_unknown_kind,
    legible_degrade_text,
    record_unknown_kind_frame,
)
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.textual_chat_test_helpers import QueueTransport


def _render_to_text(renderable) -> str:
    """Render any Rich renderable to a plain string via a no-color Console —
    same helper ``test_3318_body_neutralize.py`` already established for
    this exact purpose (the ``agent``-kind Markdown object has no
    ``.plain``)."""
    buf = io.StringIO()
    Console(file=buf, color_system=None, width=100).print(renderable)
    return buf.getvalue()


# ── group 1: legible_degrade_text — pure function ───────────────────────────


def test_degrade_returns_text_verbatim_when_non_empty() -> None:
    """Tier 1: tier ① — a real ``text`` is shown as-is, untouched."""
    assert legible_degrade_text("agent", "hello") == "hello"


def test_degrade_names_the_kind_when_text_is_empty() -> None:
    """Tier 1: tier ② — empty ``text`` but a real ``kind`` names the kind,
    so a reader knows WHAT arrived even with nothing to say about it."""
    result = legible_degrade_text("__totally_unrecognized__", "")
    assert result.strip(), "must not be blank"
    assert "__totally_unrecognized__" in result


def test_degrade_is_never_empty_when_both_kind_and_text_are_empty() -> None:
    """Tier 1: tier ③, THE degenerate case the issue thread named by name —
    ``text=""`` AND ``kind=""``. This is the whole point of the
    "structurally cannot be empty" invariant: there is no fourth branch
    left to fall through, so this call CANNOT return ``""``."""
    result = legible_degrade_text("", "")
    assert result != ""
    assert result.strip() != ""


def test_degrade_whitespace_only_text_still_falls_through_to_a_real_tier() -> None:
    """Tier 1: falsification pair for the degenerate case above — a
    whitespace-only ``text`` is falsy-adjacent but not empty by Python's
    ``if text:`` check; confirms the function's OWN boundary (empty
    string, not "blank-looking") rather than a stronger claim this
    function does not actually make."""
    # `"  "` IS truthy in Python (`if "  "` is True), so tier ① fires and
    # returns it verbatim — documenting the function's real boundary
    # rather than asserting a stronger "never visually blank" claim.
    assert legible_degrade_text("status", "  ") == "  "


# ── group 2: the live render paths — TUI presenter + plain renderers ───────


def test_tui_presenter_never_renders_blank_for_an_unrecognized_kind_with_no_text() -> None:
    """Tier 2: the defect this stage closes, on the REAL render path
    (``_body_and_background``'s generic fallback) — was ``msg.text or " "``,
    which IS non-empty by Python's own truthiness (a single space) but
    renders as a blank line. Built via ``OutboxMessage.from_wire`` — the
    actual untrusted-wire construction path an unrecognized kind arrives
    through in production (``__post_init__`` validation is bypassed there
    by design, so an out-of-vocabulary kind can reach a live client at
    all)."""
    msg = OutboxMessage.from_wire(kind="__genuinely_unrecognized__", text="")
    body, _background = _body_and_background(msg)
    rendered = _render_to_text(body)
    assert rendered.strip(), f"rendered blank for an unrecognized kind: {rendered!r}"
    assert "__genuinely_unrecognized__" in rendered


def test_tui_presenter_never_renders_blank_when_kind_and_text_are_both_empty() -> None:
    """Tier 2: the degenerate case, on the real render path — the ONE case
    ``msg.text or " "`` could never have caught even by accident (an
    empty ``kind`` names nothing), reproduced via ``from_wire`` exactly as
    #6234's own wire-skew investigation would have seen it."""
    msg = OutboxMessage.from_wire(kind="", text="")
    body, _background = _body_and_background(msg)
    rendered = _render_to_text(body)
    assert rendered.strip(), f"rendered blank for kind='' text='': {rendered!r}"


def test_console_chat_renderer_never_writes_a_blank_line_for_an_unrecognized_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the plain (``--cui``) renderer used to write ``msg.text``
    RAW — an unrecognized kind with empty text wrote nothing but a
    newline. ``_write`` is overridden here (the renderer's OWN method, not
    an external collaborator) to capture what would have hit the
    terminal — the same "override the object under test's own IO seam"
    shape ``ConsoleChatRenderer`` already exposes it for (it exists
    precisely because writes bypass ``patch_stdout``)."""
    renderer = ConsoleChatRenderer()
    written: "list[str]" = []
    monkeypatch.setattr(renderer, "_write", written.append)

    msg = OutboxMessage.from_wire(kind="__genuinely_unrecognized__", text="")
    renderer.message(msg)

    [line] = written
    assert line.strip(), f"wrote a blank line: {line!r}"
    assert "__genuinely_unrecognized__" in line


def test_rich_chat_renderer_never_prints_a_blank_line_for_an_unrecognized_kind() -> None:
    """Tier 2: same guarantee on the Rich-backed plain renderer's OWN
    ``else`` branch (the per-kind styling fallback any unrecognized kind
    takes) — read back through its real ``_flush`` buffer, no IO
    interception needed since it already buffers before writing."""
    renderer = RichChatRenderer()
    msg = OutboxMessage.from_wire(kind="__genuinely_unrecognized__", text="")
    renderer._console.print(
        legible_degrade_text(msg.kind, msg.text), markup=False,
    )
    rendered = renderer._buffer.getvalue()
    assert rendered.strip(), f"rendered blank: {rendered!r}"
    assert "__genuinely_unrecognized__" in rendered


# ── group 3: the witness — is_unknown_kind / UnknownKindStats / emit ───────


def test_is_unknown_kind_true_for_a_genuinely_foreign_kind() -> None:
    """Tier 1: a kind outside DISPLAY_KINDS is unknown."""
    assert is_unknown_kind("__genuinely_unrecognized__") is True


def test_is_unknown_kind_false_for_a_real_display_kind() -> None:
    """Tier 1: falsification pair — a kind IN the vocabulary is not
    unknown, so an ordinary "agent" reply never trips the witness."""
    assert is_unknown_kind("agent") is False
    assert is_unknown_kind("status") is False


def test_unknown_kind_stats_record_is_first_occurrence_only_per_kind() -> None:
    """Tier 1: mirrors PumpSwallowStats.record's own dedup shape — the
    SAME kind returns True once, then False on every repeat, while count
    keeps growing (the caller's bounded-audit-event gate)."""
    stats = UnknownKindStats()
    first = stats.record("__weird__")
    second = stats.record("__weird__")
    third = stats.record("__weird__")
    assert (first, second, third) == (True, False, False)
    assert stats.count == 3


def test_unknown_kind_stats_a_different_kind_is_a_separate_first_occurrence() -> None:
    """Tier 1: falsification pair — dedup is keyed by kind, not global."""
    stats = UnknownKindStats()
    assert stats.record("__weird_a__") is True
    assert stats.record("__weird_b__") is True


def _read_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    found: "list[dict]" = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


async def _wait_for_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    """Poll for at least one matching event, unboundedly — no ``attempts=``
    ceiling, no ``sleep(N)`` (testing.md's own rules). Needed ONLY in an
    ASYNC test: ``EventStore.write`` (``core/events/event_store.py``)
    documents its own split — synchronous when no event loop is running,
    off-loop via a background ``DurabilityWorker`` whenever one IS (the
    exact situation every ``async def test_...`` in this file runs under)
    — so the write can genuinely still be in flight the instant an
    ``await run_output_loop(...)``/pilot call returns. ``asyncio.sleep(0)``
    is a cooperative yield (lets the worker's own task run a step), not a
    wait duration this poll depends on; CI's own ``--timeout=120`` is the
    kill switch if the write never lands."""
    while True:
        found = _read_events_of_kind(events_dir, kind)
        if found:
            return found
        await asyncio.sleep(0)


def test_record_unknown_kind_frame_emits_a_real_event_with_position_and_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: item 3's own acceptance — the record carries (1) where it
    arrived from (``transport_kind`` + ``surface``) and (2) where it was
    detected (``detected_at``, a REAL ``file:line`` captured via
    ``inspect``, not a hardcoded string that could drift from the actual
    call site). Real ``emit_cli_event`` + real ``.reyn/events`` read-back —
    no mock, matching ``test_5732_pump_swallow_visibility.py``'s own
    end-to-end idiom for the sibling mechanism."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    stats = UnknownKindStats()
    record_unknown_kind_frame(
        stats, "__genuinely_unrecognized__",
        ui_surface="plain_cui", transport_kind="QueueTransport",
    )

    events = _read_events_of_kind(reyn_dir / "events", "display_frame_unknown_kind")
    [event] = events
    assert event["data"]["frame_kind"] == "__genuinely_unrecognized__"
    assert event["data"]["ui_surface"] == "plain_cui"
    assert event["data"]["transport_kind"] == "QueueTransport"
    # The position must point at THIS file (the actual call site above),
    # never a fixed/hardcoded string unrelated to where the call was made.
    assert __file__ in event["data"]["detected_at"] or Path(__file__).name in (
        event["data"]["detected_at"]
    )
    assert ":" in event["data"]["detected_at"], "must carry a line number, not just a path"


def test_record_unknown_kind_frame_emits_once_for_a_repeat_but_keeps_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the same bounded-witness shape #5732 established — a kind
    that keeps arriving (a misrouted producer emitting the same wrong
    kind on every frame) durably records exactly ONE audit-event, while
    the public count keeps growing (charter Q1 — who bounds this if it
    repeats)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    stats = UnknownKindStats()
    for _ in range(5):
        record_unknown_kind_frame(
            stats, "__weird__", ui_surface="plain_cui", transport_kind="QueueTransport",
        )

    events = _read_events_of_kind(reyn_dir / "events", "display_frame_unknown_kind")
    [event] = events  # exactly one — unpack raises otherwise
    assert stats.count == 5


# ── group 4: end to end — a real TextualChatApp pump ────────────────────────


class _StubReadModel(ChatReadModel):
    """Mirrors ``test_5732_pump_swallow_visibility.py``'s own
    ``_StubReadModel`` — the minimal real (non-mock) read model
    ``TextualChatApp`` needs to construct and mount."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return {"model_active_class": "opus"}

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
    def history_path(self):
        return Path("/tmp/reyn_6230_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


@pytest.mark.asyncio
async def test_an_unrecognized_kind_through_the_real_pump_renders_and_is_witnessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: end to end through ``TextualChatApp._ingest_frame`` — a
    genuinely unrecognized kind, pushed as a real frame off a real
    ``QueueTransport`` (mirrors ``test_5732``'s own real-pump idiom), both
    (a) lands as a real conversation entry (never silently dropped — this
    stage never changed THAT, only what an unrecognized kind LOOKS like)
    and (b) durably records a ``display_frame_unknown_kind`` audit-event
    naming this kind and a real transport_kind.

    Public surface only: the render side is checked via ``app.conversation``
    (the ``FlowModel`` the widget itself renders from — the same public
    attribute every existing textual_chat test reads for entry counts),
    never a private stats attribute; the witness side is checked via the
    durable audit-event, the actual thing #6230's acceptance list names.

    Strip-falsify (observed): reverting the ``is_unknown_kind`` guard in
    ``_ingest_frame`` (commenting out the ``if``/call) makes the audit
    assertion below fail cleanly (``[event]`` unpacks an empty list) —
    NOT a hang; nothing here waits on an external condition the removed
    code was providing. Re-added the guard, reran, green."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(
            OutboxMessage.from_wire(kind="__genuinely_unrecognized__", text="")
        )
        await pilot.pause()
        await pilot.pause()

        # Public surface: the frame landed as a real conversation entry —
        # never silently dropped (this stage never changed THAT).
        kinds = [e.item.kind for e in app.conversation.entries]
        assert "__genuinely_unrecognized__" in kinds, kinds

    # `EventStore.write` (`core/events/event_store.py`) is off-loop
    # whenever an event loop is running (this whole test's own case) — the
    # write can genuinely still be in flight the instant the `async with`
    # block above exits, so this polls rather than reading once.
    [event] = await _wait_for_events_of_kind(
        reyn_dir / "events", "display_frame_unknown_kind",
    )
    assert event["data"]["frame_kind"] == "__genuinely_unrecognized__"
    assert event["data"]["ui_surface"] == "textual_chat"
    assert event["data"]["transport_kind"] == "QueueTransport"


@pytest.mark.asyncio
async def test_a_known_kind_never_trips_the_unknown_kind_witness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: falsification pair for the test above — an ORDINARY "agent"
    frame (already in the closed vocabulary) must never emit
    ``display_frame_unknown_kind``. Without this pair, the previous test
    alone would not distinguish "the witness correctly targets unknown
    kinds" from "the witness fires on every frame, known or not" — a
    strictly worse mechanism that would flood ``.reyn/events`` on
    ordinary traffic. Public surface only, same as the test above."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push(OutboxMessage(kind="agent", text="hello"))
        await pilot.pause()
        await pilot.pause()

        kinds = [e.item.kind for e in app.conversation.entries]
        assert "agent" in kinds, kinds

    events = _read_events_of_kind(reyn_dir / "events", "display_frame_unknown_kind")
    assert events == []


# ── plain (--cui) end to end, through run_output_loop ───────────────────────


class _OneFrameTransport(ClientTransportStub):
    """A real ``ClientTransport`` whose ``frames()`` yields exactly one
    unrecognized-kind frame, then ends — so ``run_output_loop`` (an ``async
    for`` over ``transport.frames()``) returns naturally with no ``__end__``
    sentinel needed."""

    def __init__(self, msg: OutboxMessage) -> None:
        self._msg = msg

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def has_session(self) -> bool:
        return True

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(self._msg)

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
        return False

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_plain_cui_output_loop_renders_and_witnesses_an_unrecognized_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: the plain (``--cui``, ``run_output_loop``) surface's own
    wiring — same shape as the TUI test above, driven through the real
    output loop instead of the pump, proving BOTH live surfaces got the
    witness call, not just the one the issue thread's own AttributeError
    incident happened to hit.

    Strip-falsify (observed): removing the ``is_unknown_kind`` guard in
    ``stream_client.py``'s ``_render_display_message`` makes this test
    HANG, not fail cleanly — ``_wait_for_events_of_kind`` polls
    unboundedly for an event that, with the guard gone, is never written
    at all; CI's own ``--timeout=120`` is the kill switch (mirrors
    ``test_6230_stage1_outbox_text_legibility.py``'s own precedent for
    disclosing a hang outcome in the test's own docstring). Re-added the
    guard, reran, green in ~3s."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    msg = OutboxMessage.from_wire(kind="__genuinely_unrecognized__", text="")
    transport = _OneFrameTransport(msg)
    renderer = ConsoleChatRenderer()
    written: "list[str]" = []
    monkeypatch.setattr(renderer, "_write", written.append)

    await run_output_loop(transport, renderer)

    assert written, "the frame must still be rendered"
    assert written[0].strip(), f"rendered a blank line: {written[0]!r}"

    # Same off-loop-write timing as the TUI test above — poll, don't read
    # once (`EventStore.write`'s own documented "off-loop whenever a loop
    # is running" split; this test's own event loop is exactly that case).
    [event] = await _wait_for_events_of_kind(
        reyn_dir / "events", "display_frame_unknown_kind",
    )
    assert event["data"]["frame_kind"] == "__genuinely_unrecognized__"
    assert event["data"]["ui_surface"] == "plain_cui"
    assert event["data"]["transport_kind"] == "_OneFrameTransport"
