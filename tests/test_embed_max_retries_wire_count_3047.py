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

**Blast-radius check (architect co-vet on #3054): does chat's wire count
change too?** The fix also sets `litellm.DEFAULT_MAX_RETRIES = 0` — a
process-wide global, not a per-call kwarg, since a falsy ``max_retries=0``
kwarg alone revives the same ``x or DEFAULT`` trap. That global is read in
exactly one place in the pinned litellm: `OpenAIChatCompletion.embedding()`'s
``max_retries = max_retries or litellm.DEFAULT_MAX_RETRIES``
(`llms/openai/openai.py`). The sibling `OpenAIChatCompletion.completion()`
method — chat's call path — never reads `litellm.DEFAULT_MAX_RETRIES` at
all: it does ``inference_params.pop("max_retries", 2)``, and reyn's chat path
(`llm.py`) always passes an explicit non-`None` `num_retries` into
`litellm.acompletion`, which `litellm.main.py` maps onto `max_retries` in
`optional_params` BEFORE `completion()` ever pops it — so the fallback
literal `2` there is dead code for every reyn chat call, same as the global
mutation is. `test_chat_acompletion_wire_count_unaffected_by_embed_global_mutation`
below drives ONE real `litellm.acompletion` call shaped like reyn's chat call
site (explicit `num_retries`, `stream=False`) against the same
request-counting server, once with the global at its untouched litellm
default (2) and once forced to 0 (the fix's post-import state), and asserts
the wire count is identical — closing the "discussed structurally, not
driven" gap the architect flagged.

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

import litellm
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


@pytest.mark.asyncio
async def test_chat_acompletion_wire_count_unaffected_by_embed_global_mutation(
    counting_server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tier 2: the embed fix's `litellm.DEFAULT_MAX_RETRIES = 0` global mutation
    does not change chat's wire count (architect co-vet on #3054, driven not
    inferred).

    Drives ONE `litellm.acompletion(...)` call — shaped like reyn's real chat
    call site in `llm.py` (explicit `num_retries=`, `stream=False`, routed at
    a real local HTTP server via `api_base`, no monkeypatching of litellm or
    of the call itself) — against the SAME request-counting server the embed
    tests above use, once with `litellm.DEFAULT_MAX_RETRIES` at the untouched
    litellm default (2, i.e. the state BEFORE any embed provider has run) and
    once forced to 0 (the state AFTER `_aembedding_bounded` has run, since the
    mutation is process-wide and permanent). If the two wire counts differ,
    the embed fix's global has reached into chat's retry count and the fix
    needs a different (non-global) shape.
    """
    import os

    api_base = os.environ["LITELLM_API_BASE"]  # set by the counting_server fixture
    counts: dict[int, int] = {}
    for default_max_retries in (2, 0):
        monkeypatch.setattr(litellm, "DEFAULT_MAX_RETRIES", default_max_retries)
        counting_server.request_count = 0
        with pytest.raises(litellm.exceptions.InternalServerError):
            await litellm.acompletion(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
                num_retries=3,  # same shape as llm.py's call_kwargs["num_retries"]
                timeout=5.0,
                stream=False,
                api_base=api_base,
            )
        counts[default_max_retries] = counting_server.request_count

    assert counts[2] == counts[0], (
        f"chat's wire count changed with litellm.DEFAULT_MAX_RETRIES "
        f"(2 -> {counts[2]} requests, 0 -> {counts[0]} requests) — the embed "
        f"fix's global mutation is NOT scoped to embedding as intended"
    )
    assert counts[2] > 0, "sanity: the stand-in server must have been hit at all"
