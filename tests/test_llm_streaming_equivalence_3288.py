"""Tier 3a: LLM-replay behavior — #3288 ③a stream≡whole equivalence (MAIN gate).

For the same model output, the capability-gated streaming loop inside
``recorded_acompletion`` must reconstruct an IDENTICAL result (content +
tool_calls + finish_reason + usage) to the whole-collect path. Streaming is
an internal optimization in this phase — callers see no behavioral
difference.

Uses a scripted ``litellm.acompletion`` replacement (a real async callable,
per the repo's allowed monkeypatch-with-a-real-callable idiom — see
``docs/deep-dives/contributing/testing.md`` "monkeypatch.setattr with a real
callable"). NOT a MagicMock: the streaming branch returns REAL
``litellm.types.utils.ModelResponseStream`` chunks that flow through the
production code's REAL ``litellm.stream_chunk_builder`` call, and the
whole-collect branch returns a REAL ``litellm.ModelResponse`` — the actual
litellm types production code depends on, so a shape/signature drift in
either would raise, not silently pass.
"""
from __future__ import annotations

import asyncio
from typing import Any

import litellm
from litellm.types.utils import (
    ChatCompletionDeltaToolCall,
    Delta,
    Function,
    ModelResponseStream,
    StreamingChoices,
    Usage,
)

from reyn.llm.llm import recorded_acompletion

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}]

# A multi-arg tool_call, split across TWO argument-delta chunks (exercises the
# per-index accumulation the ADR requires), plus text content split across
# TWO content-delta chunks.
_CONTENT = "Hello world"
_ARGS = '{"city": "Tokyo", "unit": "celsius"}'
_USAGE = {"prompt_tokens": 42, "completion_tokens": 13, "total_tokens": 55}


def _whole_response(model: str) -> "litellm.ModelResponse":
    return litellm.ModelResponse(
        id="resp-1", created=1, model=model, object="chat.completion",
        choices=[{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": _CONTENT,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather", "arguments": _ARGS},
                }],
            },
        }],
        usage=dict(_USAGE),
    )


async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
    """A real, scripted stand-in for ``litellm.acompletion`` — NOT a mock.
    Branches on ``stream`` exactly like the real litellm client does."""
    if not kw.get("stream"):
        return _whole_response(model)

    def _chunk(delta: Delta, finish_reason: str | None = None) -> ModelResponseStream:
        return ModelResponseStream(
            id="resp-1", created=1, model=model, object="chat.completion.chunk",
            choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
        )

    async def _gen():
        mid = len(_CONTENT) // 2
        yield _chunk(Delta(role="assistant", content=_CONTENT[:mid]))
        yield _chunk(Delta(content=_CONTENT[mid:]))
        amid = len(_ARGS) // 2
        yield _chunk(Delta(tool_calls=[
            ChatCompletionDeltaToolCall(
                id="call_1", index=0, type="function",
                function=Function(name="get_weather", arguments=_ARGS[:amid]),
            ),
        ]))
        yield _chunk(Delta(tool_calls=[
            ChatCompletionDeltaToolCall(index=0, function=Function(arguments=_ARGS[amid:])),
        ]))
        last = _chunk(Delta(), finish_reason="tool_calls")
        last.usage = Usage(**_USAGE)
        yield last

    return _gen()


def test_stream_equals_whole_result(monkeypatch) -> None:
    """Tier 3a: streamed reconstruction == whole-collect result for the same
    model output (content, multi-arg tool_call, finish_reason, usage)."""
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    # gpt-4o-mini: real litellm capability data says native-streaming +
    # function-calling are both supported → the ③a streaming branch runs.
    streamed = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "weather?"}],
        purpose="main", recorder=None, extra_kwargs={"tools": _TOOLS},
    ))
    # o1-pro: real litellm capability data says supports_native_streaming is
    # False (a genuine reasoning-only-endpoint limitation) → the existing
    # whole-collect path runs unconditionally, regardless of tools.
    whole = asyncio.run(recorded_acompletion(
        model="o1-pro", messages=[{"role": "user", "content": "weather?"}],
        purpose="main", recorder=None, extra_kwargs={"tools": _TOOLS},
    ))

    s_msg = streamed.choices[0].message
    w_msg = whole.choices[0].message
    assert s_msg.content == w_msg.content == _CONTENT
    # Behavioral: compare the extracted (name, arguments) pairs directly —
    # this also implicitly requires exactly one reconstructed tool_call on
    # each side (an empty or multi-entry list would fail the equality, not
    # just a bare cardinality count).
    assert [tc.function.name for tc in s_msg.tool_calls] == ["get_weather"]
    assert [tc.function.name for tc in w_msg.tool_calls] == ["get_weather"]
    assert s_msg.tool_calls[0].function.arguments == w_msg.tool_calls[0].function.arguments == _ARGS
    assert streamed.choices[0].finish_reason == whole.choices[0].finish_reason == "tool_calls"
    assert streamed.usage.prompt_tokens == whole.usage.prompt_tokens == _USAGE["prompt_tokens"]
    assert (
        streamed.usage.completion_tokens
        == whole.usage.completion_tokens
        == _USAGE["completion_tokens"]
    )


def test_stream_path_actually_used_for_capable_model(monkeypatch) -> None:
    """Tier 3a: sanity witness — the streaming branch is genuinely exercised
    (not accidentally always falling back to whole-collect), by asserting the
    scripted callable observed ``stream=True`` for the capable model."""
    seen: list[bool] = []

    async def _observing(model: str, messages: list, **kw: Any) -> Any:
        seen.append(bool(kw.get("stream")))
        return await _fake_acompletion(model, messages, **kw)

    monkeypatch.setattr(litellm, "acompletion", _observing)
    asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "weather?"}],
        purpose="main", recorder=None, extra_kwargs={"tools": _TOOLS},
    ))
    assert seen == [True], "capable model + tools must select the streaming branch"
