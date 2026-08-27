"""Tier 2: #5276 — the ``/visibility`` and ``/hook`` toggles emit an
audit-event for the operator's own action.

Root cause (lead-coder ruling, #5276 design review): #5276's own
investigation (charter lens 7, "reconstructable from the audit-event
trail") found these two operator actions had NO audit-event at all —
not a performance gap, a pre-existing observability hole this session's
own design pass surfaced by writing "no emit found" instead of assuming
one existed. Fix: ``Session.set_capability_visible`` emits
``visibility_changed`` (kind/name/on) unconditionally once the forwarded
call returns without raising; ``Session.set_hook_enabled`` emits
``hook_changed`` (name/enabled) ONLY on its ``applied=True`` path — the
#5230 refusal path changes nothing, so nothing is recorded there.

Real ``Session`` + real ``EventLog`` — no mocks. Uses
``tests/_support/events.py``'s ``collect_events``/``settle`` — the
established test-side mirror of production's real subscriber mechanism
(#3868/#4966) — rather than reading any private buffer, mirroring
``test_5230_hook_off_origin_aware_confirmation.py``'s own real-seam
pattern for the toggle side of this fix. ``settle`` is required here
because dispatch to subscribers runs on a background consumer task
whenever an event loop is running (#4966) — a synchronous read right
after the triggering call, with no yield in between, can otherwise miss
delivery.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle

_STARTUP_HOOK_NAME = "project-supervision-hook"
_STARTUP_HOOKS = [
    {
        "on": "turn_end",
        "name": _STARTUP_HOOK_NAME,
        "template_push": {"message": "startup fired", "wake": True},
    },
]


def _make_session(tmp_path: Path, *, hooks_config=None) -> Session:
    return make_session(
        agent_name="alice",
        state_log=StateLog(tmp_path / "s.wal"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "alice" / "state" / "snapshot.json",
        reactivity=ReactivityConfig(hooks_config=hooks_config),
    )


@pytest.mark.asyncio
async def test_visibility_toggle_emits_visibility_changed(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance — a plain ``/visibility off`` toggle emits
    ``visibility_changed`` with the operator's own kind/name/on, exactly
    once."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path)
    collected = collect_events(s._audit_events)

    s.set_capability_visible("tool", "grep", False)
    await settle(s._audit_events)

    seen = [e for e in collected if e.type == "visibility_changed"]
    assert seen, "expected a visibility_changed event, got none"
    assert seen == [seen[0]], f"expected exactly one visibility_changed event, got {seen}"
    assert seen[0].data["kind"] == "tool"
    assert seen[0].data["name"] == "grep"
    assert seen[0].data["on"] is False


@pytest.mark.asyncio
async def test_hook_toggle_emits_hook_changed_only_when_applied(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance + falsification contrast. Disabling a genuine
    per-session hook emits ``hook_changed``; disabling a PROTECTED
    (startup-origin) hook is refused (#5230) and must emit NOTHING —
    the refusal changed no state, so there is nothing to record."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    collected = collect_events(s._audit_events)

    # Refused: startup-origin hook, no state change → no event.
    refused = s.set_hook_enabled(_STARTUP_HOOK_NAME, False)
    await settle(s._audit_events)
    assert [e for e in collected if e.type == "hook_changed"] == [], (
        f"a refused (unapplied) hook toggle must emit nothing, got {collected}"
    )
    assert refused.applied is False

    # Applied: an unknown/per-session name is freely disableable → event fires.
    applied = s.set_hook_enabled("some-per-session-hook", False)
    await settle(s._audit_events)
    seen = [e for e in collected if e.type == "hook_changed"]
    assert applied.applied is True
    assert seen, "expected a hook_changed event, got none"
    assert seen == [seen[0]], f"expected exactly one hook_changed event, got {seen}"
    assert seen[0].data["name"] == "some-per-session-hook"
    assert seen[0].data["enabled"] is False
