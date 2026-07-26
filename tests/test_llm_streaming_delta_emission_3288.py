"""Tier 3a / Tier 1: #3288 ③b — the ``on_content_delta`` callback wiring inside
``recorded_acompletion`` / ``call_llm_tools`` (``src/reyn/llm/llm.py``).

③a (merged, #3304) added a capability-gated streaming loop that reconstructs
the SAME whole response internally — callers saw no behavioral difference.
③b's job is to carry the per-chunk deltas OUT to a caller-supplied callback
while leaving that reconstruction (and everything callers already observed)
untouched. This file is the llm.py-level half of that; the chat-event/
transport half lives in ``tests/test_agent_delta_chat_event_3288.py``.

Uses a scripted ``litellm.acompletion`` replacement (a real async callable —
same idiom as ``test_llm_streaming_equivalence_3288.py``), never a
``unittest.mock`` double. ``chunk_witness`` proves REAL per-chunk consumption
drove the callback invocations (not that the whole-collect fallback silently
produced a plausible-looking result while never actually streaming).
"""
from __future__ import annotations

import asyncio
from typing import Any

import litellm
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from reyn.llm.llm import call_llm_tools, recorded_acompletion

_CONTENT = "Hello world"
_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def _make_fake_acompletion(chunk_witness: "list[int] | None" = None):
    """Real, scripted stand-in for ``litellm.acompletion`` — branches on
    ``stream`` exactly like the real client. The streaming branch splits
    ``_CONTENT`` across TWO content-delta chunks plus a trailing usage-only
    terminal chunk (no content) — the terminal chunk exercises the "chunk
    with no content delta" skip path."""

    async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
        if not kw.get("stream"):
            return litellm.ModelResponse(
                id="resp-1", created=1, model=model, object="chat.completion",
                choices=[{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _CONTENT},
                }],
                usage=dict(_USAGE),
            )

        def _chunk(delta: Delta, finish_reason: "str | None" = None) -> ModelResponseStream:
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
            last = _chunk(Delta(), finish_reason="stop")
            last.usage = Usage(**_USAGE)
            pieces.append(last)
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_acompletion


def test_on_content_delta_fires_per_real_content_chunk(monkeypatch) -> None:
    """Tier 3a: ``on_content_delta`` fires once per non-empty content-delta
    chunk, in order, with the RAW per-chunk text — witnessed via real chunk
    consumption (``chunk_witness``), not merely that ``stream=True`` was
    requested (see ``_stream_and_reconstruct``'s non-aiter-able fallback)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas: list[str] = []
    result = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        on_content_delta=deltas.append,
    ))

    # All 3 scripted chunks (2 content + 1 usage-only terminal) were really
    # consumed off the async generator.
    assert chunk_witness == [1, 1, 1]
    # Only the 2 content-bearing chunks reached the callback — the terminal
    # usage-only chunk (empty delta.content) is silently skipped.
    assert deltas == [_CONTENT[: len(_CONTENT) // 2], _CONTENT[len(_CONTENT) // 2 :]]
    assert "".join(deltas) == _CONTENT
    # ③a's whole-result reconstruction is UNAFFECTED by ③b's callback.
    assert result.choices[0].message.content == _CONTENT
    assert result.usage.prompt_tokens == _USAGE["prompt_tokens"]


def test_on_content_delta_never_fires_on_the_whole_collect_path(monkeypatch) -> None:
    """Tier 3a: capability-gated — a non-streaming-capable model's whole-collect
    path never invokes ``on_content_delta``, even when a callback is supplied.
    Companion to ③a's capability-driver gate: the callback firing here would
    prove capability is NOT actually gating whether deltas are ever produced."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas: list[str] = []
    result = asyncio.run(recorded_acompletion(
        # o1-pro: real litellm capability data reports no native streaming
        # support (a genuine reasoning-only-endpoint limitation) — same model
        # ③a's own equivalence test uses for the whole-collect branch.
        model="o1-pro", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        on_content_delta=deltas.append,
    ))

    assert deltas == []
    # The whole-collect path never touches the chunk generator at all.
    assert chunk_witness == []
    assert result.choices[0].message.content == _CONTENT


def test_failing_on_content_delta_does_not_break_the_call(monkeypatch) -> None:
    """Tier 1: a raising ``on_content_delta`` must never abort the in-flight
    LLM call — the callback narrates the stream, it does not gate it (mirrors
    the ``llm_request`` ambient-emit try/except precedent already in this
    chokepoint)."""
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion())

    calls: list[str] = []

    def _boom(text: str) -> None:
        calls.append(text)
        raise RuntimeError("display sink exploded")

    result = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        on_content_delta=_boom,
    ))

    # The callback WAS invoked (and raised, and was swallowed) for each chunk —
    # a vacuous "never called" would let this test pass without exercising the
    # guard at all.
    assert calls == [_CONTENT[: len(_CONTENT) // 2], _CONTENT[len(_CONTENT) // 2 :]]
    assert result.choices[0].message.content == _CONTENT


def test_call_llm_tools_forwards_on_content_delta_through_to_the_stream(monkeypatch) -> None:
    """Tier 3a: ``call_llm_tools`` threads ``on_content_delta`` straight
    through its ``recorded_acompletion`` chokepoint call — the SAME callback
    contract, one layer up (no tools attached; ``has_tools=False`` still
    selects the streaming branch for a capable model)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas: list[str] = []
    result = asyncio.run(call_llm_tools(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        tools=[], on_content_delta=deltas.append,
    ))

    assert chunk_witness == [1, 1, 1]
    assert deltas == [_CONTENT[: len(_CONTENT) // 2], _CONTENT[len(_CONTENT) // 2 :]]
    assert result.content == _CONTENT
