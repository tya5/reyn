"""Tier 2: web_fetch blocks redirect-to-internal — the #1956 SSRF bypass.

Real local redirect server + real httpx via ``handle_web_fetch``
(``permission_resolver=None`` so L1 is skipped → this exercises L2, the IP-deny).
Pre-fix the redirect to a loopback secret was followed and the body returned
(``status='ok'``); post-fix the loopback target is denied (``status='blocked'``).
Asserts on the status string so it goes CLEAN RED on pre-fix without importing the
new module. Tier line first.
"""
from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from reyn.config.media import WebConfig, WebFetchConfig
from reyn.core.op_runtime.web import handle_web_fetch
from reyn.schemas.models import WebFetchIROp

SECRET = "LOOPBACK-SECRET-1956"
_port = 0


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{_port}/secret")
            self.end_headers()
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(SECRET.encode())

    def log_message(self, *a):  # silence
        pass


def _make_ctx(allow_private: bool = False) -> Any:
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    class _Ev:
        def emit(self, *a: object, **k: object) -> None:
            pass

    return OpContext(
        workspace=type("W", (), {})(),  # type: ignore[arg-type]
        events=_Ev(),                    # type: ignore[arg-type]
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        web_config=WebConfig(fetch=WebFetchConfig(allow_private_ips=allow_private)),
    )


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    global _port
    srv = HTTPServer(("127.0.0.1", 0), _H)
    _port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield _port
    srv.shutdown()


def _fetch(url: str, ctx: Any) -> dict:
    op = WebFetchIROp(kind="web_fetch", url=url)
    return asyncio.run(handle_web_fetch(op=op, ctx=ctx, caller="control_ir"))


def test_redirect_to_loopback_blocked(server):
    """Tier 2: a redirect chain reaching a loopback secret is BLOCKED post-fix
    (pre-fix returned status='ok' with the secret — the reproduced bypass)."""
    result = _fetch(f"http://127.0.0.1:{server}/redirect", _make_ctx())
    assert result["status"] == "blocked", result
    assert SECRET not in result.get("content", "")


def test_direct_loopback_blocked(server):
    """Tier 2: a direct loopback fetch is blocked by the L2 IP-deny."""
    result = _fetch(f"http://127.0.0.1:{server}/secret", _make_ctx())
    assert result["status"] == "blocked", result


def test_metadata_url_blocked():
    """Tier 2: a direct fetch to the cloud metadata endpoint is blocked (no
    server needed — 169.254.169.254 is a literal link-local IP)."""
    result = _fetch("http://169.254.169.254/latest/meta-data/", _make_ctx())
    assert result["status"] == "blocked", result
