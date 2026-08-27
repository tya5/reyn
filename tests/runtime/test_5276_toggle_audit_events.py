"""Tier 2: #5276 — the ``/visibility`` and ``/hook`` toggles emit an
audit-event for the operator's own action.

Root cause (lead-coder ruling, #5276 design review): #5276's own
investigation (charter lens 7, "reconstructable from the audit-event
trail") found these two operator actions had NO audit-event at all —
not a performance gap, a pre-existing observability hole this session's
own design pass surfaced by writing "no emit found" instead of assuming
one existed. Fix: ``Session.set_capability_visible`` emits
``visibility_changed`` (kind/name/on/applied) unconditionally once the
forwarded call returns without raising; ``Session.set_hook_enabled``
emits ``hook_changed`` (name/enabled/applied/origin) on BOTH outcomes.

Corrected mid-review (architect BLOCK on the first draft, #5277): that
first draft had ``hook_changed`` fire ONLY on the applied path — a
refused disable of a PROTECTED hook emitted nothing at all. Architect:
the two kinds were then following DIFFERENT rules ("did state change"
for hooks vs "did the operator act" for visibility), and the refused
path is exactly the event lens 7 needs to answer "why is this
protected hook still running after someone tried to stop it"
(#5041/#5213) — a kind that only ever fires on success can never answer
that, and the meaning cannot be widened later without changing what
already-logged events of the SAME kind mean (the #5261-rejected shape).
Both branches now emit from the start, with ``applied: bool``
distinguishing them.

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
    ``visibility_changed`` with the operator's own kind/name/on/applied,
    exactly once."""
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
    assert seen[0].data["applied"] is True


@pytest.mark.asyncio
async def test_hook_toggle_emits_hook_changed_on_both_outcomes(tmp_path, monkeypatch) -> None:
    """Tier 2: acceptance + falsification contrast (architect-corrected,
    #5277). Disabling a genuine per-session hook emits ``hook_changed``
    with ``applied=True``; disabling a PROTECTED (startup-origin) hook is
    refused (#5230) but STILL emits ``hook_changed``, with
    ``applied=False`` — the refused attempt is exactly what lens 7 needs
    on record, not silence."""
    monkeypatch.chdir(tmp_path)
    s = _make_session(tmp_path, hooks_config=_STARTUP_HOOKS)
    collected = collect_events(s._audit_events)

    # Refused: startup-origin hook — no state change, but the ATTEMPT is recorded.
    refused = s.set_hook_enabled(_STARTUP_HOOK_NAME, False)
    await settle(s._audit_events)
    seen_refused = [e for e in collected if e.type == "hook_changed"]
    assert refused.applied is False
    assert seen_refused, "a refused hook-disable attempt must still emit hook_changed"
    assert seen_refused == [seen_refused[0]], (
        f"expected exactly one hook_changed event for the refused attempt, got {seen_refused}"
    )
    assert seen_refused[0].data["name"] == _STARTUP_HOOK_NAME
    assert seen_refused[0].data["enabled"] is False
    assert seen_refused[0].data["applied"] is False
    assert seen_refused[0].data["origin"] == "startup"

    # Applied: an unknown/per-session name is freely disableable → applied=True.
    applied = s.set_hook_enabled("some-per-session-hook", False)
    await settle(s._audit_events)
    seen_applied = [e for e in collected if e.type == "hook_changed" and e is not seen_refused[0]]
    assert applied.applied is True
    assert seen_applied, "expected a hook_changed event for the applied toggle"
    assert seen_applied == [seen_applied[0]], (
        f"expected exactly one NEW hook_changed event, got {seen_applied}"
    )
    assert seen_applied[0].data["name"] == "some-per-session-hook"
    assert seen_applied[0].data["enabled"] is False
    assert seen_applied[0].data["applied"] is True
