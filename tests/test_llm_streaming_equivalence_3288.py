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


def _make_fake_acompletion(chunk_witness: "list[int] | None" = None):
    """Factory for a real, scripted stand-in for ``litellm.acompletion`` —
    NOT a mock. Branches on ``stream`` exactly like the real litellm client
    does.

    ``chunk_witness`` (optional, closure-captured): when given, one entry is
    appended to it for EVERY chunk actually pulled off the async generator —
    a live proof that the real streaming reconstruction loop iterated real
    chunks, not that the defensive non-aiter-able fallback (see
    ``recorded_acompletion``'s ``_stream_and_reconstruct``) silently
    substituted the whole response instead. A call requesting ``stream=True``
    does NOT by itself prove the streaming path ran to completion — only
    actual chunk consumption does; the fallback fires whenever the callee
    (real or faked) does not honor the request, which is exactly what a
    non-stream-aware fake would do.
    """
    async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
        if not kw.get("stream"):
            return _whole_response(model)

        def _chunk(delta: Delta, finish_reason: str | None = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-1", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            mid = len(_CONTENT) // 2
            pieces = [
                _chunk(Delta(role="assistant", content=_CONTENT[:mid])),
                _chunk(Delta(content=_CONTENT[mid:])),
            ]
            amid = len(_ARGS) // 2
            pieces.append(_chunk(Delta(tool_calls=[
                ChatCompletionDeltaToolCall(
                    id="call_1", index=0, type="function",
                    function=Function(name="get_weather", arguments=_ARGS[:amid]),
                ),
            ])))
            pieces.append(_chunk(Delta(tool_calls=[
                ChatCompletionDeltaToolCall(index=0, function=Function(arguments=_ARGS[amid:])),
            ])))
            last = _chunk(Delta(), finish_reason="tool_calls")
            last.usage = Usage(**_USAGE)
            pieces.append(last)
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_acompletion


# Default instance (no witness) for tests that don't need chunk-consumption
# proof beyond what they already assert directly on the reconstructed result.
_fake_acompletion = _make_fake_acompletion()


def test_stream_equals_whole_result(monkeypatch) -> None:
    """Tier 3a: streamed reconstruction == whole-collect result for the same
    model output (content, multi-arg tool_call, finish_reason, usage)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    # gpt-4o-mini: real litellm capability data says native-streaming +
    # function-calling are both supported → the ③a streaming branch runs.
    streamed = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "weather?"}],
        purpose="main", recorder=None, extra_kwargs={"tools": _TOOLS},
    ))
    # Witness: the streaming reconstruction loop actually pulled all 5
    # scripted chunks off the async generator — proves this exercised the
    # real per-chunk accumulation path, not the non-aiter-able fallback
    # (which would leave chunk_witness empty even though "streamed" above
    # still returned a valid-looking result via the whole-collect shortcut).
    assert chunk_witness == [1, 1, 1, 1, 1], (
        "expected all 5 scripted chunks consumed — got "
        f"{len(chunk_witness)}; a mismatch means the fallback (not the real "
        "streaming loop) produced `streamed`."
    )
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
    (not accidentally always falling back to whole-collect). Two independent
    witnesses: (1) the scripted callable observed ``stream=True`` requested
    for the capable model, AND (2) real chunks were actually consumed off
    the async generator — (1) alone is insufficient, since a call can
    request ``stream=True`` and still fall through the non-aiter-able
    defensive fallback without ever touching the per-chunk reconstruction
    loop (exactly what happens for several PRE-existing test doubles
    elsewhere in this suite that stub ``litellm.acompletion`` without
    branching on ``stream`` — see ``_stream_and_reconstruct``'s docstring)."""
    seen: list[bool] = []
    chunk_witness: list[int] = []
    _fake = _make_fake_acompletion(chunk_witness)

    async def _observing(model: str, messages: list, **kw: Any) -> Any:
        seen.append(bool(kw.get("stream")))
        return await _fake(model, messages, **kw)

    monkeypatch.setattr(litellm, "acompletion", _observing)
    asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "weather?"}],
        purpose="main", recorder=None, extra_kwargs={"tools": _TOOLS},
    ))
    assert seen == [True], "capable model + tools must select the streaming branch"
    assert chunk_witness == [1, 1, 1, 1, 1], (
        "stream=True was requested but no chunks were actually consumed — "
        "the call must have gone through the non-aiter-able fallback instead "
        "of the real streaming reconstruction loop."
    )


def test_llm_replay_synthetic_stream_preserves_parallel_tool_calls() -> None:
    """Tier 1: co-vet BLOCK fix — ``LLMReplay._synthetic_stream`` must assign
    each PARALLEL tool_call its OWN index, not a shared ``index=0``.

    ``litellm.stream_chunk_builder`` accumulates tool_call argument deltas
    PER INDEX. Before the fix, every synthetic chunk claimed index=0
    regardless of which tool_call it belonged to, so 2 parallel tool calls
    MERGED into 1 on reconstruction (their argument strings concatenated
    into invalid JSON) — silently, no exception. reyn does emit parallel
    tool calls, so a fixture recorded with 2+ tool calls in one turn would
    corrupt on replay under streaming.

    Non-vacuity: this test is RED against the pre-fix (index=0-for-all)
    ``_synthetic_stream`` (asserts below would fail: 1 reconstructed
    tool_call instead of 2, and/or invalid JSON args) and GREEN against the
    ``enumerate``-based fix.
    """
    import json

    import litellm

    from reyn.dev.testing.replay import LLMReplay

    whole = litellm.ModelResponse(
        id="resp-parallel", created=1, model="gpt-4o-mini", object="chat.completion",
        choices=[{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {
                        "id": "call_a", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "/a.txt"}'},
                    },
                    {
                        "id": "call_b", "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "/b.txt"}'},
                    },
                ],
            },
        }],
        usage={"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
    )

    async def _drain() -> object:
        gen = LLMReplay._synthetic_stream(whole)
        chunks = [chunk async for chunk in gen]
        return litellm.stream_chunk_builder(chunks, messages=[{"role": "user", "content": "read both"}])

    rebuilt = asyncio.run(_drain())
    rebuilt_calls = rebuilt.choices[0].message.tool_calls

    # Both parallel tool calls survive distinctly (a merge would collapse
    # this to a single entry).
    assert [tc.id for tc in rebuilt_calls] == ["call_a", "call_b"]
    assert [tc.function.name for tc in rebuilt_calls] == ["read_file", "read_file"]

    # Each call's arguments are valid, UN-concatenated JSON matching its own
    # whole-response counterpart — a merge would produce
    # '{"path":"/a.txt"}{"path":"/b.txt"}' (invalid JSON, raises on parse).
    rebuilt_args = [json.loads(tc.function.arguments) for tc in rebuilt_calls]
    assert rebuilt_args == [{"path": "/a.txt"}, {"path": "/b.txt"}]
