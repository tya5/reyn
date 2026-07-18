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
from typing import AsyncIterator

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

        async def send(payload: dict) -> bool:
            """POST one client→server message; True iff the server accepted it
            (2xx). A rejected HITL answer (403/409) returns False so the client
            falls back to an ordinary turn instead of silently dropping input."""
            last_send[0] = time.monotonic()
            try:
                resp = await client.post(submit_url, params=params, json=payload)
            except Exception:  # noqa: BLE001 — a transport error is a non-delivery
                logger.warning("remote send failed for %r", payload.get("type"))
                return False
            return resp.status_code < 300

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

                transport = AgUiTransport(sse_lines(), send)
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
