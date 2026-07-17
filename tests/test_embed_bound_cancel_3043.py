"""Tier 2: `embed` is bounded and cancellable like every other provider call (#3043).

`embed` was the ONE provider call in the OS with neither half: MCP has the
gateway's bound + `race_cancellable`; the chat LLM path has
`chat.timeout.llm_call_seconds`; `litellm.aembedding` was called with no
`timeout=` and no cancel seam, so its only ceiling was litellm's own
`request_timeout` default (6000s/attempt) and a Ctrl-C could not interrupt it.

**Why these tests need a TCP blackhole.** The defect is un-witnessable in the
environment the old tests ran in. `tests/test_embedding_provider.py` states that
`LLMReplay` only patches `litellm.acompletion`, never `aembedding` — so no test
ever drove this path; and a path that answers promptly (a real API returning a
200, or a 429) cannot tell a bounded call from an unbounded one, because
*nothing is waiting*. A bound is only observable where the call would otherwise
NOT return. So each test below points a REAL `LiteLLMEmbeddingProvider` at a
REAL localhost socket that completes the TCP handshake and then never writes a
byte — the stall the bound exists for. No fakes, no monkeypatched provider: the
production provider, the production litellm, a hostile socket.

Each test carries its own `asyncio.wait_for` ceiling well below the behaviour it
falsifies, so a regression FAILS (TimeoutError) instead of hanging the suite —
i.e. strip the bound and these go red, they do not go slow.
"""
from __future__ import annotations

import asyncio
import socket

import pytest

from reyn.core.events.events import EventLog
from reyn.core.op_runtime.context import OpContext
from reyn.core.op_runtime.embed import handle as embed_handle
from reyn.data.embedding.litellm_provider import (
    LiteLLMEmbeddingProvider,
    resolve_embed_timeout,
)
from reyn.data.workspace.workspace import Workspace
from reyn.schemas.models import EmbedIROp
from reyn.security.permissions.permissions import PermissionDecl

_MODEL = "openai/text-embedding-3-small"


@pytest.fixture
def stalled_endpoint(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the real provider at a real TCP endpoint that never replies.

    A listening socket that is never `accept()`ed IS the blackhole: the kernel
    completes the 3-way handshake for backlog connections on its own, so the
    client reaches ESTABLISHED, sends its request, and waits forever for bytes
    that never come. That is the I/O stall the bound exists for — and the
    environment the bound's claim is about. An endpoint that ANSWERS (200, or
    the 429 a real quota-less key returns) is a different environment: nothing
    is waiting there, so it cannot witness a bound at all.

    `LITELLM_API_BASE` is the provider's own production routing knob
    (`_proxy_kwargs`) — no patching of the provider or of litellm.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)  # never accept()ed
    url = f"http://127.0.0.1:{sock.getsockname()[1]}"
    monkeypatch.setenv("LITELLM_API_BASE", url)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-never-used")
    try:
        yield url
    finally:
        sock.close()


@pytest.fixture
def refused_endpoint(monkeypatch: pytest.MonkeyPatch) -> str:
    """Point the real provider at a closed port — fails fast, no stall.

    The control environment for the cancel seam: a call that ends on its own.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("LITELLM_API_BASE", url)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key-never-used")
    return url


def _make_ctx(cancel_event: "asyncio.Event | None" = None) -> tuple[OpContext, EventLog]:
    events = EventLog()
    return (
        OpContext(
            workspace=Workspace(events=events),
            events=events,
            permission_decl=PermissionDecl(),
            cancel_event=cancel_event,
        ),
        events,
    )


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stalled_embedding_call_is_bounded_not_left_to_litellm_default(
    stalled_endpoint: str,
) -> None:
    """Tier 2: a stalled embedding endpoint surfaces an error under the configured
    bound — it does NOT run to litellm's own 6000s/attempt request_timeout.

    Falsifies the defect directly: pre-#3043 this call returns after ~6000s (the
    test's 30s ceiling makes that a failure, which is the point).
    """
    provider = LiteLLMEmbeddingProvider(
        {"timeout": 1.0, "max_retries": 1, "classes": {"standard": _MODEL}}
    )

    with pytest.raises(RuntimeError, match="Embedding failed"):
        await asyncio.wait_for(provider.embed(["hello"], "standard"), timeout=30.0)


@pytest.mark.asyncio
async def test_bound_applies_to_every_attempt_not_only_the_first(
    stalled_endpoint: str,
) -> None:
    """Tier 2: the bound is PER attempt — N stalled attempts each end at the
    deadline, so the retry loop terminates instead of multiplying 6000s by N."""
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 0.5,
            "max_retries": 3,
            "retry_backoff": 1.0,  # 1.0^attempt == 1s sleeps; keeps the test quick
            "classes": {"standard": _MODEL},
        }
    )

    with pytest.raises(RuntimeError, match="after 3 attempts"):
        await asyncio.wait_for(provider.embed(["hello"], "standard"), timeout=30.0)


@pytest.mark.asyncio
async def test_batches_are_bounded_independently_of_batch_count(
    stalled_endpoint: str,
) -> None:
    """Tier 2: the bound holds on the internal-batching path too — a multi-batch
    embed against a stalled endpoint still terminates (each batch's attempt is
    bounded), rather than any one batch wedging the gather forever."""
    provider = LiteLLMEmbeddingProvider(
        {
            "timeout": 0.5,
            "max_retries": 1,
            "batch_size": 1,  # 3 texts -> 3 batches
            "classes": {"standard": _MODEL},
        }
    )

    with pytest.raises(RuntimeError, match="Embedding failed"):
        await asyncio.wait_for(
            provider.embed(["a", "b", "c"], "standard"), timeout=30.0
        )


# ---------------------------------------------------------------------------
# The operator knob
# ---------------------------------------------------------------------------

def test_default_bound_is_finite_and_matches_the_chat_llm_call_bound() -> None:
    """Tier 2: with no operator config the bound is finite (the invariant) and is
    the same number a chat LLM call carries — an embed is the same kind of call.

    Reads `chat.timeout.llm_call_seconds`'s own default rather than restating
    60.0, so the two cannot silently drift apart.
    """
    from reyn.config.chat import TimeoutConfig

    assert resolve_embed_timeout({}) == TimeoutConfig().llm_call_seconds


def test_operator_knob_reaches_the_provider_from_the_real_config_object() -> None:
    """Tier 2: `embedding.timeout` set in reyn.yaml reaches the provider's bound.

    Drives the REAL parse path (`_build_embedding_config`) with a NON-default
    value into the REAL provider constructor — the production wiring, not the
    dict form the legacy/test path uses.
    """
    from reyn.config.embedding import _build_embedding_config

    cfg = _build_embedding_config({"timeout": 12.5})
    assert cfg.timeout == 12.5
    assert LiteLLMEmbeddingProvider(cfg).timeout == 12.5


def test_non_positive_timeout_opts_out_of_the_bound() -> None:
    """Tier 2: `<= 0` opts out (no bound) — the MCP gateway's own
    `call_timeout_seconds` contract, so one knob's semantics teach the other."""
    assert resolve_embed_timeout({"timeout": 0}) is None
    assert resolve_embed_timeout({"timeout": -1}) is None
    assert LiteLLMEmbeddingProvider({"timeout": 0}).timeout is None


def test_malformed_timeout_falls_back_to_the_bound_not_to_no_bound() -> None:
    """Tier 2: a malformed value fails SAFE — it keeps a finite bound rather than
    silently restoring the unbounded call (mirrors `resolve_call_timeout`)."""
    assert resolve_embed_timeout({"timeout": "not-a-number"}) == resolve_embed_timeout({})
    assert resolve_embed_timeout({"timeout": None}) == resolve_embed_timeout({})


# ---------------------------------------------------------------------------
# The cancel seam
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cancel_event_interrupts_a_stalled_embed_op_immediately(
    stalled_endpoint: str,
) -> None:
    """Tier 2: Ctrl-C mid-embed cancels the in-flight call at once and surfaces the
    cancelled outcome + audit-event — it does not wait out the bound.

    The bound here is the 60s default and the cancel fires at 0.3s, so the bound
    CANNOT be what ends this call: only the cancel seam can. Pre-#3043 the op had
    no seam at all and this would run the full 60s (past the 20s ceiling).
    """
    cancel_event = asyncio.Event()
    ctx, events = _make_ctx(cancel_event=cancel_event)

    async def _fire_soon() -> None:
        await asyncio.sleep(0.3)
        cancel_event.set()

    asyncio.ensure_future(_fire_soon())
    result = await asyncio.wait_for(
        embed_handle(EmbedIROp(kind="embed", texts=["hello"], embedding_model=_MODEL), ctx),
        timeout=20.0,
    )

    assert result["status"] == "cancelled"
    assert "embed_cancelled" in [e.type for e in events.all()]


@pytest.mark.asyncio
async def test_uncancelled_embed_keeps_its_normal_failure_shape(
    refused_endpoint: str,
) -> None:
    """Tier 2: the cancel seam is invisible when no cancel fires — an unset event
    does not reshape the outcome, so `status="cancelled"` means a cancel and
    nothing else (a provider failure still raises, and emits no cancel event)."""
    ctx, events = _make_ctx(cancel_event=asyncio.Event())  # never set

    with pytest.raises(RuntimeError, match="Embedding failed"):
        await asyncio.wait_for(
            embed_handle(EmbedIROp(kind="embed", texts=["hello"], embedding_model=_MODEL), ctx),
            timeout=60.0,
        )

    assert "embed_cancelled" not in [e.type for e in events.all()]
