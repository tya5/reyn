"""Tier 2: the #3287 own-echo suppression WIRING through run_chat_client (#3611 item 3).

#3611's investigation item 3 asked whether #3287's original own-echo defense
(the fix this session's #3611 slash-echo work is siblings with) has the same
kind of automated coverage its own ``is_tty``-driven wiring point needs. It
does not: ``client_driver.run_chat_client``'s

    own_submissions = set() if (is_tty and own_connection_id is None) else None

is computed from the SAME caller-supplied ``is_tty`` boolean the slash-echo
fix reads, and every existing ``test_stream_client_own_echo_3287.py`` test
drives ``run_output_loop``/``route_input_line`` with ``own_submissions``
handed in directly — never through this construction. A regression here
(``is_tty`` stops reaching this line, or the condition itself breaks) would
ship silently, exactly the shape #3611 exists to close.

Real-pty production coverage is rejected for the SAME two reasons #3611's own
PR (#3744) already established: (1) driving prompt_toolkit's raw-mode
rendering with real typed keystrokes races the kernel's own canonical-mode
echo and produces a false-positive double-render indistinguishable from the
defect being hunted; (2) CI's pty support is unconfirmed, so a silently-broken
gate would read as "covered."

Differs from #3611's own approach in TWO ways worth naming:

1. ``run_input_loop`` takes an externally-constructed ``PromptSession`` as a
   parameter (so a test can hand it one wired to ``prompt_toolkit``'s
   ``create_pipe_input``), but ``run_chat_client`` constructs its OWN
   ``PromptSession()`` internally with no injection point.
   ``prompt_toolkit.PromptSession`` itself (a THIRD-PARTY class, not a reyn
   collaborator) is therefore monkeypatched to force the SAME
   ``create_pipe_input``/``DummyOutput`` construction ``run_chat_client``'s
   own code would otherwise be unable to accept — its own logic runs
   completely unmodified; only where ``PromptSession()``'s input/output
   resolve from is redirected, the same category of "use the library's own
   test seam" technique ``create_pipe_input``/``DummyOutput`` already are.

2. **A real bug found while building this test, worth recording**: a first
   draft used ``AgentRegistry`` + ``await registry.attach(name)`` (the
   pattern ``test_startup_client_before_attach_3671_p2.py`` already
   established). ``attach()``'s own docstring says it plainly — "Boot
   ``session.run()`` ... on first attach" — which starts the REAL router
   loop as a background task. That loop, given enough wall-clock time, pulls
   the just-submitted turn off the inbox and reaches a genuine
   ``litellm.acompletion`` call; the repo's own network gate
   (``reyn.dev.testing.network_gate``) caught it on an UNRELATED falsify run
   (forcing ``own_submissions=None`` happened to change timing enough for
   the router loop to win the race before teardown). The two tests below had
   already been GREEN several times before that — passing only because the
   test's own teardown usually beat the background router loop to the
   punch, not because nothing was racing. Fixed by never calling
   ``AgentRegistry.attach()`` at all: a lightweight stand-in
   (:class:`_NoRouterLoopRegistry`) provides exactly the surface
   ``InProcessTransport``/``RegistryReadModel`` read (``attached_session``,
   ``repl_outbox``, the SAME focus-listener wiring
   ``AgentRegistry.bind_focus_listeners``/``unbind_focus_listeners``
   themselves perform, ``shutdown``, ``attach_failed``) around a real,
   directly-constructed ``Session`` — with no router loop ever started, so
   there is nothing left to race. This test only needs the SYNCHRONOUS
   ``user_submitted`` chat-event ``Session.submit_user_text`` emits on
   every call (before any inbox consumption); it was never testing turn
   completion in the first place.

**What this does NOT prove**: that a physical terminal, given the correct
``own_submissions`` value, paints the submitted line exactly once and the
broadcast echo does not layer under/over it. That stays a single live
observation (per #3611, unchanged for this sibling mechanism) — this only
guards the WIRING from ``is_tty`` through to whether the broadcast
``user_submitted`` chat-event gets suppressed or rendered.

Policy compliance (docs/deep-dives/contributing/testing.md): no
unittest.mock/MagicMock/AsyncMock/patch on a reyn collaborator — a real
``Session`` (``tests/_support/agent_session.make_session``), a real
``InProcessTransport``, a real pty fd for ``sys.stdin.isatty()``, and
``prompt_toolkit``'s own real, documented headless test input/output
implementations. ``_NoRouterLoopRegistry`` reproduces (not fakes)
``AgentRegistry``'s own focus-listener glue against a real ``Session`` — the
same shape ``tests/_support/slash.py``'s ``local_transport`` already uses for
a narrower surface. No private-state assertions — behavior observed through
a recording ``ConsoleChatRenderer`` subclass's own public ``messages`` list.
"""
from __future__ import annotations

import asyncio
import os

import prompt_toolkit
import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from reyn.interfaces.repl.client_driver import run_chat_client
from reyn.interfaces.repl.read_model import RegistryReadModel
from reyn.interfaces.repl.renderer import ConsoleChatRenderer
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from tests._support.agent_session import make_session


class _NoRouterLoopRegistry:
    """Exactly the surface ``InProcessTransport``/``RegistryReadModel`` read
    off a registry, backed by a real, directly-constructed ``Session`` — with
    NO ``AgentRegistry.attach()`` ever called, so ``session.run()`` (the
    router loop) never starts. See the module docstring's point 2: an
    earlier draft using a real ``AgentRegistry`` raced that background loop
    against test teardown.

    ``bind_focus_listeners``/``unbind_focus_listeners`` reproduce
    ``AgentRegistry``'s own glue (``registry.py``, same two methods) exactly
    — not a stand-in for untested behavior, the actual few lines those
    methods run, against the same real ``Session``.

    **A second real gotcha found building this test, worth recording**: with
    no router loop, nothing ever produces a real reply — but
    ``run_output_loop``'s ``reply_seen`` pacing gate (``stream_client.py``)
    is only ``.set()`` on an ``"agent"``/``"error"`` frame, and the non-TTY
    ``run_input_loop`` branch blocks on that gate BEFORE every read past the
    first. Without a reply, that wait never resolves and the loop hangs
    forever past one line — not a defect in the wiring under test, just this
    stand-in's own absence of the piece an ordinary turn would eventually
    produce. Fixed by having the chat-event forward path append a synthetic
    ``"agent"`` ack to ``repl_outbox`` the SAME synchronous call that
    forwards ``user_submitted`` — real production code (``session.py``'s
    ``_chat_events.emit``) calls every subscriber synchronously, so this
    ack is guaranteed to be queued before ``route_input_line`` (and thus
    ``submit_user_text``) returns, landing well before the input loop's next
    pacing check. Its own text is never asserted on (only ``kind == "user"``
    messages are), so it cannot be mistaken for what this test verifies.
    """

    def __init__(self, session) -> None:
        self._session = session
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        self._on_chat_event = None

    def attached_session(self):
        return self._session

    def attach_failed(self) -> bool:
        return False

    def bind_focus_listeners(self, *, on_chat_event=None, intervention_channel=None) -> None:
        if on_chat_event is not None:
            def _forward_then_synthetic_ack(event) -> None:
                on_chat_event(event)
                if getattr(event, "type", None) == "user_submitted":
                    from reyn.runtime.outbox import OutboxMessage
                    self.repl_outbox.put_nowait(OutboxMessage(kind="agent", text=""))

            self._on_chat_event = _forward_then_synthetic_ack
            self._session.subscribe_chat_events(_forward_then_synthetic_ack)
        else:
            self._on_chat_event = None
        if intervention_channel is not None:
            try:
                self._session.register_intervention_listener(intervention_channel)
            except AttributeError:
                pass

    def unbind_focus_listeners(self) -> None:
        if self._on_chat_event is not None:
            self._session.unsubscribe_chat_events(self._on_chat_event)
        self._on_chat_event = None

    async def shutdown(self) -> None:
        pass


class _RecordingConsoleRenderer(ConsoleChatRenderer):
    """A real ``ConsoleChatRenderer`` — its ``on_chat_event`` dispatch (the
    production logic that decides WHEN a ``user_submitted`` event turns into
    a displayed message) runs completely unmodified. Only ``message``/
    ``_write`` are overridden, because the base class writes straight to
    ``sys.__stdout__`` (unusable in a test) rather than anywhere this test
    can observe."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: "list" = []

    def _write(self, s: str) -> None:  # pragma: no cover - silences banner/clear
        pass

    def message(self, msg) -> None:
        self.messages.append(msg)


async def _drive_one_submission(
    monkeypatch, tmp_path, *, is_tty: bool, text: str,
) -> "list[str]":
    """Run the REAL ``run_chat_client`` for exactly one submitted (non-slash)
    line, then EOF, and return every ``user``-kind message that reached the
    renderer.

    The pty fd exists purely so ``sys.stdin.isatty()`` is genuinely ``True``
    for the TTY leg (never faked) — the INTERNAL ``PromptSession`` that
    ``run_chat_client`` constructs is redirected (via the monkeypatched
    ``prompt_toolkit.PromptSession``) to ``prompt_toolkit``'s own
    ``create_pipe_input``, so there is no keystroke/raw-mode timing race.
    """
    monkeypatch.chdir(tmp_path)
    session = make_session(
        agent_name="own-submissions-wiring-3611",
        snapshot_path=tmp_path / "snap.json",
    )
    registry = _NoRouterLoopRegistry(session)
    transport = InProcessTransport(registry, intervention_channel=DEFAULT_CHAT_CHANNEL_ID)
    transport.start()
    read_model = RegistryReadModel(registry)
    renderer = _RecordingConsoleRenderer()

    real_prompt_session_cls = prompt_toolkit.PromptSession

    def _pipe_backed_prompt_session(*args, **kwargs):
        kwargs["input"] = pipe_input
        kwargs["output"] = DummyOutput()
        return real_prompt_session_cls(*args, **kwargs)

    try:
        if is_tty:
            master_fd, slave_fd = os.openpty()
            stdin_file = os.fdopen(slave_fd, "r")
            monkeypatch.setattr("sys.stdin", stdin_file)
            try:
                with create_pipe_input() as pipe_input:
                    pipe_input.send_text(f"{text}\r")
                    pipe_input.send_text("\x04")  # Ctrl-D -> EOFError, ends the loop
                    monkeypatch.setattr(
                        prompt_toolkit, "PromptSession", _pipe_backed_prompt_session,
                    )
                    await asyncio.wait_for(
                        run_chat_client(
                            transport=transport, renderer=renderer,
                            read_model=read_model, agent_name="own-submissions-wiring-3611",
                            is_tty=True,
                        ),
                        timeout=5.0,
                    )
            finally:
                stdin_file.close()
                os.close(master_fd)
        else:
            read_fd, write_fd = os.pipe()
            reader = os.fdopen(read_fd, "r")
            writer = os.fdopen(write_fd, "w")
            writer.write(text + "\n")
            writer.close()
            monkeypatch.setattr("sys.stdin", reader)
            try:
                await asyncio.wait_for(
                    run_chat_client(
                        transport=transport, renderer=renderer,
                        read_model=read_model, agent_name="own-submissions-wiring-3611",
                        is_tty=False,
                    ),
                    timeout=5.0,
                )
            finally:
                reader.close()
    finally:
        transport.close()

    return [m.text for m in renderer.messages if m.kind == "user"]


@pytest.mark.asyncio
async def test_tty_run_chat_client_wires_own_submissions_and_suppresses(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: #3611 item 3 wiring pin — is_tty=True (genuine, via a real pty
    fd) reaches run_chat_client's own_submissions=set() construction, so the
    broadcast user_submitted chat-event for THIS client's own turn is
    suppressed (the terminal's own PromptSession render already showed it,
    #3287's own reasoning) rather than double-rendered."""
    echoed = await _drive_one_submission(
        monkeypatch, tmp_path, is_tty=True, text="hello there",
    )
    assert echoed == [], (
        f"a TTY session re-rendered its own broadcast user_submitted event — "
        f"is_tty is no longer reaching own_submissions correctly: {echoed!r}"
    )


@pytest.mark.asyncio
async def test_non_tty_run_chat_client_wires_own_submissions_none_and_renders(
    monkeypatch, tmp_path,
) -> None:
    """Tier 2: #3611 item 3 wiring pin, the other leg — is_tty=False (a real
    OS pipe) reaches own_submissions=None, so the broadcast user_submitted
    event is the ONLY record of what was submitted and must render."""
    echoed = await _drive_one_submission(
        monkeypatch, tmp_path, is_tty=False, text="hello there",
    )
    assert echoed == ["hello there"], (
        f"a piped session did not render its own submission — its only "
        f"record of what was asked is gone: {echoed!r}"
    )
