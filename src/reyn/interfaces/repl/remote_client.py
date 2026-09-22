"""``reyn chat --connect <url>`` — the remote thin-client driver (ADR-0039 P3).

This is what makes the arc **reachable-for-purpose**: an operator runs ``reyn chat
--connect <url>``, the CLI opens an AG-UI SSE stream to the single-writer server,
decodes it back into the renderer's ``Frame`` vocabulary through the SAME
:class:`~reyn.interfaces.transport.agui.client.AgUiTransport` P2 built, and drives
the IDENTICAL stream-consuming client (:mod:`reyn.interfaces.repl.stream_client`) —
a different transport, the same client (D2). The operator sees an intervention over
the wire and answers it; the answer rides a ``TOOL_CALL_RESULT`` POST back to the
server, delivered BY ID through the single funnel.

Transport wiring only — no ``Session`` / ``Workspace`` / tool is touched here (the
single-writer contract): the client writes to the world ONLY through the transport's
``send`` seam (an httpx POST) and reads ONLY the SSE stream. A periodic heartbeat POST
is the client→server liveness signal the server's fail-close grace window reads.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator

from reyn.interfaces.transport.agui.protocol import (
    BOUNDED_PAYLOAD_TYPES,
    LONG_RUNNING_PAYLOAD_TYPES,
)
from reyn.interfaces.transport.control_outcome import ControlOutcome

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """Read a positive float override from the environment, else ``default``."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# Client→server heartbeat cadence (seconds). Comfortably under the server's
# liveness timeout (``DEFAULT_LIVENESS_TIMEOUT`` in
# ``interfaces/transport/agui/surface.py``, 60s) so a live client is never
# swept as dead — 25s keeps a 2.4x margin, in line with the idiomatic
# heartbeat/timeout ratio used by Socket.IO (25s/60s), Phoenix (30s) and
# SignalR (15s + 2x timeout). Overridable per-deployment via
# ``REYN_AGUI_HEARTBEAT_INTERVAL_S``; MUST stay below the server's timeout.
_HEARTBEAT_INTERVAL = _env_float("REYN_AGUI_HEARTBEAT_INTERVAL_S", 25.0)

#: #5894 (architect ruling ①-1): the read timeout for a BOUNDED control
#: POST (submit / cancel / answer / heartbeat, and every other payload
#: type :data:`~reyn.interfaces.transport.agui.protocol.BOUNDED_PAYLOAD_
#: TYPES` names) — the one constant, the one place. The SSE stream keeps
#: ``read=None`` (a live stream legitimately reads forever); a bounded
#: control request must not share that policy. Owner-hit: the server was
#: pinned by CPU-bound LLM request assembly, the client's control POSTs
#: waited on the SAME unbounded-read client, and Ctrl-C hung forever. With
#: this, the wait ends in a typed ``httpx.ReadTimeout`` that ``send``
#: turns into a non-delivery (``None``) the TUI can name.
#:
#: #6083 ⑵-a: NOT every control POST is bounded, though — one class
#: (:data:`~reyn.interfaces.transport.agui.protocol.LONG_RUNNING_
#: PAYLOAD_TYPES`, currently just ``attach_request``) has a server-side
#: handler that awaits an operation with genuinely unbounded duration
#: (a first-attach session load replaying its persisted WAL history).
#: Applying THIS constant to those reproduces the exact #6083 symptom one
#: level up — a slow-but-succeeding operation misread as a timed-out
#: failure. :func:`_read_timeout_for` is the one place that decision is
#: made; this constant itself is unchanged and still names the bounded
#: default correctly.
_CONTROL_TIMEOUT_S = _env_float("REYN_AGUI_CONTROL_TIMEOUT_S", 10.0)


def _read_timeout_for(ptype: "object", *, override: "float | None" = None) -> "float | None":
    """The read timeout :func:`post_control` should use for one payload
    ``type`` — ``None`` (unbounded, matching the SSE stream's own policy)
    for :data:`~reyn.interfaces.transport.agui.protocol.LONG_RUNNING_
    PAYLOAD_TYPES`; :data:`_CONTROL_TIMEOUT_S` (or ``override``, when a
    caller supplies one — the same seam tests already use to inject T)
    otherwise.

    Split out as its OWN pure function (testing.md: a test writes no
    duration; the DECISION is the thing to observe, not a live wait) — it
    takes no client, opens no socket, and its whole body is one membership
    check, so a test can assert its return value directly rather than
    proving "did not time out" by watching a clock.

    Reads the module-level ``LONG_RUNNING_PAYLOAD_TYPES`` name (a plain
    global lookup, not a fresh re-import each call) — a Python function
    body resolves a bare name against its OWN module's current globals at
    CALL time, so reassigning ``remote_client.LONG_RUNNING_PAYLOAD_TYPES``
    (this module's own bound copy of ``protocol.py``'s set — see the
    import at the top of this file) is visible here immediately, the same
    liveness the earlier per-call ``from ... import`` had, without paying
    for a fresh import on every control POST.
    """
    if ptype in LONG_RUNNING_PAYLOAD_TYPES:
        return None
    return _CONTROL_TIMEOUT_S if override is None else override


@dataclass
class ControlTimeoutCutStats:
    """#6241 ⑤: the process-lifetime witness that a BOUNDED control POST
    (a ``payload["type"]`` in :data:`~reyn.interfaces.transport.agui.
    protocol.BOUNDED_PAYLOAD_TYPES`) was actually CUT by
    :data:`_CONTROL_TIMEOUT_S` — the one failure mode no static check can
    see. #6083's own human trace into each branch's awaited callee (and
    #6244's AST-derived population of that trace) can only confirm a
    ``ptype`` does not await an UNBOUNDED operation; it cannot prove
    ``_CONTROL_TIMEOUT_S`` is enough headroom for the BOUNDED one it
    does await. A classification that is wrong in that second way is
    silent by construction (a timed-out POST reads exactly like a dead
    connection to everything downstream) unless something durably marks
    the moment it happens.

    Keyed by ``payload["type"]`` alone — unlike
    :class:`~reyn.interfaces.inline.textual_chat.app.PumpSwallowStats`'s
    ``(kind, exception type)`` pair, the exception type here is always
    ``httpx.ReadTimeout`` by construction (:meth:`record` is only ever
    called from that except-clause branch of :func:`post_control`), so a
    second axis would name nothing new. The key domain itself
    (``BOUNDED_PAYLOAD_TYPES``) is a small, FIXED frozenset declared in
    ``protocol.py`` — so unlike that sibling's open-ended ``(kind, exc
    type)`` domain, ``counts`` can never grow past that set's own size;
    it is bounded by the vocabulary, not merely by a dedup key.

    ``counts[ptype]`` is the TOTAL cuts observed for that type this
    process, always complete. :meth:`record`'s own return value is the
    SEPARATE bound the caller's audit-event honors: True only the FIRST
    time a given ``ptype`` is cut this process — a connection pinned on a
    slow network must not flood ``.reyn/events`` with one row per retry.
    """

    counts: "dict[str, int]" = field(default_factory=dict)

    def record(self, ptype: str) -> bool:
        """Record one timeout-cut occurrence for *ptype*. Returns True
        iff this exact ``ptype`` has never been recorded before on this
        instance — the caller's own signal to emit the bounded,
        first-occurrence-only audit-event. ``counts[ptype]`` still
        increments on a repeat."""
        first = ptype not in self.counts
        self.counts[ptype] = self.counts.get(ptype, 0) + 1
        return first


#: #6241 ⑤: process-lifetime default — mirrors ``PumpSwallowStats``'s own
#: "always constructed, never None, one per App instance" shape one level
#: up: the nearest equivalent of "one App instance" for a plain module of
#: async functions (no long-lived object ``post_control`` is a method of)
#: is "one process running ``reyn chat --connect``". A test that needs
#: isolation swaps this module attribute for a fresh instance via
#: ``monkeypatch.setattr`` rather than threading a new parameter through
#: ``post_control``'s already-widely-called signature.
_CONTROL_TIMEOUT_CUT_STATS = ControlTimeoutCutStats()


def _record_control_timeout_cut(ptype: str, read_timeout: "float | None") -> None:
    """#6241 ⑤: warn-once witness that ``ptype``'s BOUNDED classification
    just cut a real control POST — fires only from :func:`post_control`'s
    own ``httpx.ReadTimeout`` branch, and only when ``ptype`` is in
    :data:`~reyn.interfaces.transport.agui.protocol.BOUNDED_PAYLOAD_
    TYPES` (a payload in ``LONG_RUNNING_PAYLOAD_TYPES`` reads
    ``read=None`` and structurally cannot raise this).

    Fires only the FIRST time THIS ``ptype`` is cut this process
    (:meth:`ControlTimeoutCutStats.record` on the module-level
    :data:`_CONTROL_TIMEOUT_CUT_STATS` — bounded-by-key, the SAME shape
    ``pump_exception_swallowed`` established: a slow network cuts the
    SAME ptype on every retry, so a durable record per OCCURRENCE would
    flood ``.reyn/events``; the always-complete per-ptype count stays
    available via ``ControlTimeoutCutStats.counts``, readable from a
    test or a debugger — there is no operator-facing surface that reads
    it today (no status line, no log line, no command). The audit-event
    this function emits, landing in ``.reyn/events``, is the ONLY
    operator-facing side of this mechanism; ``counts`` itself does not
    reach one).

    Never carries the exception's own message or traceback (the SAME
    posture ``pump_exception_swallowed``'s own doc row states verbatim)
    — :func:`post_control`'s existing ``logger.warning`` call (unchanged)
    already covers the free-text half; this event carries only the
    structured facts a post-mortem reader queries by.
    """
    if not _CONTROL_TIMEOUT_CUT_STATS.record(ptype):
        return
    try:
        from reyn.core.events.events import emit_cli_event

        emit_cli_event(
            "control_post_bounded_timeout_cut",
            payload_type=ptype,
            timeout_seconds=read_timeout,
        )
    except Exception:  # noqa: BLE001 — diagnostic-only, must not break the POST path
        logger.exception(
            "remote_client: failed to emit control_post_bounded_timeout_cut "
            "for payload_type=%r (diagnostic-only, does not block the POST)",
            ptype,
        )


async def post_control(
    client, url: str, *, params: dict, payload: dict,
    timeout_s: "float | None" = None,
) -> "ControlOutcome":
    """POST one client→server control message with the CONTROL timeout
    policy (#5894 ①-1, #6083 ⑵-a) and return the TYPED outcome (#5907 ②):
    :class:`ControlOutcome` — ``delivered(payload)`` on a 2xx, ``refused``
    on ≥300 (the server's own reason), ``not_delivered`` when the request
    raised (the control read timeout, a connect error …).

    :func:`_read_timeout_for` decides the read timeout from ``payload``'s
    own ``type`` (bounded by default, unbounded for the derived long-
    running class); the ``client`` passed in keeps its OWN default
    (``read=None``, the SSE stream's) untouched — one client, two request
    kinds, at most two policies. A 2xx whose body is empty / not JSON is
    still delivered, with a truthy ``{"status": "ok"}`` payload, so every
    ``if accepted:`` caller keeps the old bool contract — the outcome
    itself is truthy iff delivered.

    ``timeout_s`` is a parameter so a test can supply T for a BOUNDED
    payload type — it is the subject there, never a wait the test sits
    out; it has no effect on a long-running type, which is always
    unbounded regardless of what a caller passes (there is no "T" to
    inject for those — see :func:`_read_timeout_for`).
    """
    import httpx

    read_timeout = _read_timeout_for(payload.get("type"), override=timeout_s)
    try:
        resp = await client.post(
            url, params=params, json=payload,
            timeout=httpx.Timeout(read_timeout, connect=10.0),
        )
    except Exception as exc:  # noqa: BLE001 — a transport error is a non-delivery
        ptype = payload.get("type")
        # #6241 ⑤: a `ReadTimeout` on a `ptype` classified BOUNDED is the
        # ONE failure mode #6083/#6244's static trace cannot see (that
        # classification is a correctness claim about the SERVER side's
        # awaited callee, not a claim `_CONTROL_TIMEOUT_S` is enough
        # headroom) — durably mark it rather than let it read identically
        # to a dead connection. `LONG_RUNNING_PAYLOAD_TYPES` reads
        # `read=None` and cannot raise this, so the membership check is
        # what keeps this from firing for that class.
        if isinstance(exc, httpx.ReadTimeout) and ptype in BOUNDED_PAYLOAD_TYPES:
            _record_control_timeout_cut(ptype, read_timeout)
        logger.warning("remote send failed for %r: %s", ptype, type(exc).__name__)
        return ControlOutcome.not_delivered(type(exc).__name__, read_timeout)
    if resp.status_code >= 300:
        reason: "str | None"
        try:
            body = resp.json()
            reason = str(body.get("detail") or body.get("error") or body) if isinstance(body, dict) else str(body)
        except Exception:  # noqa: BLE001 — a non-JSON refusal still has a status
            # SILENT-EXCEPT-RETURN-OK: reported-elsewhere -- the refusal is already reported via ControlOutcome.refused(status_code, ...) below regardless of whether this best-effort reason parses
            reason = (resp.text or "").strip() or None
        return ControlOutcome.refused(resp.status_code, reason)
    try:
        return ControlOutcome.delivered(resp.json())
    except Exception:  # noqa: BLE001 — an empty/non-JSON 2xx body is still an accept
        return ControlOutcome.delivered({"status": "ok"})


def _heartbeat_due(last_send: float, now: float, interval: float = _HEARTBEAT_INTERVAL) -> bool:
    """Piggyback decision: True iff no client→server POST (real traffic or a
    prior heartbeat) landed within the last ``interval`` seconds, so the
    dedicated heartbeat ping is not redundant. A pure function of the last-send
    timestamp so the policy is unit-testable without a live event loop / socket.
    """
    return (now - last_send) >= interval


def connect_failure_message(status: int, agent_name: str, base_url: str) -> str:
    """Map an SSE-connect HTTP status to an actionable, cause-naming message.

    A bare "server refused the connection (404)" hides the real cause: a 404 on
    ``reyn chat --connect`` almost always means the *agent* wasn't found (the
    client defaults to agent ``"default"`` when none is passed), and a 401 is an
    auth-token problem. Name the cause and give the next step for each.
    """
    if status == 404:
        return (
            f"Error: agent '{agent_name}' not found on the server (404). "
            f"List available agents: curl {base_url}/a2a/agents . "
            "(If you didn't pass an agent name, it defaults to 'default'.)"
        )
    if status == 401:
        return (
            "Error: authentication failed (401) — pass --token <secret> "
            "(the token `reyn web` prints on launch), or set REYN_WEB_AUTH_TOKEN."
        )
    return f"Error: server refused the connection ({status}) at {base_url}."


async def run_remote_repl(
    *,
    base_url: str,
    agent_name: str,
    token: "str | None" = None,
    renderer,
    config=None,
) -> None:
    """Attach to a remote server's session over AG-UI SSE and run the REPL.

    ``base_url`` is the server root (e.g. ``http://127.0.0.1:8080``). The SSE
    stream is ``<base_url>/agui/chat/<agent>/events``; client→server messages POST
    to ``<base_url>/agui/chat/<agent>``. ``token`` is the P0 bearer secret (from
    ``--token`` or ``REYN_WEB_AUTH_TOKEN``); a UDS / loopback server may need none.
    """
    try:
        import httpx
    except ImportError as e:
        from reyn.interfaces.install_guard import missing_dep_message

        print(missing_dep_message(e, "httpx", "web"), file=sys.stderr)
        sys.exit(1)

    from reyn.interfaces.transport.agui.client import AgUiTransport

    from .client_driver import run_chat_client
    from .read_model import RemoteReadModel

    base_url = base_url.rstrip("/")
    events_url = f"{base_url}/agui/chat/{agent_name}/events"
    submit_url = f"{base_url}/agui/chat/{agent_name}"
    connection_id = uuid.uuid4().hex
    params: dict = {"connection_id": connection_id}
    if token:
        params["token"] = token

    from reyn._network import build_async_http_client

    async with build_async_http_client(
        timeout=httpx.Timeout(None, connect=10.0), egress="remote_repl"
    ) as client:
        # Monotonic timestamp of the last client→server POST of ANY kind (a real
        # turn/answer/cancel, or a prior heartbeat). The heartbeat loop piggybacks
        # on real traffic: if one already crossed the wire within the interval
        # window, the dedicated ping is redundant and is skipped — real activity
        # already refreshed the server-side liveness timestamp (``agui_submit``
        # refreshes it for every accepted POST, not just ``type: heartbeat``).
        last_send = [0.0]

        async def send(payload: dict) -> "ControlOutcome":
            """POST one client→server message; the parsed JSON response body on
            a 2xx accept (always a truthy dict, even if the body itself parsed
            empty — see below), ``None`` if the server rejected it (403/409/…)
            or the POST itself failed. A rejected HITL answer therefore still
            reads as falsy exactly like the old bool contract (``if accepted:``
            / ``bool(accepted)`` at every existing call site are unaffected by
            this widened return type).
            ``submit_user_text`` (#3287) additionally reads ``resp["msg_id"]``
            here — the server's echo of the SAME correlation id its broadcast
            ``user_submitted`` audit-event carries (#3300 P2a). This id is
            NOT what closes remote own-echo suppression, though (#3309 F2):
            it only becomes visible once this await returns, and the server
            may already have pushed the SSE broadcast for the same submission
            over the OTHER connection (the events stream) in the interim — a
            cross-channel race a POST-ack-timed id cannot avoid. Remote
            suppression instead matches on `own_connection_id`
            (`run_chat_client`/`run_output_loop`), known up-front and
            independent of this response entirely; `msg_id` here stays
            load-bearing for a DIFFERENT reason — #3300 Y-client (cancel-by-id)
            needs the client to learn its own message id, per
            ``session.py``'s ``submit_user_text`` docstring.
            """
            last_send[0] = time.monotonic()
            # #5894 ①-1: the control-POST policy lives in ``post_control``
            # (its own bounded read timeout); ``client``'s default
            # ``read=None`` stays for the SSE stream below, which is the
            # request that must never time out.
            return await post_control(client, submit_url, params=params, payload=payload)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                if _heartbeat_due(last_send[0], time.monotonic()):
                    await send({"type": "heartbeat"})

        try:
            async with client.stream("GET", events_url, params=params) as resp:
                if resp.status_code >= 400:
                    print(
                        connect_failure_message(
                            resp.status_code, agent_name, base_url
                        ),
                        file=sys.stderr,
                    )
                    sys.exit(1)

                async def sse_lines() -> AsyncIterator[str]:
                    async for line in resp.aiter_lines():
                        yield line

                # #5139: ``agent_name`` seeds this connection's OWN idea of
                # "which (agent, sid) is the FIRST backlog batch for" — the
                # URL this connection was opened against; see
                # ``AgUiTransport.__init__``'s own comment for why the
                # very first connect needs this seeded rather than
                # learning it off a ``session_attached`` announce (that
                # event only ever fires for a later mid-stream switch).
                transport = AgUiTransport(sse_lines(), send, agent_name=agent_name)
                # ADR-0039 P3: the REMOTE half of the unified chat client. It
                # constructs the transport-specific pair (an ``AgUiTransport`` +
                # a ``RemoteReadModel`` reading the server's STATE_* status view
                # over the wire) and hands off to the SAME shared driver the local
                # path uses — so an interactive TTY remote attach renders the inline
                # CUI (with the frame-available status bar), not the plain console.
                read_model = RemoteReadModel(transport)
                hb = asyncio.create_task(heartbeat())
                try:
                    await run_chat_client(
                        transport=transport,
                        renderer=renderer,
                        read_model=read_model,
                        agent_name=agent_name,
                        is_tty=sys.stdin.isatty(),
                        config=config,
                        # #3287/#3309 F2: THIS client's own connection_id
                        # (minted above, BEFORE any submit, and stamped on
                        # every POST) — the server echoes it back into every
                        # user_submitted broadcast's meta.auth_connection_id,
                        # so run_output_loop can recognise "this is MY OWN
                        # remote submission" by identity, with no dependency
                        # on the POST-ack racing the SSE broadcast.
                        own_connection_id=connection_id,
                    )
                finally:
                    hb.cancel()
                    await asyncio.gather(hb, return_exceptions=True)
        except httpx.ConnectError:
            print(
                f"Error: could not connect to {base_url}. Is `reyn web` running there?",
                file=sys.stderr,
            )
            sys.exit(1)


__all__ = ["run_remote_repl"]
