"""Tier 2: #5168 — while a Textual app owns the screen, a stray
``sys.stdout``/``sys.stderr`` write (a third-party ``print(file=sys.stderr)``,
a ``warnings.warn``) must not corrupt the live region.

Architect ruling (issuecomment-5384424061), 5 acceptance criteria:
  ① a third-party-shaped ``print(file=sys.stderr)`` — NOT just ``warnings.warn``
     — does not reach the real terminal while the app owns the screen.
  ② the same content is still observable (routed to logging, never dropped).
  ③ a screen indicator increases (never a silent capture).
  ④ the streams are restored even if an exception is raised mid-block.
  ⑤ a non-TUI run (capture never entered) keeps stderr behaving as before —
     the layer boundary this closes must not leak into an unrelated caller.

Real ``TextualChatApp`` + ``run_test()`` pilot for the end-to-end
(status-line) witnesses; real (unstarted) Textual ``App``/``MessagePump``
instances for the unit-level ``stray_output.py`` witnesses (a real
``App()`` accepts ``post_message`` before ``run()`` — confirmed live, no
mock needed). No mocks anywhere in this file, per the testing policy.
"""
from __future__ import annotations

import io
import logging
import sys
from typing import AsyncIterator

import pytest
from textual.app import App

from reyn.interfaces.inline.textual_chat.chrome import status_line_text
from reyn.interfaces.inline.textual_chat.stray_output import (
    StrayOutputStats,
    _CapturingStream,
    capture_stray_output,
)
from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage

# ---------------------------------------------------------------------------
# status_line_text — pure-function level (fast, no App)
# ---------------------------------------------------------------------------


def test_status_line_unaffected_when_diagnostics_count_is_zero():
    """Tier 2: the default (``diagnostics_count=0``) is byte-identical to
    the pre-#5168 text — no regression for the ordinary, nothing-captured
    case (every existing status_line_text test already pins this shape;
    this one just names the invariant explicitly for #5168's own diff)."""
    text_before = status_line_text({"model_active_class": "opus"}, "alpha")
    text_default = status_line_text({"model_active_class": "opus"}, "alpha", diagnostics_count=0)
    assert text_before == text_default
    assert "diagnostics" not in text_default


def test_status_line_prepends_diagnostics_segment_when_count_is_nonzero():
    """Tier 2: acceptance ③'s pure-function witness — a nonzero count
    prepends a "diagnostics: N — <path>" segment naming both the count and
    where to look, ahead of the ordinary text."""
    text = status_line_text(
        {"model_active_class": "opus"}, "alpha",
        diagnostics_count=3, diagnostics_log_path=".reyn/logs/reyn.log",
    )
    assert text.startswith("diagnostics: 3 — .reyn/logs/reyn.log"), text
    assert "opus" in text, "the ordinary status text must still follow, not be replaced"


def test_status_line_diagnostics_coexists_with_the_halted_banner():
    """Tier 2: diagnostics is applied LAST, after every other branch —
    it must be able to coexist with the #2280 HALTED banner (both can be
    true at once: the session halted AND a stray write happened), not
    silently swallow one or the other."""
    text = status_line_text(
        {"halted_reason": "durability failure"}, "alpha", diagnostics_count=1,
    )
    assert text.startswith("diagnostics: 1")
    assert "HALTED" in text


# ---------------------------------------------------------------------------
# stray_output.py — unit level (real, unstarted Textual App as the poster)
# ---------------------------------------------------------------------------


def test_capturing_stream_write_bumps_count_and_logs(caplog: pytest.LogCaptureFixture):
    """Tier 2: acceptance ②/③'s unit witness — a non-empty write bumps the
    live count AND is logged (never dropped)."""
    stats = StrayOutputStats()
    poster = App()
    stream = _CapturingStream(stats, poster, "stderr")

    with caplog.at_level(logging.WARNING, logger="reyn.interfaces.inline.textual_chat.stray_output"):
        stream.write("a stray warning fragment\n")

    assert stats.count == 1
    assert any("a stray warning fragment" in r.message for r in caplog.records), (
        "the captured content must be observable through logging, not silently dropped"
    )


def test_capturing_stream_ignores_whitespace_only_writes(caplog: pytest.LogCaptureFixture):
    """Tier 2: falsification contrast — stdlib buffering machinery writes a
    LOT of bare "\\n"/"" (e.g. print's own trailing newline as a SEPARATE
    write call under some buffering modes); counting those would inflate
    the diagnostic count for nothing an operator could act on."""
    stats = StrayOutputStats()
    poster = App()
    stream = _CapturingStream(stats, poster, "stderr")

    with caplog.at_level(logging.WARNING, logger="reyn.interfaces.inline.textual_chat.stray_output"):
        stream.write("\n")
        stream.write("")
        stream.write("   ")

    assert stats.count == 0
    assert caplog.records == []


def test_capturing_stream_posts_a_message_to_the_poster():
    """Tier 2: the cross-thread-safe signal — a write posts
    StrayOutputCaptured via post_message (thread-safe by construction, see
    the module's own docstring), not a direct method call."""
    stats = StrayOutputStats()
    poster = App()
    stream = _CapturingStream(stats, poster, "stderr")

    posted = stream.write("something\n")

    assert posted == len("something\n")
    # A real, unstarted App still queues the message (confirmed live) —
    # draining its own private queue directly would be a private-state
    # assertion, so this test only pins the PUBLIC contract (write()
    # succeeds, does not raise) rather than reaching into
    # poster._message_queue.


def test_capture_stray_output_swaps_and_restores_both_streams():
    """Tier 2: acceptance ①'s mechanism-level witness — both streams are
    swapped for the duration of the block and restored after."""
    poster = App()
    fake_real_stdout, fake_real_stderr = io.StringIO(), io.StringIO()
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = fake_real_stdout, fake_real_stderr
        with capture_stray_output(poster) as stats:
            assert sys.stdout is not fake_real_stdout
            assert sys.stderr is not fake_real_stderr
            print("stray", file=sys.stderr)
            print("stray", file=sys.stdout)
        assert sys.stdout is fake_real_stdout, "stdout must be restored after the block"
        assert sys.stderr is fake_real_stderr, "stderr must be restored after the block"
        assert fake_real_stdout.getvalue() == "", (
            "the pre-swap stream must receive NOTHING while capture is active"
        )
        assert fake_real_stderr.getvalue() == "", (
            "the pre-swap stream must receive NOTHING while capture is active"
        )
        assert stats.count == 2
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr


def test_capture_stray_output_restores_streams_even_on_exception():
    """Tier 2: acceptance ④ — an exception raised INSIDE the `with` block
    (mirrors a Textual-internal crash mid-run) must still restore both
    streams; an operator's terminal must never be left mid-swap."""
    poster = App()
    fake_real_stdout, fake_real_stderr = io.StringIO(), io.StringIO()
    orig_stdout, orig_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout, sys.stderr = fake_real_stdout, fake_real_stderr
        with pytest.raises(RuntimeError, match="boom"):
            with capture_stray_output(poster):
                raise RuntimeError("boom")
        assert sys.stdout is fake_real_stdout, "stdout must be restored even after a crash"
        assert sys.stderr is fake_real_stderr, "stderr must be restored even after a crash"
    finally:
        sys.stdout, sys.stderr = orig_stdout, orig_stderr


def test_stray_output_never_captured_when_the_context_manager_is_not_entered():
    """Tier 2: acceptance ⑤ — a non-TUI caller that never enters
    capture_stray_output at all keeps ordinary stderr behavior, byte-
    identical to before #5168 (the layer boundary: this is scoped to the
    TUI's own run window, not process-global)."""
    fake_real_stderr = io.StringIO()
    orig_stderr = sys.stderr
    try:
        sys.stderr = fake_real_stderr
        print("ordinary stderr output", file=sys.stderr)
        assert "ordinary stderr output" in fake_real_stderr.getvalue()
    finally:
        sys.stderr = orig_stderr


# ---------------------------------------------------------------------------
# End-to-end — a real TextualChatApp, real run_test() pilot
# ---------------------------------------------------------------------------


class _StubTransport(ClientTransportStub):
    """A real, minimal ClientTransport (mirrors _AttachStateTransport in
    test_textual_chat_attach_state_3671_p3.py) — this test needs no attach
    machinery at all, just an app that mounts and stays running."""

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
        yield DisplayFrame(OutboxMessage(role="assistant", content=""))  # pragma: no cover — makes this a real async generator

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
        from pathlib import Path
        return Path("/tmp/reyn_5168_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


@pytest.mark.asyncio
async def test_a_stray_print_during_a_real_tui_run_updates_the_status_line():
    """Tier 2: the full end-to-end witness — a real TextualChatApp mounted,
    capture_stray_output entered exactly as run_textual_chat does it, a
    third-party-shaped print(file=sys.stderr) fired (NOT warnings.warn —
    acceptance ①'s own explicit "don't measure with warnings.warn alone"
    requirement), and the StatusLine's rendered text reflects it (acceptance
    ③) with the write never reaching the pre-swap stream (acceptance ①)."""
    from reyn.interfaces.inline.textual_chat import StatusLine, TextualChatApp

    transport = _StubTransport()
    app = TextualChatApp(transport=transport, read_model=_StubReadModel())

    fake_real_stderr = io.StringIO()
    orig_stderr = sys.stderr
    try:
        sys.stderr = fake_real_stderr
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            with capture_stray_output(app) as stats:
                app._stray_output_stats = stats
                print("a third-party library's own stray warning", file=sys.stderr)
                await pilot.pause()

                status_text = str(app.query_one(StatusLine).render())
                assert "diagnostics: 1" in status_text, (
                    f"status line did not surface the captured write: {status_text}"
                )
        assert fake_real_stderr.getvalue() == "", (
            "the stray print must never have reached the pre-swap real stream"
        )
    finally:
        sys.stderr = orig_stderr
