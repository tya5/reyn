"""Tier 2: #4364 C-4 — ``reyn doctor``'s model/api_base reachability check.

architect's motivating case: a configured model name (``openai/gpt-5.6-luna``)
that the LiteLLM proxy expected bare (``gpt-5.6-luna``) — no error until the
first real chat turn.

**Question REPLACED per this session's ruling**, not implemented as first
proposed: a real litellm completion probe would make ``reyn doctor`` itself
charge the operator for inference — exactly what the cross-cutting
cost/budget band exists to keep OS-internal diagnostics from doing. Replaced
with a 0-token ``GET {api_base}/v1/models`` — reachability from the HTTP
response itself (any response, including 401/403, proves reachability), and
(when the response lists models) whether each declared model name's BARE
form is accepted.

Real HTTP server (stdlib ``http.server`` on a real loopback socket, a real
collaborator — no mocks) — the exact request `_print_model_reachability`
makes is driven end-to-end, including the "no Authorization header sent"
claim (asserted by the handler itself).
"""
from __future__ import annotations

import http.server
import json
import threading

from reyn.interfaces.cli.commands.doctor import _print_model_reachability


class _ModelsHandler(http.server.BaseHTTPRequestHandler):
    """Serves a fixed ``/v1/models`` listing and records every request it
    received (path + headers) so tests can assert on what was actually
    sent — including that no ``Authorization`` header ever arrives."""

    model_ids: "list[str]" = []
    received_requests: "list[dict]" = []

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's own name
        type(self).received_requests.append(
            {"path": self.path, "headers": dict(self.headers)},
        )
        if self.path == "/v1/models":
            body = json.dumps({"data": [{"id": m} for m in type(self).model_ids]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args: object) -> None:  # silence stdlib access log
        pass


def _run_server(model_ids: "list[str]") -> "tuple[http.server.HTTPServer, str]":
    handler = type("_Handler", (_ModelsHandler,), {"model_ids": model_ids, "received_requests": []})
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    return server, f"http://127.0.0.1:{port}"


class _LLM:
    def __init__(self, *, api_base: str, models: dict) -> None:
        self.api_base = api_base
        self.models = models


class _Config:
    def __init__(self, llm: _LLM) -> None:
        self.llm = llm


def test_no_api_base_declared_prints_not_checked(capsys) -> None:
    """Tier 2: accept-side — no ``llm.api_base`` declared prints the D-3
    "not checked" line, never attempts a request."""
    config = _Config(_LLM(api_base="", models={"standard": "openai/gpt-4o"}))

    _print_model_reachability(config)
    out = capsys.readouterr().out

    assert "? not checked — no llm.api_base declared" in out


def test_reachable_proxy_with_accepted_model_name_reports_both_ok(capsys) -> None:
    """Tier 2: the core witness — a real reachable proxy whose model list
    contains the declared model's BARE name reports both reachability and
    acceptance as ✓."""
    server, base_url = _run_server(["gpt-4o"])
    try:
        config = _Config(_LLM(api_base=base_url, models={"light": "openai/gpt-4o"}))

        _print_model_reachability(config)
        out = capsys.readouterr().out

        assert f"✓ {base_url}: reachable (HTTP 200)" in out
        assert "✓ light ('gpt-4o'): accepted" in out
    finally:
        server.shutdown()


def test_model_name_mismatch_is_flagged_bare_vs_prefixed(capsys) -> None:
    """Tier 2: architect's own repro shape — a declared model name whose
    BARE form is NOT in the proxy's list is flagged, naming the bare-vs-
    prefixed hint, not silently treated as accepted."""
    server, base_url = _run_server(["gpt-4o"])  # does NOT include gpt-5.6-luna
    try:
        config = _Config(_LLM(api_base=base_url, models={"standard": "openai/gpt-5.6-luna"}))

        _print_model_reachability(config)
        out = capsys.readouterr().out

        assert f"✓ {base_url}: reachable" in out
        assert "✗ standard ('gpt-5.6-luna'): NOT in the proxy's model list" in out
        assert "bare vs 'provider/name'" in out
    finally:
        server.shutdown()


def test_unreachable_api_base_reports_failure_not_a_crash(capsys) -> None:
    """Tier 2: a real connection failure (nothing listening) is reported
    as ✗ unreachable, not an unhandled exception (D-2: doctor never
    crashes on an unreachable endpoint)."""
    # Port 1 is a real, universally-unassignable low port — a real
    # connection attempt that always refuses on a loopback host.
    config = _Config(_LLM(api_base="http://127.0.0.1:1", models={"standard": "openai/gpt-4o"}))

    _print_model_reachability(config)
    out = capsys.readouterr().out

    assert "✗ http://127.0.0.1:1: unreachable" in out


def test_no_authorization_header_is_ever_sent(capsys) -> None:
    """Tier 2: owner's standing instruction (litellm-boundary convention)
    — the request never carries an Authorization header, asserted against
    what the REAL server actually received, not what the client claims to
    send."""
    server, base_url = _run_server(["gpt-4o"])
    handler_cls = server.RequestHandlerClass
    try:
        config = _Config(_LLM(api_base=base_url, models={}))

        _print_model_reachability(config)
        capsys.readouterr()

        assert handler_cls.received_requests, "expected the server to receive a request"
        for req in handler_cls.received_requests:
            assert "authorization" not in {k.lower() for k in req["headers"]}
    finally:
        server.shutdown()


def test_non_model_list_response_reports_reachable_but_names_not_checked(capsys) -> None:
    """Tier 2: a 200 response with no model list still reports reachable
    (that much is proven) but discloses the name-form check couldn't
    run, rather than silently skipping it or claiming acceptance."""
    server, base_url = _run_server([])  # empty data: [] — a real, valid 200
    try:
        config = _Config(_LLM(api_base=base_url, models={"standard": "openai/gpt-4o"}))

        _print_model_reachability(config)
        out = capsys.readouterr().out

        assert f"✓ {base_url}: reachable (HTTP 200)" in out
        # Empty list IS a model list (just empty) -> the declared model is
        # correctly reported as not-in-list, not "not checked".
        assert "✗ standard ('gpt-4o'): NOT in the proxy's model list" in out
    finally:
        server.shutdown()
