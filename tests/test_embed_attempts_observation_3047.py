"""Tier 2: a successful embed emits an `embed_attempts` audit-event carrying
reyn's OWN retry-loop count, and does so WITHOUT touching the cost aggregate
(#3047 (c), observation-only).

#3047's investigation measured that the embedding cost tracker records only the
ONE returned response's tokens, so a call that retried N times before succeeding
silently reports 1-of-N delivered requests. #3054 collapsed the *count* itself
(9->3, `max_retries=0`); this closes the *visibility* gap owner GO'd as
candidate (c): make the retry overhead OBSERVABLE via a P6 audit-event, priced
NOWHERE (so it cannot double-count against `record_embedding`).

**Why a REAL retry, not a first-try success (architect co-vet #1).** A test
whose provider succeeds on attempt 1 has `attempts == successful_batches == 1`,
so it would pass identically whether `attempts` is wired to the real loop
counter or hard-coded to 1 — a vacuous witness. So the primary test drives a
REAL `LiteLLMEmbeddingProvider` (via `LITELLM_API_BASE`, its own production
proxy knob — no monkeypatched provider, no monkeypatched litellm) at a REAL
localhost server that answers the FIRST request with a retryable 500 and the
SECOND with a valid 200 embedding. reyn's own `_embed_batch_with_retry` loop
therefore runs exactly twice, and we assert `attempts == 2`,
`successful_batches == 1` — a value only the real loop counter can produce.

No real provider is contacted and no cost is spent (local socket only).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.embed import handle as embed_handle
from reyn.data.embedding.litellm_provider import LiteLLMEmbeddingProvider
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.services.budget_gateway import BudgetGateway
from reyn.schemas.models import EmbedIROp
from reyn.security.permissions.permissions import PermissionDecl

# A real litellm-priceable embedding model, so the recorded figure is a real
# `litellm.model_cost` lookup rather than a fabricated rate.
_MODEL = "openai/text-embedding-3-small"
_RESPONSE_TOKENS = 7  # the ONE returned response's usage — the cost-invariance anchor


class _FailThenSucceedHandler(BaseHTTPRequestHandler):
    """First POST -> retryable 500; every subsequent POST -> a valid 200
    embedding. Drives reyn's `_embed_batch_with_retry` loop through EXACTLY one
    real retry (attempt 1 fails, attempt 2 succeeds), so `attempts == 2`."""

    request_count = 0
    _lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)  # drain the full body — a real delivered request
        with _FailThenSucceedHandler._lock:
            _FailThenSucceedHandler.request_count += 1
            n = _FailThenSucceedHandler.request_count
        if n == 1:
            body = b'{"error": {"message": "stand-in 500", "type": "server_error"}}'
            self.send_response(500)
        else:
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}
                    ],
                    "model": "text-embedding-3-small",
                    "usage": {"prompt_tokens": _RESPONSE_TOKENS, "total_tokens": _RESPONSE_TOKENS},
                }
            ).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


@pytest.fixture
def fail_then_succeed_server(monkeypatch: pytest.MonkeyPatch):
    _FailThenSucceedHandler.request_count = 0
    srv = HTTPServer(("127.0.0.1", 0), _FailThenSucceedHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("LITELLM_API_BASE", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-never-used")
    try:
        yield _FailThenSucceedHandler
    finally:
        srv.shutdown()
        thread.join(timeout=5)


def _ctx_with_gateway() -> tuple[OpContext, EventLog, BudgetGateway]:
    events = EventLog()
    ws = Workspace(events=events)
    gateway = BudgetGateway(
        budget_tracker=BudgetTracker(CostConfig()),
        events=events,
        agent_name="embedder",
    )
    ctx = OpContext(
        workspace=ws,
        events=events,
        permission_decl=PermissionDecl(),
        budget_gateway=gateway,
    )
    return ctx, events, gateway


@pytest.mark.asyncio
async def test_real_retry_populates_attempts_and_op_emits_embed_attempts(
    fail_then_succeed_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: one real retry (500 then 200) -> `attempts == 2`,
    `successful_batches == 1`, and the op emits `embed_attempts` carrying both.

    Drives the op `handle()` through a REAL `LiteLLMEmbeddingProvider` routed at
    the fail-then-succeed stand-in — so both the provider's populate and the op's
    emit are on the real path. A first-try success would make this vacuous
    (`attempts == successful_batches`); the forced retry is what gives `2 != 1`,
    a value only the real loop counter can produce (architect co-vet #1)."""
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 5.0,
            "max_retries": 3,
            "retry_backoff": 1.0,  # 1.0^attempt == 1s sleeps; keeps the test quick
            "classes": {"standard": _MODEL},
        }
    )
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)

    ctx, events, _gateway = _ctx_with_gateway()
    result = await embed_handle(
        EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard"), ctx
    )

    assert result.get("status") != "error", result

    # The witness is the audit-event: the op reads `result.get("attempts")` off
    # the provider's EmbedBatchResult and emits it (the op's own return shape does
    # not carry `attempts` — the provider populates the data, the op emits it).
    # The real retry loop ran twice: attempt 1 (500) + attempt 2 (200), so the
    # provider-populated count reaches the event as 2 (not the vacuous 1).
    attempts_events = [e for e in events.all() if e.type == "embed_attempts"]
    assert attempts_events, [e.type for e in events.all()]
    payload = attempts_events[0].data
    assert payload["attempts"] == 2, payload
    assert payload["successful_batches"] == 1, payload
    assert payload["model"]  # the canonical model is stamped


@pytest.mark.asyncio
async def test_embed_attempts_does_not_double_count_cost(
    fail_then_succeed_server, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: cost is unchanged by the observation seam — the tracker records
    ONLY the ONE returned response's tokens, never `tokens * attempts`.

    The whole point of candidate (c) is that observation does not touch the cost
    aggregate. Two attempts were delivered but only one returned, so the recorded
    figure must be the single response's `_RESPONSE_TOKENS`, NOT twice it."""
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 5.0,
            "max_retries": 3,
            "retry_backoff": 1.0,
            "classes": {"standard": _MODEL},
        }
    )
    import reyn.core.op_runtime.embed as _embed_mod
    monkeypatch.setattr(_embed_mod, "get_provider", lambda *a, **kw: provider)

    ctx, _events, gateway = _ctx_with_gateway()
    result = await embed_handle(
        EmbedIROp(kind="embed", texts=["hello"], embedding_model="standard"), ctx
    )

    assert result.get("status") != "error", result
    # Recorded tokens == the single returned response's usage, NOT x2 for the
    # two delivered attempts. attempts=2 in the audit-event, tokens=7 in cost.
    assert gateway.embedding_cost.tokens == _RESPONSE_TOKENS, (
        f"cost recorded {gateway.embedding_cost.tokens} tokens; expected the "
        f"single returned response's {_RESPONSE_TOKENS} (attempts must not "
        f"multiply the cost — observation-only, no double-count)"
    )
    assert gateway.embedding_cost.calls == 1


def test_litellm_provider_module_does_not_import_op_runtime_or_ctx() -> None:
    """Tier 2: (P7) the provider carries the `attempts` DATA on its own TypedDict
    but never imports `op_runtime` / `OpContext` — emitting the audit-event is
    the op layer's job (architect co-vet #3). A source grep, not an import probe,
    so it catches a lazy/in-function import too."""
    import inspect

    from reyn.data.embedding import litellm_provider

    src = inspect.getsource(litellm_provider)
    # Scan IMPORT statements only — the module docstring legitimately mentions
    # "op_runtime" in prose ("Does NOT import from op_runtime"), so a raw
    # substring grep false-positives. An in-function/lazy import is still an
    # import line, so this catches those too.
    import_lines = [
        ln.strip()
        for ln in src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    offenders = [
        ln for ln in import_lines
        if "op_runtime" in ln or "OpContext" in ln or ".context" in ln
    ]
    assert not offenders, f"provider must not import op_runtime/OpContext (P7): {offenders}"
