"""Tier 2: the AG-UI emitter's control-sentinel disposition (ADR-0039 P6a).

A few ``__…__`` display sentinels get per-entry dispositions on the AG-UI wire:

- **Client-consumed → FORWARDED** (profiled ``CUSTOM``): ``__copy_last_reply__`` /
  ``__rewind_list__`` are consumed by the CLIENT over the transport stream (a real
  client-side clipboard copy / rewind picker). In the thin-client model transport
  IS the AG-UI wire, so they MUST reach it — filtering them would make remote
  ``/copy`` / ``/rewind`` silent no-ops.
- **Filtered** (``CONTROL_FILTER_KINDS``, an explicit per-entry allowlist — never
  the negation of a forward-set): ``__end__`` (the stream terminator) and
  ``__session_switch_request__`` produce ZERO wire events. For the switch sentinel
  the filter is a genuine FAIL-SAFE rather than the active mechanism — the AG-UI
  tap consumes that sentinel itself (#3310 N3 switch-follow, or a silent drop for
  an unresolvable sid), so it never reaches the emitter from that source.
``__attach_request__`` retired (#4534 PR-2): ``/attach`` now goes through
``ClientTransport.request_attach``, a typed operation with no display-channel
sentinel — the forwarder's own ``__attach_request__`` branch is gone too, so
there is no longer a live example of "forwarder swallows it for
``repl_outbox`` yet it still reaches the wire" (that was the reachability
point #3362 measured). The still-live mechanism that survives is
``__session_switch_request__``'s absence from the wire: it is the TAP
(``_SessionFrameSource._drain_one_session``) consuming it, not merely
``CONTROL_FILTER_KINDS`` filtering it — the forwarder's ``continue`` is
SUBSCRIBER-LOCAL (it and the AG-UI tap are independent ``outbox_hub``
subscribers, and the hub fans every message out to every subscription), so it
only means "not re-posted to ``repl_outbox``" and says nothing about the wire
on its own.

Real instances only — a real ``AgUiEmitter`` over real SSE text; no mocks.
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.interfaces.transport.agui.emitter import AgUiEmitter
from reyn.interfaces.transport.agui.endpoint import _SessionFrameSource
from reyn.interfaces.transport.agui.protocol import (
    CONTROL_FILTER_KINDS,
    parse_sse_blocks,
)
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from tests._support.agent_session import make_session


async def _frame_source(frames):
    for f in frames:
        yield f


def _registry(tmp_path):
    """A real :class:`AgentRegistry` whose forwarder actually runs."""
    shared = BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return make_session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    reg.create("alpha")
    reg.create("beta")
    return reg


async def _collect_all(agen) -> "list":
    """Collect every item from an async generator until it terminates.

    #4275: the caller always pushes an ``__end__`` sentinel, which
    ``AgUiEmitter.stream()`` returns on — the stream is naturally finite, so
    no wall-clock window is needed. If a caller regresses and never sends
    ``__end__``, this hangs, surfaced by CI's kill switch rather than a
    bounded window that would silently truncate the collected list instead
    of failing.
    """
    return [item async for item in agen]


async def _wire_events(frames):
    emitter = AgUiEmitter(_frame_source(frames), lambda: None)
    sse = "".join([chunk async for chunk in emitter.stream()])
    return parse_sse_blocks(sse.split("\n"))


def _reyn_display_names(events) -> set[str]:
    names: set[str] = set()
    for ev in events:
        data = ev.data or {}
        reyn = data.get("_reyn") if isinstance(data, dict) else None
        if isinstance(reyn, dict) and reyn.get("frame") == "display":
            names.add(f"reyn.display.{reyn.get('kind')}")
    return names


@pytest.mark.asyncio
async def test_client_consumed_sentinels_are_forwarded_on_the_wire() -> None:
    """Tier 2: wire-existence probe — ``__copy_last_reply__`` / ``__rewind_list__``
    ARE forwarded (a NON-zero AG-UI event each), because the client consumes them
    over the transport stream. Filtering them would break remote /copy and /rewind."""
    frames = [
        DisplayFrame(OutboxMessage(kind="__copy_last_reply__", text="c")),
        DisplayFrame(OutboxMessage(kind="__rewind_list__", text="r")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    names = _reyn_display_names(events)

    assert "reyn.display.__copy_last_reply__" in names
    assert "reyn.display.__rewind_list__" in names
    # Neither is in the filter set (the disposition backing the forward).
    assert "__copy_last_reply__" not in CONTROL_FILTER_KINDS
    assert "__rewind_list__" not in CONTROL_FILTER_KINDS


@pytest.mark.asyncio
async def test_filtered_control_sentinels_are_not_on_the_wire() -> None:
    """Tier 2: the filtered sentinels (``__session_switch_request__`` / ``__end__``)
    produce ZERO wire events; a surrounding ``agent`` frame is forwarded normally
    (the filter is per-kind, not a stream-wide drop)."""
    frames = [
        DisplayFrame(OutboxMessage(kind="__session_switch_request__", text="s")),
        DisplayFrame(OutboxMessage(kind="agent", text="hello")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
    ]
    events = await _wire_events(frames)
    names = _reyn_display_names(events)

    assert "reyn.display.__session_switch_request__" not in names
    assert "reyn.display.__end__" not in names
    assert "reyn.display.agent" in names


@pytest.mark.asyncio
async def test_tap_not_filter_keeps_switch_request_off_the_wire(
    tmp_path,
) -> None:
    """Tier 2: REACHABILITY, through the real tap — ``__session_switch_request__``
    is kept off the AG-UI wire by the TAP consuming it
    (``_SessionFrameSource._drain_one_session``), not merely by
    ``CONTROL_FILTER_KINDS`` filtering it (#3362's distinction; #4534 PR-2
    dropped this file's former ``__attach_request__`` arm — that sentinel's
    forwarder branch is retired, so there is no live case left of "forwarder
    swallows it for ``repl_outbox`` yet it still reaches the wire").

    Every other test in this file feeds a synthetic frame list straight to the
    emitter, which can only ever show what the emitter does with a frame it is
    HANDED — it cannot show whether a frame arrives at all. This test closes
    that gap by running the REAL ``AgentRegistry`` forwarder concurrently with
    the REAL ``_SessionFrameSource`` + emitter, and reading the wire.

    Two arms:

    - ``__session_switch_request__`` — **not on the wire**. Measured by strip:
      removing the filter entry alone leaves this arm green, and only removing
      the filter entry AND the tap's consumption turns it red — the filter is
      a backstop for a source that does not consume it. This arm deliberately
      does not attribute itself to the filter; the synthetic
      ``test_filtered_control_sentinels_are_not_on_the_wire`` above is the
      filter's own gate (and does go red when the entry is removed).
    - an ordinary ``agent`` frame — on the wire, so a silent tap cannot make the
      negative arm pass for the wrong reason.
    """
    reg = _registry(tmp_path)
    session = await reg.attach("alpha")
    assert reg.attached_name == "alpha"

    source = _SessionFrameSource(session, registry=reg, agent_name="alpha")
    source.start()
    emitter = AgUiEmitter(source.frames(), lambda: None)

    async def _drive() -> None:
        await asyncio.sleep(0.2)
        await session._put_outbox(
            OutboxMessage(kind="__session_switch_request__", text="no-such-sid")
        )
        await asyncio.sleep(0.2)
        await session._put_outbox(OutboxMessage(kind="agent", text="hello"))
        await asyncio.sleep(0.2)
        await session._put_outbox(OutboxMessage(kind="__end__", text=""))

    task = asyncio.create_task(_drive())
    try:
        sse = "".join(await _collect_all(emitter.stream()))
    finally:
        await task
        source.close()
    names = _reyn_display_names(parse_sse_blocks(sse.split("\n")))

    assert "reyn.display.agent" in names, (
        f"the tap produced nothing — the arm below would be vacuous: {names}"
    )
    assert "reyn.display.__session_switch_request__" not in names, (
        f"the switch sentinel leaked onto the wire (tap consumption + the "
        f"CONTROL_FILTER_KINDS backstop both bypassed): {names}"
    )


@pytest.mark.asyncio
async def test_end_sentinel_terminates_the_stream() -> None:
    """Tier 2: ``__end__`` terminates the stream — frames after it are never
    emitted (the emitter returns on the sentinel)."""
    frames = [
        DisplayFrame(OutboxMessage(kind="agent", text="before end")),
        DisplayFrame(OutboxMessage(kind="__end__", text="")),
        DisplayFrame(OutboxMessage(kind="agent", text="AFTER end — must not appear")),
    ]
    events = await _wire_events(frames)
    blob = "".join(str(ev.data) for ev in events)
    assert "before end" in blob
    assert "AFTER end" not in blob
