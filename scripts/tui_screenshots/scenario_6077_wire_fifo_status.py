"""#6077 proposal 5 scenario: the status line's wire-FIFO segment
(``wire_fifo_waiting`` + ``wire_fifo_inflight_elapsed``, #6249).

owner has not seen the real TUI render of this segment (company-PC/
environment constraint — CLAUDE.md's TUI colour-policy sibling rule: a
visible change is captured and shown, never handed to the owner to run).
This drives a REAL ``TextualChatApp`` to the exact state
``tests/interfaces/test_6077_wire_fifo_status_visibility.py``'s own
``test_wire_fifo_segment_shows_waiting_count_and_injected_clock_elapsed``
builds end to end — the SAME real ``_enqueue_wire`` unit shape, the SAME
injected ``clock`` seam ``TextualChatApp.__init__`` already documents (no
``sleep``, no new clock), the SAME real heartbeat ``on_timer`` refresh —
reusing that test's own idioms verbatim rather than inventing a new one.

Captured state: one in-flight unit (started at the injected clock's
``0.0``, advanced to ``4.2`` s before capture) with THREE more units
queued behind it, so the status line reads ``wire: 3 waiting, sending
4.2s`` — several queued sends, not zero, per the request that a screenshot
made for the owner show a nonzero backlog rather than the boundary case.

CI: manual -- imported by tui_screenshot.py, never run directly.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# #3024: this module imports `reyn` too (independent of whether the caller
# already guarded) -- see verify_env_identity.py's own guard_bare_script_or_
# exit docstring. `tui_screenshot.py` (the only sanctioned caller) already
# runs this before importing any scenario module, but this call stays here
# too: a scenario module is itself a `scripts/*.py`-shaped file per that
# guard's own contract, and this repeats at negligible cost (a single
# find_spec probe) rather than relying on caller discipline.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(_REPO_ROOT / "src"), str(_REPO_ROOT), str(_REPO_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from verify_env_identity import guard_bare_script_or_exit  # noqa: E402

guard_bare_script_or_exit()

from reyn.interfaces.inline.textual_chat import TextualChatApp  # noqa: E402
from reyn.interfaces.repl.read_model import (  # noqa: E402
    LOCAL_CHAT_READ_CAPABILITIES,
    ChatReadModel,
)
from reyn.runtime.outbox import OutboxMessage  # noqa: E402
from tests._support.textual_chat_test_helpers import QueueTransport  # noqa: E402

SIZE = (100, 30)

# A real-shaped status snapshot (same key set `interfaces/inline/app.py`'s own
# `_snapshot` produces — mirrors `scenario_4535_image_present.py`'s own
# `_SNAP` fixture) so the status bar this scenario screenshots shows genuine
# Model / Agent / cost / ctx figures alongside the new wire segment, not the
# read-model-absent zeros.
_SNAP = {
    "model": "claude-opus-4-8",
    "model_active_class": "opus",
    "model_classes": ["light", "opus", "strong"],
    "agent_names": ["default", "planner"],
    "attached_name": "default",
    "session_tree": [],
    "usage": (1200, 340, 1540),
    "cost_usd": 0.0123,
    "cost_agent": 0.0123,
    "cost_total": 0.0500,
    "agent_tokens": 1540,
    "ctx_used": 90000,
    "ctx_window": 200000,
    "ctx_source": "model",
    "ctx_recent_usage": (90000, 40000),
    "cache_usage_reported": True,
    "usage_breakdown_reported": True,
}


class _SnapshotReadModel(ChatReadModel):
    """A real :class:`ChatReadModel` seam impl returning a fixed real-shaped
    snapshot (mirrors `scenario_4535_image_present.py`'s own
    `_SnapshotReadModel`) — the status bar reads model/agent/cost/ctx off
    this same seam a live session's read model uses."""

    @property
    def capabilities(self):
        return LOCAL_CHAT_READ_CAPABILITIES

    def snapshot(self, config=None):
        return _SNAP

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
        return Path("/tmp/reyn_6077_screenshot_history")

    def conversation_history(self, *, limit=None):
        return []

    def load_older_conversation_history(self, *, agent=None, session_id=None):
        return 0


class _FakeClock:
    """The SAME injectable-clock stand-in
    `test_6077_wire_fifo_status_visibility.py`'s own `_FakeClock` uses —
    starts at ``0.0``, advances only when told to. Wired through
    `TextualChatApp.__init__`'s own `clock` parameter (the same seam the
    tool-elapsed timer already uses) — never `time.monotonic()`, never a
    `sleep`."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


#: Module-level so `direct()` (which only receives `app`/`pilot`) can drive it.
_CLOCK = _FakeClock()


def make_app() -> TextualChatApp:
    return TextualChatApp(
        transport=QueueTransport(), read_model=_SnapshotReadModel(), clock=_CLOCK,
    )


async def direct(app: TextualChatApp, pilot) -> None:
    transport: QueueTransport = app._transport  # type: ignore[assignment]

    # One prior exchange, so the screenshot shows the chat surface actually
    # in use (an empty first-turn screen would not exercise the composer/
    # status-bar layout the way a live session's does).
    await transport.push(OutboxMessage(kind="user", text="how's the deploy going?"))
    await pilot.pause()
    await transport.push(
        OutboxMessage(kind="agent", text="checking now, one moment...", meta={"chain_id": "c1"})
    )
    await pilot.pause()

    # The exact real unit shape `test_6077_wire_fifo_status_visibility.py`'s
    # own end-to-end present-witness test drives: one slow, real, still-
    # running unit (never a `sleep` — gated on a real `asyncio.Event`) plus
    # three further real no-op units queued BEHIND it, so
    # `self._wire_fifo.qsize()` reads 3 — a nonzero backlog, not the
    # boundary (0-waiting) case.
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_unit() -> None:
        started.set()
        await release.wait()

    async def _noop_unit() -> None:
        return None

    app._enqueue_wire(_slow_unit())  # noqa: SLF001
    await started.wait()
    await pilot.pause()

    app._enqueue_wire(_noop_unit())  # noqa: SLF001
    app._enqueue_wire(_noop_unit())  # noqa: SLF001
    app._enqueue_wire(_noop_unit())  # noqa: SLF001
    await pilot.pause()

    # Advance the injected clock (never `time.monotonic()`, never a
    # `sleep`) and drive the SAME public `on_timer` + the App's own real
    # heartbeat timer identity the test file uses to trigger the refresh
    # #6249 wires onto the heartbeat specifically for the "nothing else
    # will re-render while stuck" case.
    from textual import events

    _CLOCK.advance(4.2)
    assert app.pump_heartbeat_timer is not None
    await app.on_timer(
        events.Timer(timer=app.pump_heartbeat_timer, time=0.0, count=1),
    )
    await pilot.pause()

    # Never release `release` — the in-flight unit (and the screenshot's
    # captured state) must stay exactly as built above.
