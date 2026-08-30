"""Tier 2: #5260 — ``Session.subscribe_audit_events`` threads its new
``kinds`` param through to the underlying ``EventLog.add_subscriber`` (added
by #5263, already covered end-to-end at that layer by
``test_5260_subscriber_declared_kinds.py``).

Before this, the ONLY route to a fixed-interest declaration was reaching
into ``session._audit_events`` directly — the narrow public seam this
method exists to be an encapsulated alternative to (see this method's own
docstring: "so UI callers subscribe without reaching into
``_audit_events``") had no way to express one at all. A caller going
through the public API was stuck with "every event", the same over-broad
default #5260's own filing found at 3 internal registration sites.

Real ``Session`` throughout (mirrors ``test_2761_pr2_hotreload_immediate_
apply.py``'s own ``subscribe_audit_events`` witness style — effect through
the public seam, not a private-attribute peek).
"""
from __future__ import annotations

import pytest

from tests._support.agent_session import make_session
from tests._support.events import settle


@pytest.mark.asyncio
async def test_a_kinds_declaration_through_the_public_seam_is_honoured() -> None:
    """Tier 2: a callback registered via ``session.subscribe_audit_events(cb,
    kinds={...})`` is called for a declared kind and NOT for an undeclared
    one — the same filter contract #5263 gives ``EventLog.add_subscriber``
    directly, now reachable through the public seam too."""
    session = make_session(agent_name="test_agent")
    received: list[str] = []
    session.subscribe_audit_events(
        lambda e: received.append(e.type), kinds={"session_halted"},
    )

    # #5557: both emits below drive the "does the kinds= filter pass/block
    # by type" mechanism witness — the specific event types are arbitrary
    # (any two distinct real kinds would serve identically); the claim is
    # about subscribe_audit_events's filter, not about when production
    # actually emits session_halted/visibility_changed.
    session._audit_events.emit("session_halted", reason="test")
    session._audit_events.emit("visibility_changed", kind="tool", name="x", on=True, applied=True)
    await settle(session._audit_events)

    assert received == ["session_halted"], (
        f"a kinds= declaration through Session.subscribe_audit_events must "
        f"admit only the declared kind; observed={received!r}"
    )


@pytest.mark.asyncio
async def test_omitting_kinds_still_admits_every_event() -> None:
    """Tier 2: falsification pair — without this, a public seam that
    silently narrowed by default (rather than requiring an explicit
    ``kinds=``) would still pass the test above, and every EXISTING caller
    of ``subscribe_audit_events`` (none of which pass ``kinds``) would
    silently stop receiving most events."""
    session = make_session(agent_name="test_agent")
    received: list[str] = []
    session.subscribe_audit_events(lambda e: received.append(e.type))

    # #5557: same reasoning as the test above — drives the "no kinds= means
    # every event admitted" mechanism witness, event types are arbitrary.
    session._audit_events.emit("session_halted", reason="test")
    session._audit_events.emit("visibility_changed", kind="tool", name="x", on=True, applied=True)
    await settle(session._audit_events)

    assert received == ["session_halted", "visibility_changed"]
