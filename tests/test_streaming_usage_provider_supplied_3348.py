"""Tier 2 / Tier 1: #3348 — a streamed call records the PROVIDER's token
counts, never litellm's local estimate.

``recorded_acompletion``'s streaming loop (#3288 ③a) reconstructs the whole
response with litellm's ``stream_chunk_builder``. That builder can only report
provider-supplied usage if the stream actually carried a usage-bearing chunk,
and litellm's ``CustomStreamWrapper`` only yields one when the call passed
``stream_options={"include_usage": True}``. Reyn used to gate that flag on
``litellm.get_supported_openai_params(model=...)`` — a list of what a provider
accepts ON THE WIRE. Gemini and Anthropic do not list it, so on those models
the flag was never sent, no usage chunk was ever produced, and
``stream_chunk_builder`` fell back to ``litellm.token_counter`` — a LOCAL
ESTIMATE that then flowed into ``record_llm`` → ``/cost`` → budget caps as if
it were the provider's own number (measured live on Gemini: 13 recorded vs 7
actual, +86%).

The flag is consumed client-side by the stream wrapper, and litellm's param
layer never rejects it (see the Tier 1 test below), so #3348 sets it
unconditionally — one path, no provider branching.

Real instances throughout: a real ``BudgetTracker`` is the recorder (its public
``agent_tokens`` read is the assertion surface), and ``litellm.acompletion`` is
replaced with a real scripted async callable — never a ``unittest.mock``
double — that reproduces the wrapper's client-side contract: it emits the
usage-bearing final chunk ONLY when ``include_usage`` was requested. A test
whose fake always emitted usage would pass with or without the fix.
"""
from __future__ import annotations

import asyncio
from typing import Any

import litellm
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage
from litellm.utils import get_optional_params

from reyn.llm.llm import call_llm_tools, recorded_acompletion
from reyn.runtime.budget.budget import BudgetTracker, CostConfig

# Sentinels chosen so no token-count ESTIMATE of the tiny prompt below could
# ever coincide with them — a near-miss estimate must not be able to pass this
# test for the wrong reason.
_PROVIDER_PROMPT_TOKENS = 7777
_PROVIDER_COMPLETION_TOKENS = 131
_CONTENT = "hello world"
_MESSAGES = [{"role": "user", "content": "hi"}]

# The model the historical gate excluded: litellm's supported-params list for
# Gemini has no ``stream_options``, so the pre-#3348 code never sent the flag
# for it. This is the exact shape of the live defect.
_GATED_MODEL = "gemini/gemini-2.5-flash-lite"


def _make_fake_acompletion(seen_kwargs: "list[dict] | None" = None,
                           chunk_witness: "list[int] | None" = None):
    """Real, scripted stand-in for ``litellm.acompletion``.

    Reproduces the one behaviour that matters here — litellm's
    ``CustomStreamWrapper.check_send_stream_usage``: the usage-bearing final
    chunk is emitted **only** when the caller asked for
    ``stream_options={"include_usage": True}``. Otherwise the stream ends with
    a plain finish chunk carrying no usage at all, and the caller's
    reconstruction has nothing but an estimate to fall back on.
    """

    async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
        if seen_kwargs is not None:
            seen_kwargs.append(dict(kw))
        if not kw.get("stream"):
            return litellm.ModelResponse(
                id="resp-3348", created=1, model=model, object="chat.completion",
                choices=[{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _CONTENT},
                }],
                usage={
                    "prompt_tokens": _PROVIDER_PROMPT_TOKENS,
                    "completion_tokens": _PROVIDER_COMPLETION_TOKENS,
                    "total_tokens": _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS,
                },
            )

        include_usage = bool((kw.get("stream_options") or {}).get("include_usage"))

        def _chunk(delta: Delta, finish_reason: "str | None" = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-3348", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            pieces = [
                _chunk(Delta(role="assistant", content=_CONTENT)),
                _chunk(Delta(), finish_reason="stop"),
            ]
            if include_usage:
                usage_chunk = _chunk(Delta())
                usage_chunk.usage = Usage(
                    prompt_tokens=_PROVIDER_PROMPT_TOKENS,
                    completion_tokens=_PROVIDER_COMPLETION_TOKENS,
                    total_tokens=_PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS,
                )
                pieces.append(usage_chunk)
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_acompletion


def test_gemini_streamed_call_records_the_providers_own_token_counts(monkeypatch) -> None:
    """Tier 2: cost accounting records provider-supplied usage, not an estimate.

    The model is one litellm's supported-params data excludes ``stream_options``
    for — the pre-#3348 gate therefore never sent the flag, the scripted
    provider never emitted a usage chunk, and the recorded figure was
    ``litellm.token_counter``'s estimate of the prompt. Asserting the RECORDED
    number (a real ``BudgetTracker``'s public per-agent total, the same counter
    ``/cost`` and the budget caps read) rather than merely that a flag was
    passed keeps the witness at the level the defect lived at."""
    assert "stream_options" not in (
        litellm.get_supported_openai_params(model=_GATED_MODEL) or []
    ), (
        f"{_GATED_MODEL} now lists stream_options in litellm's capability data — "
        "this test no longer witnesses the gated-provider case it was written for"
    )

    seen_kwargs: list[dict] = []
    chunk_witness: list[int] = []
    monkeypatch.setattr(
        litellm, "acompletion", _make_fake_acompletion(seen_kwargs, chunk_witness),
    )

    tracker = BudgetTracker(CostConfig())
    response = asyncio.run(recorded_acompletion(
        model=_GATED_MODEL, messages=_MESSAGES, purpose="main",
        recorder=tracker, agent="alpha",
    ))

    # The streaming branch was really driven (chunks drained off the async
    # generator), so this says something about the streaming path and not
    # about the whole-collect fallback.
    assert chunk_witness, "no chunk was consumed — the streaming branch never ran"
    # The provider's figures, exactly — reconstruction summed the usage chunk
    # instead of estimating.
    assert response.usage.prompt_tokens == _PROVIDER_PROMPT_TOKENS
    assert response.usage.completion_tokens == _PROVIDER_COMPLETION_TOKENS
    # …and those are the figures that landed in the cost counters.
    assert tracker.agent_tokens("alpha") == (
        _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS
    )
    # The mechanism: the flag went out on the streaming call itself.
    (stream_call,) = [k for k in seen_kwargs if k.get("stream")]
    assert stream_call["stream_options"] == {"include_usage": True}


def test_call_llm_tools_records_provider_usage_on_the_router_path(monkeypatch) -> None:
    """Tier 2: the same guarantee through ``call_llm_tools`` — the entry point
    ``RouterLoop`` uses for every chat turn, which does its own
    ``budget.record_llm``. The chokepoint being right is not enough if the
    caller that actually feeds ``/cost`` in production takes another route."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(
        litellm, "acompletion", _make_fake_acompletion(None, chunk_witness),
    )

    tracker = BudgetTracker(CostConfig())
    result = asyncio.run(call_llm_tools(
        model=_GATED_MODEL, messages=_MESSAGES, tools=[],
        budget=tracker, budget_agent="alpha",
    ))

    assert chunk_witness, "no chunk was consumed — the streaming branch never ran"
    assert result.usage.prompt_tokens == _PROVIDER_PROMPT_TOKENS
    assert tracker.agent_tokens("alpha") == (
        _PROVIDER_PROMPT_TOKENS + _PROVIDER_COMPLETION_TOKENS
    )


def test_stream_options_is_never_rejected_by_litellms_param_layer() -> None:
    """Tier 1: the external contract that makes the unconditional flag safe.

    ``stream_options`` is exempt from litellm's unsupported-param pruning
    (``litellm/utils.py``: ``if k == "user" or k == "stream_options" or k ==
    "stream": continue``), so passing it to a provider that does not accept it
    is DROPPED before the wire, never raised — with or without ``drop_params``.
    If litellm ever changes that, the unconditional flag becomes a hard error
    on every Gemini/Anthropic call and this goes RED first."""
    for model, provider in (
        ("gemini-2.5-flash-lite", "gemini"),
        ("claude-sonnet-4-5", "anthropic"),
    ):
        for drop in (True, False):
            params = get_optional_params(
                model=model, custom_llm_provider=provider,
                stream=True, stream_options={"include_usage": True},
                drop_params=drop,
            )
            assert params.get("stream") is True
            assert "stream_options" not in params, (
                f"{provider} unexpectedly forwards stream_options on the wire"
            )
