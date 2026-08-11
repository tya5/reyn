"""MCP OAuth browser-authorization flow — #4282 (fastmcp retired for the
client's OAuth path in favour of the official ``mcp`` SDK's
``mcp.client.auth.OAuthClientProvider`` directly).

``OAuthClientProvider`` needs the caller to supply TWO callbacks
(``redirect_handler``/``callback_handler``) implementing the actual
browser-based Authorization Code Grant round trip — fastmcp's own ``OAuth``
class did this internally (``uvicorn.Server`` + a Starlette app in
``fastmcp.client.oauth_callback``). This module is reyn's OWN equivalent,
built from starlette/uvicorn directly — **both already core reyn
dependencies** (see ``pyproject.toml``), so this is zero new dependency
surface, not a new one. fastmcp's own callback-server module is
deliberately NOT imported here: importing it would keep a live dependency
on fastmcp exactly where #4282's whole point is to drop it.

Lifecycle (live-verified against a real bound localhost listener + a real
httpx GET before this was written, not assumed from reading uvicorn's API
alone): a ``uvicorn.Server`` is run via ``await server.serve()`` inside an
``anyio.create_task_group()`` in the SAME task/call tree as the rest of the
OAuth flow (mirrors fastmcp's own approach) — poll ``server.started`` until
the listener is actually up (uvicorn does not expose an awaitable "ready"
signal), then wait for the single ``/callback`` request (or the configured
timeout) via an ``anyio.Event``, then ``server.should_exit = True`` to stop
serving and let the task group exit cleanly.
"""
from __future__ import annotations

import logging
import socket
import webbrowser
from typing import Any

logger = logging.getLogger(__name__)


class OAuthCallbackTimeout(Exception):
    """Raised when no ``/callback`` request arrived within ``timeout`` seconds."""


def find_available_port(host: str = "127.0.0.1") -> int:
    """Return an ephemeral port currently free on ``host``.

    Same bind-port-0-then-release approach fastmcp's own
    ``find_available_port`` uses — there is a narrow TOCTOU window between
    releasing the socket here and the callback server re-binding it, but
    it is the standard, unavoidable approach for "let the OS pick a free
    port" without holding the socket open across the two separate libraries
    (this module's port-finder vs. uvicorn's own bind) involved.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


async def redirect_handler(authorization_url: str) -> None:
    """``OAuthClientProvider``'s ``redirect_handler`` contract: open the
    authorization URL in the operator's browser. No pre-flight validity
    check (fastmcp's own does one — a GET before opening, to catch a bad
    ``client_id`` early) — deliberately out of scope here: this module's
    job is opening the browser, not validating the server's response
    shape; the browser itself surfaces a bad-client error to the operator
    directly, same as it would for a bad password or any other login
    failure."""
    logger.info("MCP OAuth authorization URL: %s", authorization_url)
    webbrowser.open(authorization_url)


async def run_callback_server(
    *, host: str, port: int, timeout: float
) -> tuple[str, str | None]:
    """``OAuthClientProvider``'s ``callback_handler`` contract: run a
    localhost HTTP server on ``host``:``port`` until the browser redirects
    back to ``/callback?code=...&state=...`` (or ``?error=...``), then
    return ``(code, state)``.

    Raises :class:`OAuthCallbackTimeout` if no callback request arrives
    within ``timeout`` seconds, or the underlying error (e.g. a
    ``RuntimeError`` built from an ``?error=`` query param the
    authorization server sent) on an explicit failure response.
    """
    import anyio
    import uvicorn
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    result: dict[str, Any] = {}
    received = anyio.Event()

    async def _callback(request: Any) -> Any:
        params = request.query_params
        if "error" in params:
            result["error"] = RuntimeError(
                f"MCP OAuth authorization failed: {params.get('error')} "
                f"({params.get('error_description', 'no description')})"
            )
            received.set()
            return PlainTextResponse(
                "Authorization failed — you may close this window.", status_code=400,
            )
        result["code"] = params.get("code")
        result["state"] = params.get("state")
        received.set()
        return PlainTextResponse("Authorization complete — you may close this window.")

    app = Starlette(routes=[Route("/callback", _callback, methods=["GET"])])
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve)
        try:
            while not server.started:
                await anyio.sleep(0.01)
            try:
                with anyio.fail_after(timeout):
                    await received.wait()
            except TimeoutError as exc:
                raise OAuthCallbackTimeout(
                    f"MCP OAuth callback timed out after {timeout}s — no browser "
                    "redirect arrived at the local callback server."
                ) from exc
        finally:
            server.should_exit = True

    if "error" in result:
        raise result["error"]
    return result["code"], result.get("state")
