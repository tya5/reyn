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
import sys
import uuid
from typing import AsyncIterator

logger = logging.getLogger(__name__)

# Client→server heartbeat cadence (seconds). Comfortably under the server's
# default liveness timeout so a live client is never swept as dead.
_HEARTBEAT_INTERVAL = 10.0


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

    from prompt_toolkit import PromptSession

    from reyn.interfaces.transport.agui.client import AgUiTransport

    from .stream_client import run_input_loop, run_output_loop

    base_url = base_url.rstrip("/")
    events_url = f"{base_url}/agui/chat/{agent_name}/events"
    submit_url = f"{base_url}/agui/chat/{agent_name}"
    connection_id = uuid.uuid4().hex
    params: dict = {"connection_id": connection_id}
    if token:
        params["token"] = token

    async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10.0)) as client:

        async def send(payload: dict) -> bool:
            """POST one client→server message; True iff the server accepted it
            (2xx). A rejected HITL answer (403/409) returns False so the client
            falls back to an ordinary turn instead of silently dropping input."""
            try:
                resp = await client.post(submit_url, params=params, json=payload)
            except Exception:  # noqa: BLE001 — a transport error is a non-delivery
                logger.warning("remote send failed for %r", payload.get("type"))
                return False
            return resp.status_code < 300

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
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
                renderer.banner(agent_name)
                reply_seen: asyncio.Event = asyncio.Event()
                reply_seen.set()
                prompt_session: PromptSession[str] = PromptSession()

                inputs = asyncio.create_task(
                    run_input_loop(transport, prompt_session, renderer, reply_seen)
                )
                outputs = asyncio.create_task(
                    run_output_loop(transport, renderer, reply_seen)
                )
                hb = asyncio.create_task(heartbeat())
                try:
                    await asyncio.wait(
                        {inputs, outputs}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for t in (inputs, outputs, hb):
                        t.cancel()
                    await asyncio.gather(inputs, outputs, hb, return_exceptions=True)
        except httpx.ConnectError:
            print(
                f"Error: could not connect to {base_url}. Is `reyn web` running there?",
                file=sys.stderr,
            )
            sys.exit(1)


__all__ = ["run_remote_repl"]
