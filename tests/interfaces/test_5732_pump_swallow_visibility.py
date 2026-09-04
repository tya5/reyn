"""Tier 2: #5732 (architect ruling, real-machine incident #5731 —
``_coalesce_pipeline_step`` raised ``AttributeError`` on EVERY ``"status"``-
kind frame, shipped, undetected: tests stayed green and nothing surfaced it
to a human) — ``TextualChatApp._pump_frames``'s 4 sibling ``except
Exception`` blocks (``/copy`` sentinel, ``/rewind`` sentinel,
``__open_artifact__`` sentinel, ``_ingest_frame``) must keep the app alive
(unchanged) AND make the swallowed failure legible: a complete, public
``PumpSwallowStats.count``, a status-line segment once it is nonzero, and
a bounded ``pump_exception_swallowed`` audit-event (one per first-seen
``(frame_kind, exception type)`` pair, never one per occurrence).

The ``/copy`` sentinel (``__copy_last_reply__``) was NOT in the architect's
own original 3-item enumeration below (nor lead-coder's brief) — e2e-coder
disclosed it as a 4th, identically-shaped sibling on the original PR;
lead-coder independently re-read the window their own census used, found
it excluded ``/copy`` by construction, and BLOCKED on folding it in now
rather than leaving "one catch still silent" open (the architect's own
"1 つだけ直すと残り 2 つが黙ったまま残ります" ruling holds at 3-of-4 too).

Architect's own 8-point acceptance (issuecomment on #5732), verbatim below
— written against 3 siblings; this PR applies the same ① at 4:
  ① all sibling catches treated alike (originally worded "3"; now 4 — see
     the ``/copy`` disclosure above)
  ② deliberately breaking ``_ingest_frame`` makes a test go RED
  ③ production behaviour unchanged (catch stays, loop survives)
  ④ no test-only branch (behaviour does not vary by environment)
  ⑤ the same ``(kind, exception type)`` failing 100x is 1 event, count 100
  ⑥ a DIFFERENT ``(kind, exception type)`` counts separately
  ⑦ no traceback on the operator surface; nothing shown at 0
  ⑧ the indicator does not disappear (not a transient toast)

No mocks: real ``PumpSwallowStats``, real ``TextualChatApp`` + ``run_test()``
pilot (mirrors ``tests/interfaces/test_5168_tui_stdio_capture.py``'s own
end-to-end idiom), real ``emit_cli_event`` + a real ``.reyn/events`` read-back
(mirrors ``tests/core/test_asyncio_diagnostics.py``'s own idiom) for the
audit-event witnesses. ``_ingest_frame`` is broken by monkeypatching the
method itself to raise (② above) — not by editing source — since the point
under test is the PUMP's own reaction to a broken handler, not the handler's
own internals.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from reyn.interfaces.inline.textual_chat.app import PumpSwallowStats
from reyn.interfaces.inline.textual_chat.chrome import status_line_text
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

# ---------------------------------------------------------------------------
# PumpSwallowStats — unit level (real instance, no App needed)
# ---------------------------------------------------------------------------


def test_record_returns_true_only_on_the_first_occurrence_of_a_pair() -> None:
    """Tier 2: acceptance ⑤'s dedup-gate witness — the SAME (kind,
    exception type) pair returns True once, then False on every further
    occurrence, regardless of how many times it recurs."""
    stats = PumpSwallowStats()
    first = stats.record("status", ValueError("boom"))
    second = stats.record("status", ValueError("boom again"))
    third = stats.record("status", ValueError("boom a third time"))
    assert first is True
    assert second is False
    assert third is False


def test_count_grows_every_occurrence_even_while_dedup_gates_the_event() -> None:
    """Tier 2: acceptance ⑤'s "count 100" half — the public count is
    COMPLETE (every occurrence), independent of the event-emission gate
    the ``record()`` return value drives."""
    stats = PumpSwallowStats()
    for _ in range(5):
        stats.record("status", ValueError("boom"))
    assert stats.count == 5


def test_a_different_exception_type_for_the_same_kind_is_a_separate_pair() -> None:
    """Tier 2: acceptance ⑥ — (kind, exception type) is the KEY, not kind
    alone. A second, DIFFERENT exception type for the same frame kind must
    get its own first-occurrence True, not be folded into the first pair's
    dedup."""
    stats = PumpSwallowStats()
    first = stats.record("status", ValueError("boom"))
    second = stats.record("status", AttributeError("different failure class"))
    assert first is True
    assert second is True, (
        "a different exception TYPE for the same kind must not be treated "
        "as a repeat of the first pair"
    )


def test_a_different_kind_for_the_same_exception_type_is_a_separate_pair() -> None:
    """Tier 2: acceptance ⑥, the other axis — kind is also part of the key,
    not just the exception type."""
    stats = PumpSwallowStats()
    first = stats.record("status", ValueError("boom"))
    second = stats.record("__open_artifact__", ValueError("boom"))
    assert first is True
    assert second is True, (
        "a different frame kind with the same exception type must not be "
        "treated as a repeat of the first pair"
    )


# ---------------------------------------------------------------------------
# status_line_text — pure-function level
# ---------------------------------------------------------------------------


def test_status_line_unaffected_when_pump_swallow_count_is_zero() -> None:
    """Tier 2: acceptance ⑦'s "nothing shown at 0" half — the default is
    byte-identical to the pre-#5732 text."""
    text_before = status_line_text({"model_active_class": "opus"}, "alpha")
    text_default = status_line_text(
        {"model_active_class": "opus"}, "alpha", pump_swallow_count=0,
    )
    assert text_before == text_default
    assert "draw failed" not in text_default


def test_status_line_prepends_draw_failed_segment_when_count_is_nonzero() -> None:
    """Tier 2: acceptance ⑦'s "N — see log" half; acceptance ⑧ — the
    segment names a count, never a traceback or exception message."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha", pump_swallow_count=3,
    )
    assert text.startswith("draw failed: 3 — see log"), text
    assert "opus" in text, "the ordinary status text must still follow"
    assert "Traceback" not in text
    assert "Error" not in text


def test_status_line_pump_swallow_coexists_with_diagnostics_segment() -> None:
    """Tier 2: two independently-gated segments (#5168's diagnostics_count,
    #5732's pump_swallow_count) must not silently swallow each other when
    both are nonzero at once."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        diagnostics_count=1, pump_swallow_count=2,
    )
    assert "draw failed: 2" in text
    assert "diagnostics: 1" in text


# ---------------------------------------------------------------------------
# _record_pump_swallow — real emit_cli_event, real .reyn/events read-back
# ---------------------------------------------------------------------------


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


class _StubTransport(ClientTransportStub):
    """Mirrors ``test_5168_tui_stdio_capture.py``'s own ``_StubTransport`` —
    no attach machinery needed, just an app that mounts and stays running."""

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def has_session(self) -> bool:
        return True

    def attach_failed(self) -> bool:
        return False

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        import asyncio
        await asyncio.Event().wait()
        return
        yield DisplayFrame(OutboxMessage(kind="status", text=""))  # pragma: no cover

    async def submit_user_text(self, text: str) -> str:
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


class _StubReadModel(ChatReadModel):
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
        return Path("/tmp/reyn_5732_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


def test_record_pump_swallow_emits_a_real_event_with_no_parameter_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: REAL regression witness for the ``emit_cli_event(kind=...)``
    collision this PR fixed (``emit_cli_event``'s own first positional
    parameter is ALSO named ``kind`` — passing the frame kind under that
    same name raises ``TypeError: emit_cli_event() got multiple values for
    argument 'kind'``). Calling the real, unmocked path end to end is the
    only way this class of bug is caught — a mocked ``emit_cli_event``
    would not raise on a bad keyword the mock itself accepts anything for."""
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp

    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=_StubTransport(), read_model=_StubReadModel())
    app._record_pump_swallow("status", AttributeError("no attribute 'append'"))

    events = _read_events_of_kind(reyn_dir / "events", "pump_exception_swallowed")
    [event] = events
    assert event["data"]["frame_kind"] == "status"
    assert event["data"]["exception_type"] == "AttributeError"


def test_record_pump_swallow_emits_once_for_a_repeated_pair_but_keeps_counting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ⑤ end to end — the SAME (kind, exception type)
    failing repeatedly (the #5731 shape: a broken call site fails every
    frame) durably records exactly ONE audit-event, while the public count
    keeps growing."""
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp

    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=_StubTransport(), read_model=_StubReadModel())
    stats = PumpSwallowStats()
    app._pump_swallow_stats = stats  # mirrors test_5168's own app._stray_output_stats wiring
    for _ in range(5):
        app._record_pump_swallow("status", AttributeError("append"))

    events = _read_events_of_kind(reyn_dir / "events", "pump_exception_swallowed")
    [event] = events  # exactly one event captured — unpack raises otherwise
    assert event["data"]["exception_type"] == "AttributeError"
    assert stats.count == 5


def test_record_pump_swallow_emits_separately_for_a_different_exception_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ⑥ end to end — a second, different (kind,
    exception type) pair gets its own durable event."""
    from reyn.interfaces.inline.textual_chat.app import TextualChatApp

    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=_StubTransport(), read_model=_StubReadModel())
    app._record_pump_swallow("status", AttributeError("append"))
    app._record_pump_swallow("status", ValueError("a different failure class"))

    events = _read_events_of_kind(reyn_dir / "events", "pump_exception_swallowed")
    [event_a, event_b] = events  # exactly two events captured — unpack raises otherwise
    assert {event_a["data"]["exception_type"], event_b["data"]["exception_type"]} == {
        "AttributeError", "ValueError",
    }


# ---------------------------------------------------------------------------
# End-to-end — a real TextualChatApp, real run_test() pilot, a genuinely
# broken _ingest_frame (the #5731 shape) driving a real frame through the
# pump.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_ingest_frame_is_visible_in_the_status_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ② — the PR's own "body". Deliberately breaking
    ``_ingest_frame`` (mirrors #5731's real ``AttributeError`` on every
    "status"-kind frame) must turn THIS test red were the #5732 fix ever
    reverted: with the fix in place, the swallowed failure surfaces on the
    status line; strip :meth:`TextualChatApp._record_pump_swallow`'s call
    site (or revert it to a bare ``except Exception:``) and this assertion
    fails — exactly the gap #5731 shipped through undetected."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _StubTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    def _broken_ingest(self, msg):
        raise AttributeError("'_CursorFlowView' object has no attribute 'append'")

    monkeypatch.setattr(TextualChatApp, "_ingest_frame", _broken_ingest)

    async def _one_status_frame() -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(OutboxMessage(kind="status", text="thinking"))
        import asyncio
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "frames", _one_status_frame)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()

        # Public read, per the architect's own design ("count は公開の読みに
        # 載る") — the status line, not a private ._pump_swallow_stats access.
        status_text = str(app.query_one(StatusLine).render())
        assert "draw failed: 1" in status_text, (
            f"a swallowed ingest failure did not surface on the status "
            f"line: {status_text!r}"
        )


@pytest.mark.asyncio
async def test_the_pump_survives_a_broken_ingest_frame_and_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: acceptance ③ — production behaviour is UNCHANGED: the catch
    stays, and a broken frame handler does not end the pump's own loop. A
    SECOND, ordinary frame after the broken one must still be ingested."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _StubTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    real_ingest = TextualChatApp._ingest_frame
    call_kinds: "list[str]" = []

    def _fails_once_then_delegates(self, msg):
        call_kinds.append(msg.kind)
        if msg.kind == "status":
            raise AttributeError("'_CursorFlowView' object has no attribute 'append'")
        return real_ingest(self, msg)

    monkeypatch.setattr(TextualChatApp, "_ingest_frame", _fails_once_then_delegates)

    async def _two_frames() -> "AsyncIterator[DisplayFrame]":
        yield DisplayFrame(OutboxMessage(kind="status", text="thinking"))
        yield DisplayFrame(OutboxMessage(kind="agent", text="hello"))
        import asyncio
        await asyncio.Event().wait()

    monkeypatch.setattr(transport, "frames", _two_frames)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()

        assert call_kinds == ["status", "agent"], (
            "the pump must keep draining frames after one handler raised — "
            f"got {call_kinds!r}"
        )
        # Public read, mirrors the sibling test above — only the broken
        # (status) frame should have been recorded, not the ordinary one.
        status_text = str(app.query_one(StatusLine).render())
        assert "draw failed: 1" in status_text, (
            f"only the broken (status) frame should have been recorded: "
            f"{status_text!r}"
        )
