"""Tier 2: one `embed()` call puts `max_retries: 3` requests on the wire, not 9 (#3047).

#3043 measured (cost-probe-coder, driven against a REAL localhost stand-in
server, never a real provider) that `litellm.aembedding(...)` was called
without `max_retries=`, so litellm's own
`max_retries = max_retries or litellm.DEFAULT_MAX_RETRIES` turned the missing
kwarg into `2`, which litellm hands to the OpenAI SDK client as
`AsyncOpenAI(max_retries=2)`. That client retries INTERNALLY, underneath
reyn's own `_embed_batch_with_retry` loop: 1 initial + 2 SDK retries = 3 HTTP
requests per reyn attempt, times reyn's configured `max_retries: 3` = **9
requests delivered on the wire for one `embed()` call** — invisible to the
`attempt %d/%d` log line, which only ever counts to 3.

This is the COST lever (not #3043's LATENCY lever — `embedding.timeout`
bounds how long a call WAITS, it does not reduce how many requests are SENT;
see `_embed_batch_with_retry`'s docstring). The fix passes `max_retries=0`
into every `litellm.aembedding(...)` call (`_aembedding_bounded`), making
reyn's own retry loop the ONLY retry layer.

**Why this needs a real request-counting server, not a mock.** A test that
patches `litellm.aembedding` never exercises the OpenAI SDK client that does
the amplifying — it would pass identically whether the fix is wired or not.
So this test is a REAL `LiteLLMEmbeddingProvider` routed (via
`LITELLM_API_BASE`, the provider's own production proxy knob — no
monkeypatching of the provider or of litellm) at a REAL local HTTP server
that answers every request with a retryable 500 instantly, and the server
counts requests it actually received. Pre-fix this counts 9; post-fix, 3 —
driven, not inferred. No real provider is contacted and no cost is spent
(local socket only, per #3047's brief).
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reyn.data.embedding.litellm_provider import LiteLLMEmbeddingProvider

_MODEL = "openai/text-embedding-3-small"


class _CountingRetryableErrorHandler(BaseHTTPRequestHandler):
    """Answers every POST with a retryable 500 instantly — no stall, no delay.

    A 500 is treated as transient by both the OpenAI SDK client's own retry
    logic AND reyn's `_embed_batch_with_retry` `except Exception` catch-all,
    so every request in both retry layers actually fires (nothing short-
    circuits on a shape mismatch that would undercount the wire).
    """

    request_count = 0
    _lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # drain the full body — a real delivered request
        with _CountingRetryableErrorHandler._lock:
            _CountingRetryableErrorHandler.request_count += 1
        body = b'{"error": {"message": "stand-in 500", "type": "server_error"}}'
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def counting_server(monkeypatch: pytest.MonkeyPatch):
    _CountingRetryableErrorHandler.request_count = 0
    srv = HTTPServer(("127.0.0.1", 0), _CountingRetryableErrorHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LITELLM_API_BASE", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-never-used")
    try:
        yield _CountingRetryableErrorHandler
    finally:
        srv.shutdown()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_one_embed_call_delivers_exactly_reyn_max_retries_requests(
    counting_server,
) -> None:
    """Tier 2: `max_retries: 3` puts exactly 3 requests on the wire, not 9.

    Pre-fix (no `max_retries=` passed to `litellm.aembedding`) this counted 9 —
    3 reyn attempts x 3 OpenAI-SDK-internal retries each, all against the SAME
    real local server counting real delivered requests.
    """
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 5.0,
            "max_retries": 3,
            "retry_backoff": 1.0,  # 1.0^attempt == 1s sleeps; keeps the test quick
            "classes": {"standard": _MODEL},
        }
    )

    with pytest.raises(RuntimeError, match="Embedding failed after 3 attempts"):
        await provider.embed(["hello"], "standard")

    assert counting_server.request_count == 3, (
        f"expected 3 requests on the wire (reyn's own retry loop, SDK retry "
        f"disabled), got {counting_server.request_count}"
    )


@pytest.mark.asyncio
async def test_single_attempt_no_reyn_retry_delivers_exactly_one_request(
    counting_server,
) -> None:
    """Tier 2: with reyn's own retry loop disabled (`max_retries: 1`), exactly
    ONE request reaches the wire — pre-fix the SDK's own hidden retry would
    still have delivered 3 for this single reyn attempt."""
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 5.0,
            "max_retries": 1,
            "classes": {"standard": _MODEL},
        }
    )

    with pytest.raises(RuntimeError, match="Embedding failed after 1 attempts"):
        await provider.embed(["hello"], "standard")

    assert counting_server.request_count == 1
