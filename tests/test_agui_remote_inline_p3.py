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
- **The status bar is frame-available on remote**: the ``task_count`` MAIN-bar
  field rides ``STATE_*`` and the ``RemoteReadModel`` projects it (+ the other
  wire keys) into the snapshot the chips read.
- **The read-model degrades gracefully**: session-local affordances (intervention
  region, command-UI, task tree) are empty on remote, never faked.
- **The shared driver drives EITHER transport to termination.**

Real emitter / codec / AgUiTransport / renderers throughout — no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.cli.commands.chat import _inline_interactive
from reyn.interfaces.cli.logger_factory import make_renderer
from reyn.interfaces.repl.client_driver import run_chat_client
from reyn.interfaces.repl.read_model import (
    RemoteReadModel,
    project_remote_snapshot,
)
from reyn.interfaces.repl.renderer import ConsoleChatRenderer, InlineChatRenderer
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.state import _WIRE_KEYS, project_status
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


# --- status bar frame-availability: task_count rides STATE_* -------------------


def test_task_count_is_on_the_wire_projection() -> None:
    """Tier 2: `task_count` is part of the STATE_* status read-model vocabulary
    (MAIN-bar parity for the remote `task` chip)."""
    assert "task_count" in _WIRE_KEYS
    projected = project_status({"task_count": 4}, waiting_on=None)
    assert projected["task_count"] == 4


@pytest.mark.asyncio
async def test_remote_read_model_projects_wire_status_incl_task_count() -> None:
    """Tier 2: driving a real emitter → wire → AgUiTransport populates the
    RemoteStatusView, and the RemoteReadModel projects the MAIN-bar fields —
    including task_count — into the snapshot shape the inline chips read."""
    state = {
        "attached_name": "researcher", "model": "opus",
        "cost_agent": 1.5, "cost_total": 3.0, "agent_tokens": 42,
        "ctx_used": 200, "ctx_window": 1000, "task_count": 3,
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
    # The MAIN-bar parity field the remote `task` chip reads.
    assert snap["task_count"] == 3


# --- graceful degrade: session-local affordances are empty, never faked --------


@pytest.mark.asyncio
async def test_remote_read_model_degrades_local_only_affordances() -> None:
    """Tier 2: the remote read-model returns empty (not fabricated) for everything
    NOT on the wire — intervention region, command-UI, task tree — and declares it
    has no command-UI region so the /rewind text fallback engages."""

    async def _noop_send(_payload):
        return None

    transport = AgUiTransport(_sse_lines(""), _noop_send)
    rm = RemoteReadModel(transport)
    assert rm.intervention_head() is None
    assert rm.pending_command_ui() is None
    assert rm.has_command_ui_region is False
    assert await rm.list_active_tasks() == []
    # A pre-STATE_SNAPSHOT snapshot renders a placeholder model, empty tree/counts.
    snap = rm.snapshot()
    assert snap["model"] == "—"
    assert snap["task_tree"] == []
    assert snap["task_count"] == 0


def test_project_remote_snapshot_expansion_keys_are_empty() -> None:
    """Tier 2: dropdown-expansion keys absent from the wire project to safe empties
    (opening a dropdown on a remote client must not raise or invent values)."""
    snap = project_remote_snapshot({"model": "m", "ctx_used": 1, "ctx_window": 2})
    assert snap["model_classes"] == []
    assert snap["session_tree"] == []
    assert snap["ctx_compaction_status_fn"] is None
    assert snap["pipelines"] == []


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
