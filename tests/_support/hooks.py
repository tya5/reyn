"""``collect_hook_events`` / ``run_one_turn`` — the hook-event-bus mirror of
``tests/_support/events.py``'s ``collect_events``/``settle``, for #5494.

architect finding (2026-08-29, while reviewing #5491): a test holding only a
``Session`` had a public seam for audit events (``subscribe_audit_events``,
and #5467's own ``collect_events(session)``/``settle(session)``) but NONE
for hook events, and none for driving exactly one turn from a piece of user
text — ``run()`` is an unbounded inbox while-loop, ``run_one_iteration()``
processes exactly one inbox item of a SINGLE kind. Before this, such a test
had no way to do either except reach ``session._hook_bus.subscribe()`` /
``session._run_router_loop(...)`` directly — #5470's own production witness
(``tests/dev/test_llm_stub_tool_call_5470.py``) did exactly that, and is
this fix's own first migrated consumer.

architect's ruling (#5467's own shape, applied HERE — same conclusion,
narrower acceptance): production gets NO new public API for either — a
``Session.subscribe_hook_events()``/``Session.run_one_turn()`` with no real
production consumer would be a seam manufactured purely for tests, the
exact shape #5442/#5447/#5443 removed the same night this issue was filed.
The private reach narrows to the functions in THIS ONE file instead, same
as ``collect_events``/``settle``'s own ``_resolve_log``.

**Deliberately narrower than ``collect_events``**: :func:`collect_hook_events`
accepts a ``Session`` ONLY, not also a raw ``HookBus`` (unlike
``collect_events``'s dual ``EventLog``-or-``Session`` acceptance) —
architect's explicit correction: accepting more types only grows the
"which one do I pass" decision a caller has to make, and every current/
foreseeable caller already holds a ``Session``, never a bare ``HookBus``.

**Population split, not bundled** (architect's own measurement,
``origin/main``, 2026-08-29): ``_hook_bus`` — 6 files — is small enough to
seam-and-migrate in ONE PR (this one); ``_run_router_loop`` — 29 files, 85
sites — is not, so :func:`run_one_turn` is added here (closing the "no
public 1-turn seam" half of this issue) but its OWN migration is deferred
to a later PR, the same phase-1/phase-2 split #5467 used (bundling both
would risk the "#5467 phase 2, two unfinished migrations in parallel"
shape architect explicitly weighed against).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.hooks.bus import HookBusSubscription
    from reyn.runtime.session import Session


def collect_hook_events(session: "Session") -> "HookBusSubscription":
    """Subscribe to *session*'s hook-event bus (the ONE place in ``tests/``
    allowed to read ``session._hook_bus`` for the purpose of subscribing to
    it — a test may still reach ``session._hook_bus.publish(...)`` directly
    to DRIVE its own scenario, the same "arrange, not assert" distinction
    ``tests/_support/events.py``'s own ``_resolve_log`` already relies on).

    Returns ``HookBus``'s own native subscription object (an async context
    manager with ``get``/``get_nowait`` — see ``reyn.hooks.bus``'s own
    module docstring) — the SAME shape ``reyn.hooks.composer``/
    ``composed_consumer`` already use internally as real production
    subscribers, not an adapted callback list the way ``collect_events``
    wraps ``EventLog.add_subscriber``. ``HookBus`` has no callback-style
    registration to wrap; forcing one here would add an internal draining
    task this module does not otherwise need, purely to look like
    ``collect_events``.

    *session* only — deliberately narrower than ``collect_events``'s
    dual ``EventLog``-or-``Session`` acceptance (see module docstring).
    """
    return session._hook_bus.subscribe()


async def run_one_turn(session: "Session", user_text: str, chain_id: str) -> None:
    """Run exactly one turn for *user_text* under *chain_id* on *session*
    (the ONE place in ``tests/`` allowed to read ``session._run_router_loop``
    for the purpose of driving one turn — same scoping discipline as
    :func:`collect_hook_events` above).

    Neither ``session.run()`` (an unbounded while-loop draining the
    session's inbox) nor ``session.run_one_iteration()`` (processes exactly
    one inbox item of a SINGLE kind) is the shape a test holding only a
    ``Session`` — no inbox access — needs to drive one turn directly and
    synchronously to completion. ``_run_router_loop`` is Session's own
    internal turn-execution seam (already used internally by ``run()``,
    ``run_one_iteration()``, and ``reyn.runtime.services.inter_agent_
    messaging``'s several injection points); this wrapper changes none of
    that seam's behavior, it gives a test a single named place to reach it
    from, instead of each test file reaching ``session._run_router_loop``
    on its own.
    """
    await session._run_router_loop(user_text, chain_id)
