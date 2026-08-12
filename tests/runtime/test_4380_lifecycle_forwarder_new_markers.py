"""Tier 2: ChatLifecycleForwarder — new markers (#4380).

The audit-event side already declares/emits these kinds (``force_close_
triggered``, ``turn_cancelled``, ``chain_timeout``, ``permission_denied``,
``intervention_denied``) — a real owner-reported gap was that NONE of them
reached the conv pane, so an operator watching the LIVE session saw
nothing when any of these fired (only the Events tab, closed by default).
Owner ruling (#4380): add all of them, dim, same visual weight as the
existing ``[↑]``/``[✗]``/``[⚠]`` markers — "don't stand out." One
(``permission_denied``) additionally bundles consecutive repeats (a
separate PR concern, covered by ``test_4380_ingest_frame_bundles_
permission_denied.py`` — this file pins only the PRODUCER side: that the
emitted marker's TEXT and ``meta`` are correct, not the app-side
coalescing that consumes ``meta["lifecycle_bundle_key"]``).

#4381 PR-4 removed a SIXTH mechanism this file originally also covered,
``router_force_close_handoff`` (layer②, the router_loop_driver.py outer
retry) — owner ruling: "２の force close 廃止して spill にしよう。予算の
ための force close は残すで良い". ``force_close_triggered`` (layer①,
in-loop cumulative-budget cutoff — a different axis, cost not context
size) is unaffected and stays covered below.
"""
from __future__ import annotations

import asyncio
from typing import Any

from reyn.runtime.lifecycle_forwarder import ChatLifecycleForwarder
from reyn.schemas.models import Event


def _drain(q: asyncio.Queue) -> list[Any]:
    items: list[Any] = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def _fire(event_type: str, data: dict) -> list[Any]:
    q: asyncio.Queue = asyncio.Queue()
    fwd = ChatLifecycleForwarder(q)
    fwd(Event(type=event_type, data=data))
    return _drain(q)


def test_force_close_triggered_emits_a_dim_system_marker() -> None:
    """Tier 2: layer① in-loop cumulative-budget cutoff gets a live marker —
    previously silent (only visible via the Events tab)."""
    msgs = _fire("force_close_triggered", {"chain_id": "c1", "iteration": 3})
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[✗ force close: turn ended early to stay within budget]"


def test_turn_cancelled_emits_a_marker() -> None:
    """Tier 2: previously ZERO live visibility (router_loop.py's own
    history-append has no outbox side-effect — only visible on the NEXT
    restore). This is the genuinely new gap, closed here."""
    msgs = _fire("turn_cancelled", {"chain_id": "c1"})
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[✗ turn cancelled]"


def test_chain_timeout_emits_a_marker_naming_who_it_waited_on() -> None:
    """Tier 2: waiting_on + timeout_seconds surface — the documented
    payload (events.md's own table for this kind), not invented fields."""
    msgs = _fire(
        "chain_timeout",
        {
            "chain_id": "c1",
            "waiting_on": ["planner", "reviewer"],
            "timeout_seconds": 120,
            "origin_agent": "alpha",
        },
    )
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[✗ chain timeout: waiting on planner, reviewer (120s)]"


def test_chain_timeout_degrades_gracefully_with_no_waiting_on() -> None:
    """Tier 2: accept-side — a malformed/absent waiting_on still produces a
    marker (never a KeyError from a forward-compat event-shape drift)."""
    msgs = _fire("chain_timeout", {"chain_id": "c1"})
    (only,) = msgs
    assert only.text == "[✗ chain timeout: waiting on ?]"


def test_permission_denied_emits_a_marker_and_carries_the_bundle_key() -> None:
    """Tier 2: the ONE kind of the 6 that bundles (owner ruling) — the
    marker must carry ``meta["lifecycle_bundle_key"]`` = (kind, path,
    reason) verbatim, since app.py's own coalescing keys off exactly this
    tuple. Getting the key's SHAPE wrong here would silently break
    bundling without failing this producer-side test on its own."""
    msgs = _fire(
        "permission_denied",
        {
            "run_id": "r1", "actor": "alpha", "phase": "",
            "kind": "file.write", "path": "/etc/passwd", "reason": "denied by policy",
        },
    )
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[✗ permission denied: file.write /etc/passwd]"
    assert only.meta.get("lifecycle_bundle_key") == (
        "permission_denied", "file.write", "/etc/passwd", "denied by policy",
    )


def test_permission_denied_with_different_path_has_a_different_bundle_key() -> None:
    """Tier 2: owner's own constraint, applied literally — same op kind,
    DIFFERENT path, must produce a DIFFERENT bundle key (never conflated
    as "the same denial")."""
    msgs_a = _fire(
        "permission_denied",
        {"kind": "file.write", "path": "/a", "reason": "denied by policy"},
    )
    msgs_b = _fire(
        "permission_denied",
        {"kind": "file.write", "path": "/b", "reason": "denied by policy"},
    )
    assert msgs_a[0].meta["lifecycle_bundle_key"] != msgs_b[0].meta["lifecycle_bundle_key"]


def test_permission_denied_with_different_reason_has_a_different_bundle_key() -> None:
    """Tier 2: same kind AND same path, DIFFERENT reason — owner's own
    correction to the original (kind, path) proposal: "reason を足す"."""
    msgs_a = _fire(
        "permission_denied",
        {"kind": "file.write", "path": "/a", "reason": "denied by policy"},
    )
    msgs_b = _fire(
        "permission_denied",
        {"kind": "file.write", "path": "/a", "reason": "sandbox violation"},
    )
    assert msgs_a[0].meta["lifecycle_bundle_key"] != msgs_b[0].meta["lifecycle_bundle_key"]


def test_intervention_denied_emits_a_marker_with_no_bundle_key() -> None:
    """Tier 2: owner ruling — NOT bundled. The marker must carry no
    ``lifecycle_bundle_key`` at all, so app.py's coalescing never
    accidentally treats two different unanswered questions as one repeat."""
    msgs = _fire(
        "intervention_denied",
        {"intervention_id": "iv1", "kind": "ask_user", "run_id": "r1", "actor": "alpha", "reason": "session lost"},
    )
    (only,) = msgs
    assert only.kind == "system"
    assert only.text == "[✗ intervention denied: ask_user]"
    assert "lifecycle_bundle_key" not in only.meta


def test_unrelated_event_types_are_dropped() -> None:
    """Tier 1: existing accept-side guard, re-affirmed with the new kinds
    absent from the dispatch — an event this forwarder has no ``on_<kind>``
    handler for produces nothing (mirrors the file's own pre-existing
    ``test_unrelated_event_types_are_dropped``-style coverage for the
    original 9 kinds)."""
    msgs = _fire("some_unrelated_kind", {"anything": 1})
    assert msgs == []
