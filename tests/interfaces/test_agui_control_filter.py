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
- ``__attach_request__`` is a **live wire kind** — corrected in #3362, which
  measured it landing on the wire with the registry forwarder running. The
  forwarder's ``continue`` is SUBSCRIBER-LOCAL (it and the AG-UI tap are
  independent ``outbox_hub`` subscribers, and the hub fans every message out to
  every subscription), so it only means "not re-posted to ``repl_outbox``". Both
  sentinels reach the tap; what happens after that is decided per-kind by the tap
  and by ``CONTROL_FILTER_KINDS``, never by the forwarder. This file's docstring
  previously asserted the opposite — the canonical reasoning lives in
  ``protocol.py`` beside ``CONTROL_FILTER_KINDS``.

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


async def _collect_within(agen, *, window: float) -> "list":
    """Collect from an async generator for a BOUNDED wall-clock window.

    A broken tap can strand the stream permanently; bounding turns that into a
    fast, named assertion failure instead of a CI timeout.
    """
    out: list = []
    it = agen.__aiter__()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + window
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            out.append(await asyncio.wait_for(it.__anext__(), timeout=remaining))
        except (asyncio.TimeoutError, StopAsyncIteration):
            break
    return out


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
async def test_forwarder_continue_does_not_keep_a_sentinel_off_the_wire(
    tmp_path,
) -> None:
    """Tier 2: REACHABILITY, through the real tap — the registry forwarder's
    ``continue`` is subscriber-local and does not gate the AG-UI wire (#3362).

    Every other test in this file feeds a synthetic frame list straight to the
    emitter, which can only ever show what the emitter does with a frame it is
    HANDED — it cannot show whether a frame arrives at all. That gap is what let
    "``__attach_request__`` never reaches the AG-UI tap" survive in three
    docstrings while being false. This test closes it by running the REAL
    ``AgentRegistry`` forwarder (the thing that ``continue``s) concurrently with
    the REAL ``_SessionFrameSource`` + emitter, and reading the wire.

    Three arms, so neither direction can pass vacuously:

    - ``__attach_request__`` — not in ``CONTROL_FILTER_KINDS`` → **on the wire**,
      even though the forwarder swallowed it for ``repl_outbox``.
    - ``__session_switch_request__`` — **not on the wire**. Same tap, same
      forwarder, opposite outcome, which is what shows the forwarder is not the
      discriminator. ★The mechanism here is the TAP consuming the sentinel
      (``_drain_one_session``), NOT ``CONTROL_FILTER_KINDS``: measured by strip —
      removing the filter entry alone leaves this arm green, and only removing
      the filter entry AND the tap's consumption turns it red. The filter is a
      backstop for a source that does not consume it. This arm deliberately does
      not attribute itself to the filter; the synthetic
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
            OutboxMessage(kind="__attach_request__", text="beta")
        )
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
        sse = "".join(await _collect_within(emitter.stream(), window=5.0))
    finally:
        await task
        source.close()
    names = _reyn_display_names(parse_sse_blocks(sse.split("\n")))

    # ★Premise witness, on the PUBLIC surface: the forwarder is what ``continue``s,
    # so if it were not running this test would prove nothing about it. Rather than
    # inspecting its task, observe its EFFECT — ``__attach_request__("beta")`` makes
    # the forwarder swap the attached agent. alpha → beta is only reachable by that
    # branch actually executing, i.e. by the forwarder having consumed the very
    # sentinel that also reached the wire.
    assert reg.attached_name == "beta", (
        "the registry forwarder did not consume __attach_request__ — it is not "
        "running, so the 'continue does not gate the wire' claim is untested here"
    )
    assert "reyn.display.agent" in names, (
        f"the tap produced nothing — the arms below would be vacuous: {names}"
    )
    assert "reyn.display.__attach_request__" in names, (
        "__attach_request__ did NOT reach the wire — if this now holds, the "
        "forwarder/tap topology changed and protocol.py's reasoning needs "
        f"revisiting: {names}"
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
