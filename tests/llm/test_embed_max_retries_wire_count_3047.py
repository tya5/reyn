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

**Blast-radius check (architect co-vet on #3054, re-verified #5918): does
chat's wire count change too?** The fix also sets
`litellm.DEFAULT_MAX_RETRIES = 0` — a process-wide global, not a per-call
kwarg, since a falsy ``max_retries=0`` kwarg alone revives the same ``x or
DEFAULT`` trap. That global is read in exactly one place in the pinned
litellm (1.100.0, re-verified directly — a repo-wide grep for the `or
litellm.DEFAULT_MAX_RETRIES` / `or DEFAULT_MAX_RETRIES` shape inside the
installed package returns exactly this one hit):
`OpenAIChatCompletion.embedding()`'s
``max_retries = max_retries or litellm.DEFAULT_MAX_RETRIES``
(`llms/openai/openai.py`). The sibling `OpenAIChatCompletion.completion()`
method — chat's call path — never reads `litellm.DEFAULT_MAX_RETRIES` at
all: it does ``inference_params.pop("max_retries", 2)``, a LITERAL fallback,
regardless of what reyn's own chat call site passes or omits (#5918 found
an EARLIER version of this paragraph's "reyn's chat path always passes an
explicit num_retries" claim was itself false for reyn's default,
unconfigured install — `config/chat.py`'s `llm_max_retries` field defaults
to `None`, meaning NOT PASSED, by #5793's own deliberate design; the reason
chat is safe from this global is litellm's own code shape at the site it
calls, not anything about how reyn calls it).

`test_embedding_max_retries_reflects_embed_global_mutation` below (#5918,
architect ruling — the earlier version of this test drove 2 real-HTTP loops
each bounded by a 5-second wall-clock budget and compared wire COUNTS, a
duration-dependent assertion under parallel CI load, CLAUDE.md's "a test
writes no duration" rule; the earlier sibling test for chat,
`test_chat_max_retries_unaffected_by_embed_global_mutation`, was DELETED —
#5918 again — because pinning `OpenAIChatCompletion._get_openai_client` as
"the chokepoint" turned out to have a SECOND real litellm call path
(`main.py`'s `EXPERIMENTAL_OPENAI_BASE_LLM_HTTP_HANDLER` /
`base_llm_http_handler`) the spy never covered, making the assertion a test
of "which internal route litellm happens to take today", not of anything
reyn owns — the chat-is-unaffected claim above is now carried declaratively
by this docstring instead, version+site scoped, not by a test) replaces the
wire-count comparison with a STRUCTURAL one for embedding: no HTTP leaves
the process at all. It spies on the real, unmocked `OpenAIChatCompletion.
_get_openai_client` — the chokepoint `litellm.aembedding` passes its
resolved `max_retries` through on the way to building (or reconfiguring)
the OpenAI SDK client — captures the `max_retries` value litellm's OWN code
computed and passed to it, then aborts with a private sentinel exception
before any socket is touched. This reads what litellm actually decided, not
a transcription of the fix's `or` line (test-review question 2: the same
expression written on both sides only fails when someone edits it, and
they'd edit both) — if litellm ever changes how `embedding()`'s fallback
works, the captured value changes with it and this test goes red for the
right reason.

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
from litellm.llms.openai.openai import OpenAIChatCompletion

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
@pytest.mark.allow_real_network(
    reason="#3445/#3451 group D: drives the real litellm client + real "
    "LiteLLMEmbeddingProvider against a REAL local HTTP server (127.0.0.1, "
    "the counting_server fixture) to count wire-level requests — @replay "
    "would return a canned response with no request ever reaching the wire, "
    "deleting the exact thing under test.",
)
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
@pytest.mark.allow_real_network(
    reason="#3445/#3451 group D: same real local counting_server as the "
    "sibling test above — @replay would delete the wire-level request count "
    "under test.",
)
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


class _SpyAbort(Exception):
    """Private sentinel: raised by the `_get_openai_client` spy the instant
    it captures the `max_retries` litellm's real call graph resolved to —
    before the spy lets any further code run, so no socket is ever touched.
    litellm's own `except Exception` / exception-mapping layers (both
    `embedding()` and `completion()` catch broadly, and the `@client`
    decorator wrapping both re-raises through `litellm.exceptions.*`) always
    catch and re-wrap this before it reaches the test — so a caller never
    matches on `_SpyAbort`'s own type via `pytest.raises`; it instead
    catches the broad `Exception` litellm surfaces and asserts on
    `captured` having actually been populated, which only happens if this
    spy fired first."""


def _spy_on_get_openai_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace the REAL `OpenAIChatCompletion._get_openai_client` (never a
    Mock/AsyncMock) with a thin capture-then-abort wrapper: it records the
    `max_retries` kwarg the caller — litellm's own `embedding()`/
    `completion()`, never this test — resolved and passed in, then raises
    `_SpyAbort` immediately, before delegating to the real client-building
    body. Nothing downstream of this chokepoint (cache lookup, `AsyncOpenAI`/
    `OpenAI` construction, and every later HTTP call) ever runs.

    Captures only the FIRST call (measured, directly): litellm's own
    `@client`-decorated outer retry wrapper re-invokes the whole
    `completion()`/`embedding()` body up to `num_retries` times after this
    spy's abort, and forces `max_retries=0` into every one of those INNER
    retries deliberately (litellm's own anti-amplification convention,
    unrelated to what this test is checking) — capturing anything past the
    first call would read that convention's `0`, not the `or`-fallback's
    or `num_retries`-mapping's own resolved value under test."""
    captured: dict[str, object] = {}

    def _spy(self: OpenAIChatCompletion, *args: object, **kwargs: object) -> object:
        if "max_retries" not in captured:
            captured["max_retries"] = kwargs.get("max_retries")
        raise _SpyAbort

    monkeypatch.setattr(OpenAIChatCompletion, "_get_openai_client", _spy)
    return captured


@pytest.mark.asyncio
@pytest.mark.allow_real_network(
    reason="#3445/#3451: calls the real litellm.aembedding (reyn's own "
    "network_gate wraps it at module level, before this test's own "
    "_get_openai_client spy ever runs) — but the spy aborts the call with "
    "_SpyAbort at the client-construction chokepoint, before litellm gets "
    "anywhere near opening a socket, so no request is ever attempted "
    "against api_base regardless. @replay would return a canned response "
    "and never reach the spy at all, deleting the exact thing under test.",
)
async def test_embedding_max_retries_reflects_embed_global_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5918 — the embed fix's `litellm.DEFAULT_MAX_RETRIES` global
    mutation reaches the embedding call graph's own `max_retries`, driven
    through the REAL `litellm.aembedding` call graph — no HTTP, no duration.

    Drives `litellm.aembedding(...)` with `max_retries` OMITTED (`None`,
    reyn's own pre-fix shape) once with `litellm.DEFAULT_MAX_RETRIES` at the
    untouched litellm default (2) and once forced to 0 (the state
    `_aembedding_bounded` leaves behind, permanently, once any embed call has
    run) — asserting the value that reaches `_get_openai_client` tracks the
    global exactly. Falsify: if litellm's `embedding()` stops reading
    `litellm.DEFAULT_MAX_RETRIES` in its fallback, the captured value stops
    tracking the global and this goes red — the fix's whole premise.
    """
    for default_max_retries, expected in ((2, 2), (0, 0)):
        monkeypatch.setattr(litellm, "DEFAULT_MAX_RETRIES", default_max_retries)
        captured = _spy_on_get_openai_client(monkeypatch)
        with pytest.raises(Exception, match=".*"):  # litellm re-wraps _SpyAbort — see its docstring
            await litellm.aembedding(
                model=_MODEL,
                input=["hello"],
                api_base="http://127.0.0.1:1",  # never reached — spy aborts first
                api_key="dummy-key-never-used",
                timeout=5.0,
            )
        assert "max_retries" in captured, (
            "the _get_openai_client spy never fired — litellm's aembedding "
            "call graph did not reach the client-construction chokepoint "
            "this test means to observe"
        )
        assert captured["max_retries"] == expected, (
            f"litellm.DEFAULT_MAX_RETRIES={default_max_retries} but "
            f"_get_openai_client saw max_retries={captured['max_retries']!r} "
            f"(expected {expected}) — embedding's or-fallback did not track "
            f"the global the way the fix's docstring claims"
        )
