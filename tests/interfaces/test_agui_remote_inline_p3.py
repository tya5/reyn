"""Tier 2 (ADR-0039 P3): the inline CUI renders on the REMOTE path — local ≡
remote at the renderer/loop layer, not just the transport.

Before P3, ``reyn chat --connect`` on a TTY rendered the plain console CUI while
local ``reyn chat`` rendered the Rich inline CUI: the transport was unified (P1/P2)
but the renderer SELECTION and the inline input driver were local-only. P3 closes
that with a client-side read-model seam (:mod:`reyn.interfaces.repl.read_model`)
and a shared driver (:mod:`reyn.interfaces.repl.client_driver`).

These pin the invariants that keep the two paths in lockstep:

- **Renderer selection is one shared seam** (``make_renderer`` + the
  ``_inline_interactive`` predicate) — interactive → inline, non-TTY / --cui →
  console, for BOTH paths.
- **The status bar is frame-available on remote**: the MAIN-bar chip values ride
  ``STATE_*`` and the ``RemoteReadModel`` projects them into the snapshot the chips
  read.
- **The read-model degrades gracefully**: session-local affordances (intervention
  region and command-UI) are empty/0 on remote, never faked.
- **The shared driver drives EITHER transport to termination.**

Real emitter / codec / AgUiTransport / renderers throughout — no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.cli.commands.chat import _inline_interactive, _run_remote
from reyn.interfaces.cli.logger_factory import make_renderer
from reyn.interfaces.repl.client_driver import run_chat_client
from reyn.interfaces.repl.read_model import (
    RemoteReadModel,
    project_remote_snapshot,
)
from reyn.interfaces.repl.renderer import ConsoleChatRenderer, InlineChatRenderer
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


async def _sse_lines(text):
    for line in text.split("\n"):
        yield line


# --- renderer selection: the SAME seam gates local and remote ------------------


def test_make_renderer_selects_inline_when_interactive() -> None:
    """Tier 2: the shared renderer seam returns the inline CUI when interactive
    and the plain console otherwise — the choice both `run` and `_run_remote` make."""
    inline = make_renderer(is_interactive=True)
    console = make_renderer(is_interactive=False)
    assert isinstance(inline, InlineChatRenderer)
    assert inline.uses_app_input() is True
    assert isinstance(console, ConsoleChatRenderer)
    assert console.uses_app_input() is False


def test_inline_interactive_predicate_gates_both_paths() -> None:
    """Tier 2: `_inline_interactive` (the predicate feeding `make_renderer` on BOTH
    paths) is True only on a full TTY with no --cui — so an interactive remote
    attach selects inline, and a piped / --cui remote attach selects console."""
    assert _inline_interactive(cui=False, stdin_isatty=True, stdout_isatty=True) is True
    # Piped stdout (reyn chat --connect | tee) → console fallback, both paths.
    assert _inline_interactive(cui=False, stdin_isatty=True, stdout_isatty=False) is False
    # --cui forces console even on a full TTY.
    assert _inline_interactive(cui=True, stdin_isatty=True, stdout_isatty=True) is False


# --- status bar frame-availability: MAIN-bar chip values ride STATE_* ----------


@pytest.mark.asyncio
async def test_remote_read_model_projects_wire_status() -> None:
    """Tier 2: driving a real emitter → wire → AgUiTransport populates the
    RemoteStatusView, and the RemoteReadModel projects the MAIN-bar chip values
    into the snapshot shape the inline chips read."""
    state = {
        "attached_name": "researcher", "model": "opus",
        "cost_agent": 1.5, "cost_total": 3.0, "agent_tokens": 42,
        "ctx_used": 200, "ctx_window": 1000,
    }

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="agent", text="hi"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), lambda: dict(state))
    sse = "".join([chunk async for chunk in emitter.stream()])
    assert "STATE_SNAPSHOT" in sse

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    async for _f in transport.frames():
        pass  # drain → applies STATE_* to transport.status

    snap = RemoteReadModel(transport).snapshot()
    assert snap["attached_name"] == "researcher"
    assert snap["model"] == "opus"
    assert snap["cost_agent"] == 1.5
    assert snap["ctx_window"] == 1000


# --- graceful degrade: session-local affordances are empty, never faked --------


@pytest.mark.asyncio
async def test_remote_read_model_degrades_local_only_affordances() -> None:
    """Tier 2: the remote read-model returns empty (not fabricated) for everything
    NOT on the wire — intervention region, command-UI — and declares it
    has no command-UI region so the /rewind text fallback engages."""

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(""), _noop_send)
    rm = RemoteReadModel(transport)
    assert rm.intervention_head() is None
    assert rm.pending_command_ui() is None
    assert rm.has_command_ui_region is False
    # A pre-STATE_SNAPSHOT snapshot renders a placeholder model.
    snap = rm.snapshot()
    assert snap["model"] == "—"


def test_project_remote_snapshot_expansion_keys_are_empty() -> None:
    """Tier 2: dropdown-expansion keys absent from the wire project to safe empties
    (opening a dropdown on a remote client must not raise or invent values)."""
    snap = project_remote_snapshot({"model": "m", "ctx_used": 1, "ctx_window": 2})
    assert snap["model_classes"] == []
    assert snap["session_tree"] == []
    assert snap["ctx_compaction_status_fn"] is None
    assert snap["pipelines"] == []


# --- call-site wiring: _run_remote itself selects the inline renderer ----------


def _connect_args(**over):
    import argparse
    ns = argparse.Namespace(
        connect="http://127.0.0.1:9/never", agent_name=None, token=None, cui=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def _capture_remote_renderer(args, *, stdin_isatty, stdout_isatty, tmp_path, monkeypatch):
    """Drive the REAL `_run_remote` with a recording runner (not a mock — the same
    injectable-double pattern `_run_once` uses) and return the renderer it wired.
    Isolated: cwd → tmp, root-logging handlers saved/restored (the interactive
    branch installs a file log handler)."""
    import logging

    captured: dict = {}

    async def _recording_run(*, base_url, agent_name, token, renderer):
        captured["renderer"] = renderer
        captured["base_url"] = base_url
        captured["agent_name"] = agent_name

    monkeypatch.chdir(tmp_path)
    saved_handlers = logging.root.handlers[:]
    saved_level = logging.root.level
    try:
        _run_remote(
            args, run_remote=_recording_run,
            stdin_isatty=stdin_isatty, stdout_isatty=stdout_isatty,
        )
    finally:
        logging.root.handlers[:] = saved_handlers
        logging.root.setLevel(saved_level)
    return captured


def test_run_remote_selects_inline_renderer_on_interactive_tty(tmp_path, monkeypatch) -> None:
    """Tier 2c: (regression guard for the ORIGINAL bug) the real `_run_remote`
    call-site — not just the `make_renderer` helper in isolation — hands the inline
    renderer to the remote runner on an interactive TTY, and the plain console
    renderer when piped. Strip-falsify: revert `_run_remote` to a hard-coded
    `renderer = make_chat_renderer()` and the interactive assertion goes RED."""
    interactive = _capture_remote_renderer(
        _connect_args(), stdin_isatty=True, stdout_isatty=True,
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert isinstance(interactive["renderer"], InlineChatRenderer)
    # Sanity: the rest of the composition (base_url / default agent) is wired too.
    assert interactive["base_url"] == "http://127.0.0.1:9/never"
    assert interactive["agent_name"] == "default"

    # Piped stdout (reyn chat --connect | tee) → console fallback, same as local.
    piped = _capture_remote_renderer(
        _connect_args(), stdin_isatty=True, stdout_isatty=False,
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert isinstance(piped["renderer"], ConsoleChatRenderer)

    # --cui forces console even on a full TTY.
    forced = _capture_remote_renderer(
        _connect_args(cui=True), stdin_isatty=True, stdout_isatty=True,
        tmp_path=tmp_path, monkeypatch=monkeypatch,
    )
    assert isinstance(forced["renderer"], ConsoleChatRenderer)


# --- shared driver: one body drives the remote transport to termination --------


@pytest.mark.asyncio
async def test_run_chat_client_drives_remote_transport_to_end() -> None:
    """Tier 2c: the shared driver (the SAME one local uses) banners + wires the
    loops against an AgUiTransport + RemoteReadModel and returns when the stream
    hits __end__ — proving the remote path runs through the unified driver, not a
    bespoke copy. Console renderer + is_tty=False keeps the input loop headless."""

    async def frames():
        yield DisplayFrame(OutboxMessage(kind="agent", text="remote reply"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))

    emitter = AgUiEmitter(frames(), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(sse), _noop_send)
    read_model = RemoteReadModel(transport)
    renderer = ConsoleChatRenderer()
    # Returns once the output loop consumes __end__ (FIRST_COMPLETED), cancelling
    # the headless input loop. A finite timeout is the no-hang assertion.
    await asyncio.wait_for(
        run_chat_client(
            transport=transport,
            renderer=renderer,
            read_model=read_model,
            agent_name="default",
            is_tty=False,
        ),
        timeout=3.0,
    )
