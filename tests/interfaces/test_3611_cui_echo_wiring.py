"""Tier 2: the plain ``--cui`` echo-branch WIRING, not the physical paint (#3611).

The issue's own finding: ``echo=False`` (the TTY leg — ``prompt_session.
prompt_async`` already left the line on screen, #3287) has exactly one LIVE
observation on a real terminal and no automated coverage, so a regression
there would ship silently. A real ``pty`` spike (posted on the issue) proved
this CAN be witnessed by machine in principle, but driving one with real
keystrokes races the kernel's own canonical-mode echo against
``prompt_toolkit``'s raw-mode transition and produces a spurious double-print
that looks exactly like the defect being hunted — building that into
permanent CI coverage would trade "no coverage" for "coverage that cries
wolf," the SAME silent-regression shape from a different door (and if the
runner's ``pty`` support ever goes missing, an un-skipped-but-silently-broken
gate reads as coverage that was never really there).

What is proven below instead, per the steer: the two tests assert
``run_input_loop``'s REAL ``is_tty`` computation (``sys.stdin.isatty()``,
never faked — a real pty fd for the TTY leg, a real OS pipe for the non-TTY
leg) threads through to ``route_input_line``'s ``terminal_echoed`` and then to
``maybe_dispatch_slash``'s ``echo`` with the correct value, end to end,
through the REAL production call chain. ``prompt_toolkit``'s own
``create_pipe_input``/``DummyOutput`` (its documented, official headless-test
mechanism — no real terminal, no keystroke timing, no kernel tty layer
involved) drives the TTY leg's ``prompt_async`` deterministically, so there is
no settle-race here to misread as a false red.

**What this does NOT prove**: that a physical terminal, given
``echo=False``, paints the submitted line exactly once. That claim stays a
single live observation (tui-coder, on the issue) — these tests stop at "the
branch received the value production intends it to receive," not "the pixels
on a real screen matched." A future regression in ``prompt_toolkit``'s own
render behavior, or a swap to a different prompt library, would not be
caught here.

Policy compliance (docs/deep-dives/contributing/testing.md): no
unittest.mock/MagicMock/AsyncMock/patch on a collaborator — a real ``Session``
(``tests/_support/agent_session.make_session``), a real ``InProcessTransport``
(``tests/_support/slash.local_transport``), a real ``pty``/OS pipe for
``sys.stdin``, and ``prompt_toolkit``'s own real, documented test input/output
implementations. No private-state assertions — behavior observed through the
transport's own display queue.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from prompt_toolkit import PromptSession
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.renderer import ChatRenderer
from reyn.interfaces.repl.stream_client import run_input_loop
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from tests._support.agent_session import make_session
from tests._support.slash import drain_display


def _transport_that_survives_eof_shutdown(session) -> "tuple[InProcessTransport, asyncio.Queue]":
    """Like ``tests._support.slash.local_transport``, plus a real (no-op)
    ``shutdown`` on the registry stand-in — ``run_input_loop``'s own EOF
    branch (both legs below end via a real EOF, not a manual stop) calls
    ``transport.shutdown()``, which the shared helper's stand-in does not
    implement (nothing in this test suite previously drove ``run_input_loop``
    all the way to its own EOF exit)."""
    repl_outbox: asyncio.Queue = asyncio.Queue()

    async def _shutdown() -> None:
        pass

    transport = InProcessTransport(
        SimpleNamespace(
            attached_session=lambda: session,
            repl_outbox=repl_outbox,
            shutdown=_shutdown,
        ),
        intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )
    return transport, repl_outbox


class _SilentRenderer(ChatRenderer):
    """A real, minimal ``ChatRenderer`` (every method is independently
    overridable with no-op defaults per the base class's own contract) —
    silences ``message()`` so the test never writes to the real terminal
    (the base class's own implementation targets ``sys.__stdout__``
    directly); ``prompt_text``/``bottom_toolbar`` keep the base defaults."""

    def message(self, msg) -> None:
        pass


async def _drive_one_line(
    monkeypatch, tmp_path, *, is_tty: bool, line: str,
) -> "list[str]":
    """Run the REAL ``run_input_loop`` for exactly one submitted line, then
    EOF, and return what landed on the display as a ``user``-kind message.

    The TTY leg uses a real ``pty`` fd purely so ``sys.stdin.isatty()`` is
    genuinely ``True`` (never faked) — ``prompt_async`` itself is driven by
    ``prompt_toolkit``'s own ``create_pipe_input``, not by typing through the
    pty, so there is no kernel-echo/raw-mode timing race to misread as a
    false positive (the exact risk a REAL driven pty carries, per the
    module docstring).
    """
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="cui-echo-wiring-3611",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )
    transport, display = _transport_that_survives_eof_shutdown(session)
    renderer = _SilentRenderer()

    if is_tty:
        master_fd, slave_fd = os.openpty()
        stdin_file = os.fdopen(slave_fd, "r")
        monkeypatch.setattr("sys.stdin", stdin_file)
        try:
            with create_pipe_input() as pipe_input:
                pipe_input.send_text(f"{line}\r")
                pipe_input.send_text("\x04")  # Ctrl-D: EOFError on the next prompt
                prompt_session = PromptSession(input=pipe_input, output=DummyOutput())
                await asyncio.wait_for(
                    run_input_loop(transport, prompt_session, renderer), timeout=5.0,
                )
        finally:
            stdin_file.close()
            os.close(master_fd)
    else:
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "r")
        writer = os.fdopen(write_fd, "w")
        writer.write(line + "\n")
        writer.close()  # EOF right after the one line
        monkeypatch.setattr("sys.stdin", reader)
        try:
            # Unused on this branch (the non-TTY path reads sys.stdin.readline
            # directly) — passed only because run_input_loop requires one.
            prompt_session = PromptSession(
                input=create_pipe_input().__enter__(), output=DummyOutput(),
            )
            await asyncio.wait_for(
                run_input_loop(transport, prompt_session, renderer), timeout=5.0,
            )
        finally:
            reader.close()

    return [m.text for m in drain_display(display) if m.kind == "user"]


@pytest.mark.asyncio
async def test_tty_input_loop_wires_terminal_echoed_true_no_reprint(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: #3611 wiring pin — is_tty=True (genuine, via a real pty fd)
    reaches maybe_dispatch_slash as echo=False through the REAL run_input_loop
    -> route_input_line chain, so the command line is not re-printed on top
    of what a real terminal's own render already put on screen (#3287,
    through this door)."""
    echoed = await _drive_one_line(monkeypatch, tmp_path, is_tty=True, line="/cost")
    assert echoed == [], (
        f"a TTY session re-printed the command line — is_tty is no longer "
        f"reaching maybe_dispatch_slash as echo=False: {echoed!r}"
    )


@pytest.mark.asyncio
async def test_non_tty_input_loop_wires_terminal_echoed_false_and_echoes(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: #3611 wiring pin, the other leg — is_tty=False (a real OS
    pipe, genuinely not a tty) reaches maybe_dispatch_slash as echo=True, so
    a piped session's only record of the command is this echo. Both legs are
    required: a broken implementation that always echoes, or never echoes,
    would satisfy only one of these two tests on its own."""
    echoed = await _drive_one_line(monkeypatch, tmp_path, is_tty=False, line="/cost")
    assert echoed == ["/cost"], (
        f"a piped session did not echo the command line — its only record "
        f"of what was asked is gone: {echoed!r}"
    )
