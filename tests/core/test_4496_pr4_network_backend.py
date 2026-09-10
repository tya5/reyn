"""Tier 2: #4496 PR-4 — the `network` EventBackend + `on_failure` policy.

Inherits the SAME acceptance criteria architect/lead-coder pinned for the
`discard` backend in #4496 PR-2 (`tests/core/test_4496_pr2_event_backend.py`),
applied to `network`/`on_failure=discard` per the issue's own instruction
("受入は本文の必須条件…をそのまま継承してください"):

  1. A `network` backend whose send FAILS still lets subscribers (CUI/AG-UI)
     receive the event — witness is actual subscriber delivery, not "emit
     was called" (the issue's own warning: discard's whole point is that
     events disappear from storage, so a green check only confirming "emit
     ran" proves nothing).
  2. `audit_seq` still increments monotonically when the send fails —
     "discarded, so also skip the number" would be WRONG.
  3. The backend's own send failure does not reach/interrupt subscriber
     dispatch (NetworkEventBackend's write() never raises at all — see ④).
  4. Falsify: if `NetworkEventBackend` (or any backend) were inserted as a
     subscriber instead of called from `emit()`'s own pre-dispatch seam, a
     raising write() would abort delivery to every LATER subscriber — same
     falsify shape `test_4496_pr2_event_backend.py` already pins generically
     for ANY backend; this file's own ①/③ tests below are the network-
     specific instance of that same falsify (a future edit that threaded
     `NetworkEventBackend` into `self._subscribers` turns them red
     immediately, for the same measured reason as #4587's own strip).

No mocks of reyn's own code: a real `httpx.Client` is used throughout —
either wired to `httpx.MockTransport` (httpx's own supported test seam,
already this repo's established pattern, see
`tests/runtime/test_webhook_delivery_ack.py`) for a deterministic
success/failure response, or pointed at `http://127.0.0.1:1/` (a port with
no listener — a REAL, fast, loopback-only connection-refused failure, no
internet dependency) for the "genuinely unreachable endpoint" case. The
local spool uses a real `EventStore` (cheaply constructible under
`tmp_path`), never a hand-rolled stand-in.
"""
from __future__ import annotations

import httpx
import pytest

from reyn.config import AuditEventsConfig
from reyn.core.events.backend import NetworkEventBackend
from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from tests._support.agent_session import make_session
from tests._support.events import settle

_REFUSED_ENDPOINT = "http://127.0.0.1:1/events"


def _failing_client() -> httpx.Client:
    """A real httpx.Client wired to MockTransport's own handler, which
    always raises a (real httpx) connection error — deterministic, no
    socket involved, not a fake of reyn's own code."""

    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated network failure", request=request)

    return httpx.Client(transport=httpx.MockTransport(_handler))


def _ok_client() -> httpx.Client:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(_handler))


# ── 1 + 3. subscribers still receive events when the network send fails ──


@pytest.mark.asyncio
async def test_network_backend_send_failure_subscribers_still_receive_events() -> None:
    """Tier 2: witness ① — with backend=network and a send that ALWAYS
    fails, a subscriber still gets every emitted event. The witness is the
    subscriber's own received list, not "emit returned an Event"."""
    backend = NetworkEventBackend(endpoint=_REFUSED_ENDPOINT, client=_failing_client())
    log = EventLog(backend=backend)
    received = []
    log.add_subscriber(received.append)

    e1 = log.emit("tool_executed", op="read")
    e2 = log.emit("tool_executed", op="read")
    await log.drain()
    backend.wait_idle()

    assert received == [e1, e2]
    backend.close()


@pytest.mark.asyncio
async def test_network_backend_never_raises_from_write_on_send_failure() -> None:
    """Tier 2: witness ③, NetworkEventBackend's own contract — write()
    itself never raises on a send failure (the failure is routed through
    on_failure internally); EventLog.emit()'s own try/except is a SECOND
    line of defense, not the only one. Witnessed directly by calling
    write() synchronously and waiting for the background send to actually
    run (wait_idle — a real condition, never a sleep)."""
    backend = NetworkEventBackend(endpoint=_REFUSED_ENDPOINT, client=_failing_client())
    from reyn.schemas.models import Event

    backend.write(Event(type="tool_executed", data={"op": "read"}))
    backend.wait_idle()  # the failing send has now actually run
    backend.close()
    # No exception reached here — that IS the assertion.


# ── 2. audit_seq still increments when the send fails ────────────────────


@pytest.mark.asyncio
async def test_network_backend_send_failure_audit_seq_still_monotonic() -> None:
    """Tier 2: witness ② — audit_seq keeps incrementing under a failing
    `network` backend; "discarded, so also skip the number" would be
    wrong (#4496's own contract 3: the number counts occurrence, not
    write/send success)."""
    backend = NetworkEventBackend(endpoint=_REFUSED_ENDPOINT, client=_failing_client())
    log = EventLog(backend=backend)
    e1 = log.emit("tool_executed", op="read")
    e2 = log.emit("tool_executed", op="read")
    e3 = log.emit("tool_executed", op="read")
    assert (e1.data["audit_seq"], e2.data["audit_seq"], e3.data["audit_seq"]) == (1, 2, 3)
    await log.drain()
    backend.wait_idle()
    backend.close()


# ── 4. on_failure=discard (default): nothing local, not even a spool ─────


@pytest.mark.asyncio
async def test_on_failure_discard_writes_nothing_locally() -> None:
    """Tier 2: the default policy — a failed send leaves no local trace
    anywhere, not even in a spool (no spool_store is even wired)."""
    backend = NetworkEventBackend(
        endpoint=_REFUSED_ENDPOINT, on_failure="discard", client=_failing_client(),
    )
    from reyn.schemas.models import Event

    backend.write(Event(type="tool_executed", data={"op": "read"}))
    backend.wait_idle()
    backend.close()
    gaps = backend.declare_gaps()
    assert any("discard" in g for g in gaps)


# ── 5. on_failure=spool: a failed send IS held on local disk ─────────────


@pytest.mark.asyncio
async def test_on_failure_spool_writes_the_failed_event_to_the_real_spool_store(
    tmp_path,
) -> None:
    """Tier 2: the owner's own framing (#4496) — choosing `network` to NOT
    keep events locally, and then having on_failure=spool silently keep
    them locally on a failure, must be TRUE and SAID plainly. Witnessed
    against a real `EventStore` (cheaply constructible, never a hand-
    rolled stand-in) — the spooled event is actually readable back from
    disk, not merely "a write() call was made"."""
    spool_store = EventStore(tmp_path / "network_spool")
    backend = NetworkEventBackend(
        endpoint=_REFUSED_ENDPOINT,
        on_failure="spool",
        spool_store=spool_store,
        client=_failing_client(),
    )
    from reyn.schemas.models import Event

    backend.write(Event(type="tool_executed", data={"op": "read", "marker": "spooled"}))
    backend.wait_idle()
    backend.close()
    await spool_store.flush()

    spooled = list(spool_store.iter_all())
    assert any(e.data.get("marker") == "spooled" for e in spooled), (
        "the failed event must actually land in the real spool EventStore "
        f"— got {[e.data for e in spooled]}"
    )

    gaps = backend.declare_gaps()
    assert any("held on local disk" in g for g in gaps)
    await spool_store.aclose()


@pytest.mark.asyncio
async def test_on_failure_spool_does_not_spool_a_successful_send(tmp_path) -> None:
    """Tier 2: accept-side — a send that SUCCEEDS never touches the
    spool at all (spool is strictly an on-failure mechanism)."""
    spool_store = EventStore(tmp_path / "spool")
    backend = NetworkEventBackend(
        endpoint="http://example.invalid/events",
        on_failure="spool",
        spool_store=spool_store,
        client=_ok_client(),
    )
    from reyn.schemas.models import Event

    backend.write(Event(type="tool_executed", data={"op": "read"}))
    backend.wait_idle()
    backend.close()
    await spool_store.flush()

    assert list(spool_store.iter_all()) == []
    await spool_store.aclose()


# ── declare_gaps: contract 2, always names the no-local-copy gap ─────────


def test_declare_gaps_always_names_the_network_delivery_gap() -> None:
    """Tier 2: contract 2 — even a SUCCESSFUL network backend has nothing
    local for `reyn events replay`/support-bundle/dogfood_trace to read;
    declare_gaps() must say so unconditionally, not only on failure."""
    backend = NetworkEventBackend(endpoint=_REFUSED_ENDPOINT, client=_ok_client())
    gaps = backend.declare_gaps()
    assert gaps
    assert any("endpoint" in g for g in gaps)
    backend.close()


# ── config: `audit_events.backend=network` is a real, wired value ────────


def test_audit_events_config_backend_accepts_network():
    """Tier 1: `network` is a real, wired `AuditEventsConfig.backend` value
    (dataclass-level — the parser's own fallback rules are covered
    separately in `tests/config/test_4496_pr2_audit_events_backend_config.py`)."""
    cfg = AuditEventsConfig(backend="network", network_endpoint="http://x.invalid/events")
    assert cfg.backend == "network"


# ── Session-level, real production path (not a hand-built EventLog) ──────


@pytest.mark.asyncio
async def test_session_with_network_backend_still_delivers_to_real_subscribers(
    tmp_path,
) -> None:
    """Tier 2: the real production-path witness — driven through
    `make_session` (mirrors production's `scoped_session_factory.py`
    construction shape), with `audit_events.backend=network` pointed at a
    port with NO listener (`http://127.0.0.1:1/` — a real, fast,
    loopback-only connection failure, no internet dependency, no mock of
    reyn's own code). A subscriber attached via `subscribe_audit_events`
    still receives the event even though the network send behind it is
    guaranteed to fail."""
    session = make_session(
        agent_name="network-backend-test",
        workspace_state_dir=tmp_path / ".reyn",
        events_config=AuditEventsConfig(
            backend="network", network_endpoint=_REFUSED_ENDPOINT, network_timeout_s=1.0,
        ),
    )

    received = []
    session.subscribe_audit_events(received.append)
    emitted = session._audit_events.emit("test_event", foo="bar")
    await settle(session)

    assert received == [emitted]
    assert emitted.data.get("foo") == "bar"
    assert emitted.data.get("audit_seq") == 1

    await session.aclose_audit_events()
