"""Tier 2: #2344 — the chat axis executes stacked tool_calls SERIALLY (declaration order).

Owner design decision: Reyn must not unilaterally parallelize a round's tool_calls via
``asyncio.gather``. The LLM API returns tool_calls ordered but not independence-guaranteed, and
there is no workspace-write lock, so a concurrent gather races on order-dependent calls (write X
then read X). ``RouterLoop.dispatch`` now runs a serial ``for a in actions: await`` — matching the
already-serial phase axis. Ordering + no-short-circuit error semantics are preserved.

The order-dependent falsify is deterministic (not scheduling-luck): the writer yields once
(``await asyncio.sleep(0)``) BEFORE mutating shared state. Under the old gather the reader
coroutine interleaves at that yield and observes the pre-write state; under serial the writer
completes fully before the reader starts. No mocks — a RouterLoop subclass overrides the real
``_dispatch_resolved`` collaborator with ordered, observable handlers (the same real-fake seam as
the ``llm_caller`` constructor injection).
"""
from __future__ import annotations

import asyncio

import pytest

from reyn.core.dispatch.dispatcher import DispatchContext, dispatch_tool
from reyn.core.events.events import EventLog
from reyn.runtime.router_loop import RouterLoop
from tests._support.router_loop import FakeRouterHost


class _OrderedDispatchLoop(RouterLoop):
    """A RouterLoop whose ``_dispatch_resolved`` routes to ordered, observable handlers.

    ``write`` yields once before setting ``shared["written"]`` — the deterministic seam that
    exposes gather's interleave. ``read`` reports whether it observed the write. ``boom`` returns
    a normalized error (``dispatch_tool`` never raises) to check no-short-circuit. ``trace`` records
    completion order.
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.shared: dict = {"written": False}
        self.trace: list[str] = []

    async def _dispatch_resolved(self, name: str, args: dict) -> dict:
        if name == "write":
            await asyncio.sleep(0)  # yield: under gather the reader runs here, before the write lands
            self.shared["written"] = True
            self.trace.append("write")
            return {"status": "ok", "name": "write"}
        if name == "read":
            self.trace.append("read")
            return {"status": "ok", "name": "read", "saw_write": self.shared["written"]}
        if name == "boom":
            self.trace.append("boom")
            return {"status": "error", "error": {"kind": "x", "message": "boom"}}
        self.trace.append(name)
        return {"status": "ok", "name": name}


def _loop() -> _OrderedDispatchLoop:
    return _OrderedDispatchLoop(host=FakeRouterHost(), chain_id="chain-2344", max_iterations=5)


@pytest.mark.asyncio
async def test_order_dependent_calls_execute_serially(tmp_path):
    """Tier 2: write-then-read in one round runs serially → the read OBSERVES the write.

    RED under the old ``asyncio.gather`` (the reader interleaves at the writer's yield and sees
    ``written=False``); GREEN with the serial for-loop (the writer completes first)."""
    loop = _loop()
    results = await loop.dispatch([{"name": "write", "args": {}}, {"name": "read", "args": {}}])
    assert results[1]["saw_write"] is True, "serial: the read must observe the preceding write"
    assert loop.trace == ["write", "read"], "completion order is strict declaration order"


@pytest.mark.asyncio
async def test_results_returned_in_declaration_order(tmp_path):
    """Tier 2: N calls → results[i] aligns with actions[i] (tool_calls[i] ↔ tool_results[i]);
    the ordering contract is unchanged from gather's index-preservation."""
    loop = _loop()
    actions = [{"name": f"t{i}", "args": {}} for i in range(6)]
    results = await loop.dispatch(actions)
    assert [r["name"] for r in results] == [a["name"] for a in actions]
    assert loop.trace == [a["name"] for a in actions]


@pytest.mark.asyncio
async def test_error_call_does_not_short_circuit(tmp_path):
    """Tier 2: an error result mid-round does NOT stop the rest — every call still runs
    (dispatch_tool normalizes errors, never raises), same as before."""
    loop = _loop()
    results = await loop.dispatch(
        [{"name": "write", "args": {}}, {"name": "boom", "args": {}}, {"name": "read", "args": {}}]
    )
    assert [r["status"] for r in results] == ["ok", "error", "ok"]
    assert results[2]["saw_write"] is True, "the call after the error still ran (no short-circuit)"
    assert loop.trace == ["write", "boom", "read"]


class _RealEventDispatchLoop(RouterLoop):
    """Drives the REAL ``dispatch_tool`` per call, emitting the REAL P6 audit events
    (``tool_called``/``tool_returned``) into a REAL ``EventLog`` (not a stub).

    ``_dispatch_resolved`` reconstructs the chat-branch ``DispatchContext`` (mirrors
    router_loop.py's ``caller_kind="router"`` path) with ``events=self.event_log`` — a real
    ``reyn.core.events.events.EventLog``, the same class the production session threads in — and a
    yield-controllable real invoker. The writer yields once before mutating shared state, exactly
    where the real ``await invoker`` yield sits between the ``tool_called`` and ``tool_returned``
    emits. So the append order recorded in the real EventLog is produced by the real dispatch +
    real emission path — no proxy. Under gather the pairs interleave; serial makes them contiguous.
    """

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.shared: dict = {"written": False}
        self.event_log = EventLog()

    async def _dispatch_resolved(self, name: str, args: dict) -> dict:
        async def _invoker(a: dict):
            if name == "write":
                await asyncio.sleep(0)  # the real await-invoker yield point
                self.shared["written"] = True
                return {"written": True}
            return {"saw_write": self.shared["written"]}

        dctx = DispatchContext(
            caller_kind="router", caller_id=self.host.agent_name,
            chain_id=self.chain_id, tool_catalog={name: {}}, events=self.event_log,
        )
        return await dispatch_tool(name=name, args=args, ctx=dctx, invoker=_invoker)


def _tool_event_seq(event_log: EventLog) -> list[tuple[str, str]]:
    """The (type, tool) sequence from the REAL EventLog's append-order replay source
    (``all()`` — the same list ``to_json()`` and every append-order consumer iterate)."""
    return [(e.type, e.data.get("tool")) for e in event_log.all()
            if e.type in ("tool_called", "tool_returned")]


@pytest.mark.asyncio
async def test_audit_event_order_is_declaration_order_contiguous(tmp_path):
    """Tier 2: #2344 durability — the REAL EventLog appends the P6 audit events in strict
    declaration order, each call's (tool_called, tool_returned) pair CONTIGUOUS.

    The P6 audit log is append-only + replay-capable; every append-order consumer (forwarders,
    audit reconstruction, event-derived rewind views) inherits its order. Under the old gather the
    pairs INTERLEAVE across concurrent calls (nondeterministic completion order → nondeterministic
    audit append order = a replay/reconstruction divergence risk); serial makes the append order
    deterministic and tool-call-boundary-clean. RED under gather (interleaved), GREEN under serial.
    Drives the real dispatch_tool → real EventLog emission (no stub)."""
    loop = _RealEventDispatchLoop(host=FakeRouterHost(), chain_id="chain-2344", max_iterations=5)
    await loop.dispatch([{"name": "write", "args": {}}, {"name": "read", "args": {}}])

    assert _tool_event_seq(loop.event_log) == [
        ("tool_called", "write"), ("tool_returned", "write"),
        ("tool_called", "read"), ("tool_returned", "read"),
    ], "real EventLog must append the audit events in contiguous declaration order"


@pytest.mark.asyncio
async def test_replay_reproduces_declaration_order(tmp_path):
    """Tier 2: #2344 durability — the REAL EventLog's replay (append-order iteration of ``all()``,
    the actual P6 replay semantics for the audit log) reproduces declaration order, and a cut
    between the two calls lands on a clean tool-call boundary.

    NB scope: chat-axis dispatch emits P6 AUDIT events (this EventLog); it writes NO WAL step, so
    the WAL-based ``_materialize_rewind`` engine is a separate substrate not on this path — this
    test drives the real audit-log replay, not a WAL rewind. Serial's contiguity means the pre-cut
    prefix replays exactly the COMPLETED first call (no half-executed call straddling the cut)."""
    loop = _RealEventDispatchLoop(host=FakeRouterHost(), chain_id="chain-2344", max_iterations=5)
    await loop.dispatch([{"name": "write", "args": {}}, {"name": "read", "args": {}}])

    # replay = iterate the real EventLog in append order (P6 replay semantics for the audit log).
    log = _tool_event_seq(loop.event_log)
    boundary = log.index(("tool_called", "read"))  # the tool-call boundary cut
    assert log[:boundary] == [("tool_called", "write"), ("tool_returned", "write")], \
        "the pre-cut prefix replays exactly the completed first call (no straddle)"
