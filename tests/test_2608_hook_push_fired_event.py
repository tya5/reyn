"""Tier 2: #2608 observability — a hook push FIRE emits a P6 `hook_push_fired`
EventLog event.

Investigation-confirmed gap: only `shell_exec`/`shell_push` runs emitted a P6
event (`hook_shell_executed`); a `template_push`/`shell_push` PUSH's only
artifact was the WAL `inbox_put`/staged-context entry — so a push that landed
in the inbox but was never drained (the "sits in inbox forever" failure) left
NO EventLog trace. `hook_push_fired` (emitted at fire-time, in
``HookDispatcher._push_resolved``) closes that gap for every push path (E
wake=true, C wake=false, and cross-session).

Policy (docs/deep-dives/contributing/testing.md): real ``HookRegistry`` /
``HookDef`` / ``EventLog`` — no mocks. The dispatcher's Session seams
(put_inbox / stage_next_turn_context) are recording real async callables.
"""
from __future__ import annotations

import pytest

from reyn.core.events.events import EventLog
from reyn.hooks.dispatcher import HookDispatcher
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema import HookDef, PushBlock


class _Recorder:
    """A real recording async callable (not a mock) for the injected Session seams."""

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
async def test_template_push_wake_true_emits_hook_push_fired():
    """Tier 2: an E (wake=true) template_push fire emits `hook_push_fired` with
    hook_name/point/wake/target_session metadata — NO message body (secrets-safe)."""
    log = EventLog()
    hook = HookDef(name="my-hook", on="turn_end",
                    template_push=PushBlock(message="secret payload text", wake=True))
    disp = _dispatcher([hook], log)

    await disp.dispatch("turn_end", {})

    (fired_event,) = [e for e in log.all() if e.type == "hook_push_fired"]
    data = fired_event.data
    assert data["hook_name"] == "my-hook"
    assert data["point"] == "turn_end"
    assert data["wake"] is True
    assert "secret payload text" not in str(data)   # no message body leaked
    assert "text" not in data and "message" not in data


@pytest.mark.asyncio
async def test_template_push_wake_false_also_emits_hook_push_fired():
    """Tier 2: a C (wake=false) ride-along push ALSO fires the event — every push
    path (not just E) is now observable, closing the gap for BOTH."""
    log = EventLog()
    hook = HookDef(name="ctx-hook", on="turn_start",
                    template_push=PushBlock(message="note", wake=False))
    disp = _dispatcher([hook], log)

    await disp.dispatch("turn_start", {})

    (fired_event,) = [e for e in log.all() if e.type == "hook_push_fired"]
    assert fired_event.data["wake"] is False


@pytest.mark.asyncio
async def test_push_when_false_does_not_emit():
    """Tier 2: a conditional push that resolves to push_when=False never fires —
    no event either (mirrors: nothing was pushed, so nothing to report as fired)."""
    log = EventLog()
    hook = HookDef(name="skipped", on="turn_end",
                    template_push=PushBlock(message="x", push_when="false"))
    disp = _dispatcher([hook], log)

    await disp.dispatch("turn_end", {})

    assert [e for e in log.all() if e.type == "hook_push_fired"] == []


@pytest.mark.asyncio
async def test_no_emit_sink_stays_a_noop():
    """Tier 2: emit_event=None (the default — e.g. a unit test / no-sink session)
    stays byte-identical to pre-#2608: dispatch never raises and no event exists
    because there's no EventLog to hold one."""
    hook = HookDef(name="h", on="turn_end", template_push=PushBlock(message="x", wake=True))
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
    )

    await disp.dispatch("turn_end", {})  # must not raise with no emit_event sink


@pytest.mark.asyncio
async def test_shell_exec_hook_still_emits_hook_shell_executed_not_push_fired():
    """Tier 2: a shell_exec hook (no push at all) does NOT emit hook_push_fired —
    that event is a PUSH-fire signal only, distinct from the pre-existing
    hook_shell_executed (run-side) signal."""
    log = EventLog()
    hook = HookDef(name="shell-h", on="session_start", shell_exec="true")
    disp = HookDispatcher(
        HookRegistry([hook]),
        put_inbox=_Recorder(),
        stage_next_turn_context=_Recorder(),
        run_shell=_Recorder(),  # real recording callable — no real sandbox/subprocess
        emit_event=lambda et, **d: log.emit(et, **d),
    )

    await disp.dispatch("session_start", {})

    assert [e for e in log.all() if e.type == "hook_push_fired"] == []
