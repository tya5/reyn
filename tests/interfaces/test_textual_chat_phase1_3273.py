"""Phase 1 TUI-rebuild gates (#3273): the Textual conversation pane.

These pin the four architect-specified Phase-1 invariants:

- **resize reflow** (Tier 2b): the whole conversation re-wraps at a new terminal
  width — the core capability the plain scrollback cannot do.
- **import isolation** (Tier 2c): the plain / non-TTY path stays green when
  ``textual_flowview`` is unimportable — the flowview import is lazy, TTY-only.
- **plain-fallback equivalence** (Tier 2c): the app models the SAME logical turn
  sequence the plain renderer renders from an identical frame stream.
- **input wiring** (Tier 2b): a Composer submit routes back through the transport.

All use real instances (a concrete :class:`ScriptedTransport`, a real
recording renderer) — no mocks — per the testing policy.
"""
from __future__ import annotations

import asyncio
import tomllib
from typing import AsyncIterator

import pytest

from reyn.interfaces.repl.renderer import ChatRenderer
from reyn.interfaces.repl.stream_client import run_output_loop
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage
from tests._support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT


class ScriptedTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` that replays a fixed frame list.

    Constructed cheaply from a list of :class:`OutboxMessage` — the same frames a
    session's outbox would push — so both the plain output loop and the Textual
    app can be driven from an identical script. ``end=True`` terminates the stream
    with ``__end__``; ``end=False`` keeps it open (blocks after the script) so the
    app under test stays mounted for a resize.
    """

    def __init__(self, messages: "list[OutboxMessage]", *, end: bool = True) -> None:
        self._messages = list(messages)
        self._end = end
        self.submitted: list[str] = []

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame]":
        for msg in self._messages:
            yield DisplayFrame(msg)
        if self._end:
            yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
        else:
            await asyncio.Event().wait()

    async def submit_user_text(self, text: str) -> None:
        self.submitted.append(text)

    async def answer_intervention_text(self, text: str) -> bool:
        return False

    async def answer_intervention_choice(self, choice_id: str) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        self._messages.append(msg)

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


class _RecordingRenderer(ChatRenderer):
    """A real plain renderer that records the kind of every message it renders —
    ``uses_app_input()`` stays False (base default) so it takes the plain path."""

    def __init__(self) -> None:
        self.kinds: list[str] = []

    def message(self, msg: OutboxMessage) -> None:
        self.kinds.append(msg.kind)


# A conversation script covering the logical turn kinds the gate names:
# user / assistant / tool / result / error.
_CONVERSATION = [
    OutboxMessage(kind="user", text="find the sandbox network gate"),
    OutboxMessage(kind="agent", text="Looking now."),
    OutboxMessage(
        kind="tool_call_started", text="grep", meta={"tool": "grep", "args": {"q": "gate"}}
    ),
    OutboxMessage(
        kind="tool_call_completed", text="", meta={"tool": "grep", "result": {"op": "grep", "count": 3}}
    ),
    OutboxMessage(kind="agent", text="It lives in sandbox/network.py."),
    OutboxMessage(kind="error", text="transient hiccup"),
]


@pytest.mark.asyncio
async def test_conversation_reflows_on_resize() -> None:
    """Tier 2b: the whole conversation re-wraps at a new width — a long entry
    occupies MORE rows narrow than wide. This is the headline capability (the
    plain scrollback is frozen at its emit-time width); proven end-to-end through
    the mounted app + ``pilot.resize_terminal``, asserting the FlowView's content
    height (not any exact formatting) grows when narrower."""
    from textual_flowview import FlowView

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    long_line = ("reflow " * 90).strip()
    transport = ScriptedTransport([OutboxMessage(kind="agent", text=long_line)], end=False)
    app = TextualChatApp(transport=transport)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        flow = app.query_one(FlowView)
        wide_height = flow.virtual_size.height
        await pilot.resize_terminal(40, 30)
        await pilot.pause()
        await pilot.pause()
        narrow_height = flow.virtual_size.height

    assert wide_height >= 1
    assert narrow_height > wide_height, (
        f"conversation did not reflow: wide={wide_height} narrow={narrow_height}"
    )


# Strip-falsify witness, run in a CLEAN subprocess interpreter: block
# ``textual`` / ``textual_flowview`` at ``sys.meta_path`` BEFORE any reyn import,
# then (1) import the always-loaded plain-path modules and assert neither flowview
# nor textual entered ``sys.modules`` at module load, and (2) drive a real
# non-TTY ``run_chat_client`` to ``__end__``, asserting the whole conversation
# rendered. A subprocess is required: in-process, the plain-path modules are
# already imported (cached), so a top-level ``import textual_flowview`` regression
# would be masked — the falsify-check confirmed an in-process variant stays green
# under that regression, so it would be a false witness.
_ISOLATION_SUBPROCESS = '''
import asyncio, sys
from pathlib import Path


class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("textual", "textual_flowview"):
            raise ModuleNotFoundError("blocked for isolation test: " + name)
        return None


sys.meta_path.insert(0, _Block())

# (1) module-load isolation: importing the plain path must not pull flowview/textual.
import reyn.interfaces.repl.client_driver  # noqa: E402
import reyn.interfaces.repl.stream_client  # noqa: E402
import reyn.interfaces.cli.commands.chat  # noqa: E402

assert "textual_flowview" not in sys.modules, "flowview imported at module load"
assert "textual" not in sys.modules, "textual imported at module load"

# (2) functional: a real non-TTY plain run completes without touching flowview.
from reyn.interfaces.repl.client_driver import run_chat_client  # noqa: E402
from reyn.interfaces.repl.read_model import ChatReadModel, LOCAL_CHAT_READ_CAPABILITIES  # noqa: E402
from reyn.interfaces.repl.renderer import ChatRenderer  # noqa: E402
from reyn.interfaces.transport.client_transport import ClientTransportStub  # noqa: E402
from reyn.interfaces.transport.frames import DisplayFrame  # noqa: E402
from reyn.runtime.outbox import OutboxMessage  # noqa: E402


class T(ClientTransportStub):
    def __init__(self):
        self.rendered = 0
    def start(self): pass
    def close(self): pass
    async def frames(self):
        yield DisplayFrame(OutboxMessage(kind="user", text="hi"))
        yield DisplayFrame(OutboxMessage(kind="agent", text="hello"))
        yield DisplayFrame(OutboxMessage(kind="__end__", text=""))
    async def submit_user_text(self, text): pass
    async def answer_intervention_text(self, text): return False
    async def answer_intervention_choice(self, cid): return False
    def has_session(self): return True
    def pending_intervention_head(self): return None
    def put_display(self, msg): pass
    async def cancel_inflight(self): pass
    async def shutdown(self): pass


class RM(ChatReadModel):

    @property
    def capabilities(self):
        # #4996: a test double simulating a fully-capable (local-shaped)
        # read model — every accessor above is a REAL, non-degraded
        # implementation for this test's own purposes, not a stand-in for
        # RemoteReadModel's frame-sufficiency boundary.
        return LOCAL_CHAT_READ_CAPABILITIES
    def snapshot(self, config=None): return None
    def intervention_head(self): return None
    def pending_command_ui(self): return None
    def clear_pending_command_ui(self): pass
    @property
    def has_command_ui_region(self): return True
    @property
    def history_path(self): return Path("/tmp/reyn_isolation_history")
    def conversation_history(self, *, limit=None): return []
    def load_older_conversation_history(self, *, agent=None, session_id=None): return 0


class R(ChatRenderer):
    n = 0
    def message(self, msg): R.n += 1


# #4445: no timeout= — same #4397 family (a test-owned wait-budget
# constant), same rule even though this runs inside the subprocess this
# module's own outer `subprocess.run` spawns (that call already carries
# no timeout= of its own, per the same #4397 fix) — CI's own per-test
# pytest-timeout is the kill switch either way.
asyncio.run(
    run_chat_client(transport=T(), renderer=R(), read_model=RM(),
                    agent_name="default", is_tty=False),
)
assert R.n >= 2, "plain path did not render the conversation"
assert "textual_flowview" not in sys.modules, "flowview imported during plain run"
print("ISOLATION_OK")
'''


def test_plain_path_survives_flowview_absence(out_of_process_reyn) -> None:
    """Tier 2c: with ``textual`` / ``textual_flowview`` unimportable from a clean
    interpreter, the plain / non-TTY path imports and runs green — the flowview
    import is lazy and TTY-only. Runs the strip in a subprocess (see the module
    comment for why in-process would be a false witness) and asserts it reaches
    the ``ISOLATION_OK`` sentinel.

    ``out_of_process_reyn`` (#5028): a subprocess gets no benefit from pytest's
    own ``pythonpath = ["src"]`` — it re-resolves ``reyn`` from whatever the
    venv's own editable install (or, in a git worktree, the ambient shell's)
    points at, which can be a DIFFERENT checkout's ``src`` entirely. Pinning
    the in-process-derived root as ``PYTHONPATH`` makes the subprocess read the
    SAME ``reyn`` this test imported, rather than trusting the ambient
    environment to agree. This does not widen what the subprocess can import —
    its own ``sys.meta_path`` blocker (above) still blocks ``textual``/
    ``textual_flowview`` regardless of ``PYTHONPATH``, since that block is a
    finder installed by the script itself, not a path-visibility question."""
    import os
    import subprocess
    import sys

    env = {**os.environ, "PYTHONPATH": out_of_process_reyn}
    # #4397: no timeout= — CI's own per-test pytest-timeout is the kill switch.
    proc = subprocess.run(
        [sys.executable, "-c", _ISOLATION_SUBPROCESS],
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert "ISOLATION_OK" in proc.stdout, f"stdout={proc.stdout}\nstderr={proc.stderr}"


@pytest.mark.asyncio
async def test_plain_fallback_equivalence_same_turn_sequence() -> None:
    """Tier 2c: the Textual app models the SAME logical turn sequence the plain
    renderer renders from an identical frame stream — same ``transport.frames()``
    consumed, only the drawing differs. Feeds one script to the plain output loop
    (recording renderer) and the same script to the mounted app, then asserts the
    modeled entry kinds equal the plain-rendered kinds."""
    from textual_flowview import FlowView

    from reyn.interfaces.inline.textual_chat import TextualChatApp

    # Plain side: drive the real output loop with a recording renderer.
    renderer = _RecordingRenderer()
    plain_transport = ScriptedTransport(_CONVERSATION, end=True)
    # #4445: no timeout= — same #4397 family; CI's own per-test
    # pytest-timeout is the kill switch. `plain_transport` is a finite
    # scripted sequence (`end=True`), so `run_output_loop` returns on its
    # own once drained — the timeout was never load-bearing for a real
    # condition here.
    await run_output_loop(plain_transport, renderer, None, command_ui_region=True)

    # App side: feed the identical script through the app's frame pump.
    app_transport = ScriptedTransport(_CONVERSATION, end=False)
    app = TextualChatApp(transport=app_transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await pilot.pause()
        modeled = [e.item.kind for e in app.query_one(FlowView).entries]

    assert modeled == renderer.kinds
    assert modeled == [m.kind for m in _CONVERSATION]


@pytest.mark.asyncio
async def test_composer_submit_routes_to_transport() -> None:
    """Tier 2b: a Composer submit is delivered to the session via the transport's
    send seam (input→transport wiring). Types into the real Composer and presses
    Enter through the pilot; asserts the text reached ``submit_user_text``."""
    from reyn.interfaces.inline.textual_chat import Composer, TextualChatApp

    transport = ScriptedTransport([], end=False)
    app = TextualChatApp(transport=transport)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.query_one(Composer).focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("enter")
        await pilot.pause()

    assert transport.submitted == ["hi"]


def test_textual_is_a_direct_dependency() -> None:
    """Tier 1: ``textual`` is an explicit DIRECT dependency (floor-pinned), not
    only transitive via flowview — the app code imports ``textual.app`` directly,
    so the dependency contract must name it. Reads the real ``pyproject.toml``."""
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    deps = data["project"]["dependencies"]
    textual_reqs = [d for d in deps if d.split()[0].split(">")[0].split("=")[0] == "textual"]
    assert textual_reqs, f"textual not a direct dependency; deps={deps}"
    assert any(">=" in d for d in textual_reqs), (
        f"textual direct dep must be floor-pinned; got {textual_reqs}"
    )
