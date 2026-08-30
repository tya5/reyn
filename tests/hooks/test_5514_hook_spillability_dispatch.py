"""Tier 2: #5514 §5/§8 — dispatch-time behaviour of a hook's declared
``spillability``.

Two things this file proves, both at the real production choke point
(``HookDispatcher._push_resolved``) rather than by reasoning about it:

1. A hook's declared ``spillability`` reaches the payload BOTH mouths read
   from — the wake=true trigger (E, ``Session._handle_hook_message``) and
   the wake=false ride-along (C, ``_handle_inbox_text``'s ``next_turn_
   context`` staging) — because both consume the SAME ``payload["spillability"]``
   key ``_push_resolved`` sets once. #5514 §8's own named hazard: a
   declaration reaching only ①(direct push) and silently vanishing at
   ②(the ride-along) is indistinguishable, from the declarer's side, from
   it working — a test exercising only ① would pass either way. This file
   drives BOTH wake=true and wake=false through the real dispatcher and
   reads the payload each one received.
2. A ``spillability: never`` hook whose push exceeds its own declared
   ``spillability_max_chars`` is REJECTED, never truncated (architect
   ruling, 2026-08-30, issue #5514): the push never reaches either
   consumer seam, exactly ONE ``hook_push_rejected_oversized`` event
   fires (not ``hook_push_fired`` — the push never fired), and no
   truncated form of the message appears anywhere reachable — proven
   against a companion within-cap push in the SAME test so the assertion
   is not vacuously true over an empty world (architect's own acceptance
   criterion ③).

Policy (docs/deep-dives/contributing/testing.md): real ``HookRegistry``/
``HookDef``/``EventLog`` — no mocks. The Session seams (put_inbox/
stage_next_turn_context) are recording real async callables, mirroring
``test_2608_hook_push_fired_event.py``'s own established pattern.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock
from reyn.runtime.chat_message import Spillability
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _Recorder:
    """A real recording async callable (not a mock) for the injected
    Session seams — mirrors test_2608_hook_push_fired_event.py's own."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def _dispatcher(hooks: list[HookDef], event_log: EventLog, **seams) -> HookDispatcher:
    seams.setdefault("put_inbox", _Recorder())
    seams.setdefault("stage_next_turn_context", _Recorder())
    return HookDispatcher(
        HookRegistry(hooks),
        put_inbox=seams["put_inbox"],
        stage_next_turn_context=seams["stage_next_turn_context"],
        emit_event=lambda et, **d: event_log.emit(et, **d),
    )


@pytest.mark.asyncio
async def test_declared_spillability_reaches_the_wake_true_payload():
    """Tier 2: E path — the payload put_inbox receives carries the hook's
    OWN declared member, not the general ChatMessage default."""
    hook = HookDef(
        name="my-hook", on="turn_end",
        template_push=PushBlock(message="hi", wake=True),
        spillability=Spillability.LAST_RESORT,
    )
    put_inbox = _Recorder()
    disp = _dispatcher([hook], EventLog(), put_inbox=put_inbox)

    await disp.dispatch("turn_end", {})

    (call,) = put_inbox.calls
    args, _kwargs = call
    payload = args[1]  # _put_inbox(TurnOrigin.HOOK, {**payload, "wake": True})
    assert payload["spillability"] == "last_resort"


@pytest.mark.asyncio
async def test_declared_spillability_reaches_construction_on_wake_true(
    tmp_path: Path,
) -> None:
    """Tier 2: #5514 §8 — lead-coder BLOCKING finding, 2026-08-30. The
    payload-only assertion above (``test_declared_spillability_reaches_
    the_wake_true_payload``) proved the declaration is TRANSPORTED —
    it did not prove ``Session._handle_hook_message`` (the wake=true
    consumer) actually READS it: an earlier version of that method
    hardcoded ``Spillability.FIRST_CHOICE`` regardless of what the
    payload carried, and this exact test shape (payload-only) stayed
    green through that regression, because "sent" and "sent to the
    right place and used there" are different claims. This test drives
    the REAL ``Session._handle_hook_message`` (not a re-implementation)
    with a ``spillability: never``-shaped payload and reads the
    CONSTRUCTED ``ChatMessage`` back off ``session.history`` — the
    actual site #5514 §8 requires the declaration reach.

    ``_run_router_loop`` is method-assigned to a real no-op async
    function (not a mock) so this test isolates ``_handle_hook_message``'s
    own history-append behaviour from a full router turn — mirrors
    ``test_hook_message_is_fanned_out_to_live_outbox``
    (test_hook_loop_valve_1800_7.py)'s own established pattern for the
    SAME method.

    Strip-falsify (performed during review): reverting
    ``_handle_hook_message`` to hardcode ``spillability=Spillability.
    FIRST_CHOICE`` makes the assertion below fail (``FIRST_CHOICE`` !=
    ``NEVER``), confirming this test — unlike the payload-only one — DOES
    catch the regression lead-coder found.
    """
    session = make_session(
        agent_name="spillability-construction-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )

    async def _noop(*_args, **_kwargs):
        return None

    session._run_router_loop = _noop  # type: ignore[method-assign]

    await session._handle_hook_message({
        "name": "policy-hook",
        "text": "a standing policy, never to be spilled",
        "wake": True,
        "spillability": Spillability.NEVER.value,
    })

    (only,) = [m for m in session.history if m.role == "system"]
    assert only.spillability is Spillability.NEVER


@pytest.mark.asyncio
async def test_declared_spillability_reaches_the_wake_false_ride_along_payload():
    """Tier 2: C path — #5514 §8's own named hazard. The SAME declaration,
    on a wake=false push, must reach stage_next_turn_context's payload
    too — this is the "②に届かないと黙って無視される" mouth."""
    hook = HookDef(
        name="ctx-hook", on="turn_start",
        template_push=PushBlock(message="note", wake=False),
        spillability=Spillability.LAST_RESORT,
    )
    stage = _Recorder()
    disp = _dispatcher([hook], EventLog(), stage_next_turn_context=stage)

    await disp.dispatch("turn_start", {})

    (call,) = stage.calls
    args, _kwargs = call
    payload = args[1]  # _stage_next_turn_context(HOOK_STAGE_KIND, payload)
    assert payload["spillability"] == "last_resort"


@pytest.mark.asyncio
async def test_undeclared_spillability_resolves_to_first_choice_at_both_mouths():
    """Tier 2: HookDef.spillability=None (undeclared) resolves to
    FIRST_CHOICE at the push site — not LAST_RESORT (ChatMessage's own
    general default) — see HookDef.spillability's own docstring for why.
    Checked at BOTH mouths, not just one."""
    wake_true_hook = HookDef(
        name="a", on="turn_end",
        template_push=PushBlock(message="hi", wake=True),
    )
    wake_false_hook = HookDef(
        name="b", on="turn_start",
        template_push=PushBlock(message="hi", wake=False),
    )
    put_inbox, stage = _Recorder(), _Recorder()
    disp = _dispatcher(
        [wake_true_hook], EventLog(), put_inbox=put_inbox, stage_next_turn_context=stage,
    )
    await disp.dispatch("turn_end", {})
    assert put_inbox.calls[0][0][1]["spillability"] == "first_choice"

    disp2 = _dispatcher(
        [wake_false_hook], EventLog(), put_inbox=put_inbox, stage_next_turn_context=stage,
    )
    await disp2.dispatch("turn_start", {})
    (call2,) = stage.calls
    args, _kwargs = call2
    payload = args[1]  # _stage_next_turn_context(HOOK_STAGE_KIND, payload)
    assert payload["spillability"] == "first_choice"


@pytest.mark.asyncio
async def test_never_push_within_cap_fires_normally_no_rejection_event():
    """Tier 2: the positive control this file's own docstring names —
    without it, the rejection test below could pass vacuously (an empty
    world where nothing at or under the cap is ever exercised)."""
    hook = HookDef(
        name="bounded", on="turn_end",
        template_push=PushBlock(message="short", wake=True),
        spillability=Spillability.NEVER, spillability_max_chars=100,
    )
    log = EventLog()
    collected = collect_events(log)
    put_inbox = _Recorder()
    disp = _dispatcher([hook], log, put_inbox=put_inbox)

    await disp.dispatch("turn_end", {})
    await settle(log)

    (call,) = put_inbox.calls
    assert call[0][1]["text"] == "short"
    assert [e for e in collected if e.type == "hook_push_rejected_oversized"] == []
    (fired,) = [e for e in collected if e.type == "hook_push_fired"]
    assert fired.data["hook_name"] == "bounded"


@pytest.mark.asyncio
async def test_oversized_never_push_is_rejected_not_truncated():
    """Tier 2: architect ruling, 2026-08-30 (#5514 §5/§8) — the 3-point
    acceptance verbatim: ① history unchanged (no put_inbox/stage call at
    all — nothing for a caller to append) ② exactly one event, naming the
    hook and its measured size ③ no truncated form of the message is
    reachable anywhere (checked against BOTH the (absent) inbox call and
    every event's own data)."""
    long_message = "X" * 500
    hook = HookDef(
        name="oversized", on="turn_end",
        template_push=PushBlock(message=long_message, wake=True),
        spillability=Spillability.NEVER, spillability_max_chars=100,
    )
    log = EventLog()
    collected = collect_events(log)
    put_inbox = _Recorder()
    disp = _dispatcher([hook], log, put_inbox=put_inbox)

    await disp.dispatch("turn_end", {})
    await settle(log)

    # ① history unchanged — nothing was ever pushed to either seam.
    assert put_inbox.calls == []

    # ② exactly one event, and it is the rejection kind (not push_fired).
    push_related = [
        e for e in collected
        if e.type in ("hook_push_fired", "hook_push_rejected_oversized")
    ]
    (rejection,) = push_related
    assert rejection.type == "hook_push_rejected_oversized"
    assert rejection.data["hook_name"] == "oversized"
    assert rejection.data["declared_max_chars"] == 100
    assert rejection.data["actual_chars"] == 500

    # ③ no truncated form of the message appears anywhere reachable —
    # neither a 100-char prefix nor the full 500-char body.
    truncated_prefix = long_message[:100]
    assert truncated_prefix not in str(collected)
    assert long_message not in str(collected)


@pytest.mark.asyncio
async def test_oversized_never_push_rejected_on_wake_false_too():
    """Tier 2: the rejection applies before the wake branch — a wake=false
    ride-along push is rejected identically, never staged half-truncated."""
    long_message = "Y" * 500
    hook = HookDef(
        name="oversized-c", on="turn_start",
        template_push=PushBlock(message=long_message, wake=False),
        spillability=Spillability.NEVER, spillability_max_chars=100,
    )
    log = EventLog()
    collected = collect_events(log)
    stage = _Recorder()
    disp = _dispatcher([hook], log, stage_next_turn_context=stage)

    await disp.dispatch("turn_start", {})
    await settle(log)

    assert stage.calls == []
    (rejection,) = [e for e in collected if e.type == "hook_push_rejected_oversized"]
    assert rejection.data["actual_chars"] == 500
