"""Tier 2: safe.http blocks redirect-to-internal — the #1956 SSRF bypass.

Real local redirect server + real ``safe.http`` (its allowlist is the seam, set
via ``_set_permission_context``) — no mocks. Pre-fix the allowlisted host's
redirect to a loopback secret was followed and the body returned; post-fix it
raises (``_check_host`` adds L2, and the custom redirect handler re-gates each
hop). The behavioural test asserts on ``PermissionError`` (``SSRFBlocked``'s base)
so it goes CLEAN RED on pre-fix without importing the new module; the handler
units pin per-hop L1 + L2 re-validation. Tier line first.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import reyn.api.safe.http as sh

SECRET = b"LOOPBACK-SECRET-1956"
_port = 0


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{_port}/secret")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(SECRET)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    global _port
    srv = HTTPServer(("127.0.0.1", 0), _H)
    _port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield _port
    srv.shutdown()


def test_redirect_chain_to_loopback_blocked(server):
    """Tier 2: an allowlisted host whose redirect reaches a loopback secret is
    BLOCKED post-fix (pre-fix the body was returned — the reproduced bypass)."""
    sh._set_permission_context(http_hosts=["127.0.0.1"])
    with pytest.raises(PermissionError):
        sh.get(f"http://127.0.0.1:{server}/redirect")


def test_redirect_handler_blocks_non_allowlisted_host(monkeypatch):
    """Tier 2: the redirect handler re-validates the new host — a redirect to a
    PUBLIC host NOT in the allowlist is blocked (L1 per-hop re-validation)."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    sh._set_permission_context(http_hosts=["allowed.example"])
    handler = sh._SSRFSafeRedirectHandler()
    with pytest.raises(PermissionError):
        # 1.1.1.1 is public (L2 passes) but ∉ allowlist → L1 blocks the hop.
        handler.redirect_request(None, None, 302, "Found", {}, "http://1.1.1.1/x")


def test_redirect_handler_blocks_metadata_even_if_allowlisted(monkeypatch):
    """Tier 2: a redirect to the metadata endpoint is blocked even when that host
    is allowlisted (L2 IP-deny on the redirect hop, independent of L1)."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    sh._set_permission_context(http_hosts=["169.254.169.254"])
    handler = sh._SSRFSafeRedirectHandler()
    with pytest.raises(PermissionError):
        handler.redirect_request(
            None, None, 302, "Found", {}, "http://169.254.169.254/latest/meta-data/"
        )


def test_allowlisted_public_literal_passes(monkeypatch):
    """Tier 2: (regression) an allowlisted PUBLIC host passes _check_host — the
    guard does not over-block legitimate public fetches."""
    monkeypatch.delenv("REYN_FETCH_ALLOW_PRIVATE_IPS", raising=False)
    sh._set_permission_context(http_hosts=["8.8.8.8"])
    sh._check_host("http://8.8.8.8/")  # no raise (public literal, no DNS)
