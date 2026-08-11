"""Tier 2: OS invariant — #3288 ③a usage/cost single-emission gate.

★architect pin: usage arriving across streamed chunks is SUMMED inside
``recorded_acompletion`` and emitted via the existing ``recorder.record_llm``
seam EXACTLY ONCE per call — never per-chunk. This preserves the #1190
single-cost-observability-chokepoint invariant; ``budget.py``'s
``record_llm`` signature is untouched by ③a.

A hand-written recorder (not a mock — a real class implementing the exact
``record_llm(**kw)`` collaborator contract, per ``test_cost_chokepoint_1190.py``'s
own ``_Recorder`` precedent) counts calls. The strip: a per-chunk emit would
double/multi-count — this test would go RED under that regression.
"""
from __future__ import annotations

import asyncio
from typing import Any

import litellm
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from reyn.llm.llm import recorded_acompletion
from reyn.llm.pricing import TokenUsage

_TOOLS = [{
    "type": "function",
    "function": {"name": "noop", "parameters": {"type": "object", "properties": {}}},
}]


class _Recorder:
    """Hand-written recorder capturing record_llm calls (not a mock) —
    mirrors ``test_cost_chokepoint_1190.py``'s ``_Recorder``."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def record_llm(self, **kw: Any) -> None:
        self.calls.append(kw)


def _make_fake_streaming_acompletion(chunk_witness: "list[int] | None" = None):
    """Factory for a real, scripted stand-in for litellm.acompletion's
    stream=True contract: FIVE chunks, usage attached only to the terminal
    one — the shape a real provider stream arrives in (usage is a
    terminal-chunk or cross-chunk phenomenon, never available per-chunk up
    front). ``chunk_witness`` (optional): incremented for each chunk
    actually pulled off the generator — proves the test exercised the real
    per-chunk streaming loop, not the non-aiter-able defensive fallback (see
    ``_stream_and_reconstruct``'s docstring)."""
    async def _fake_streaming_acompletion(model: str, messages: list, **kw: Any) -> Any:
        assert kw.get("stream") is True, "this fixture only models the streaming call shape"

        def _chunk(delta: Delta, finish_reason: str | None = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-1", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            last = _chunk(Delta(), finish_reason="stop")
            last.usage = Usage(prompt_tokens=20, completion_tokens=8, total_tokens=28)
            pieces = [
                _chunk(Delta(role="assistant", content="Hel")),
                _chunk(Delta(content="lo ")),
                _chunk(Delta(content="there")),
                last,
            ]
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_streaming_acompletion


def test_usage_recorded_exactly_once_when_streamed(monkeypatch) -> None:
    """Tier 2: record_llm fires exactly once per recorded_acompletion call,
    even though usage arrived split across streamed chunks (summed inside
    the chokepoint, not per-chunk)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_streaming_acompletion(chunk_witness))

    rec = _Recorder()
    response = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=rec, agent="a1",
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
    ))
    # Witness: all 4 scripted chunks were actually consumed — the usage
    # summation this test pins genuinely ran across chunks, not via the
    # fallback silently accepting a single flat response.
    assert chunk_witness == [1, 1, 1, 1], (
        f"expected all 4 scripted chunks consumed, got {len(chunk_witness)} — "
        "this must exercise the real streaming loop, not the fallback."
    )

    # ★ the gate: exactly ONE record_llm call tagged "main", not N (one per
    # chunk, which would show up as multiple "main" entries) and not zero
    # (usage silently dropped because it arrived late). Comparing the whole
    # derived purpose-sequence to a single-element list (not a bare `len(...)
    # == N` count) encodes the same cardinality claim behaviorally.
    assert [c["purpose"] for c in rec.calls] == ["main"], (
        f"expected exactly one record_llm call tagged 'main', got {rec.calls} — "
        "a per-chunk emit would double/multi-count cost."
    )
    usage = rec.calls[0]["usage"]
    assert isinstance(usage, TokenUsage)
    assert usage.prompt_tokens == 20
    assert usage.completion_tokens == 8
    # response.usage (post-reconstruction) carries the SAME summed values the
    # single record_llm call used — proving "summed here, once" rather than
    # "some other partial number recorded".
    assert response.usage.prompt_tokens == 20
    assert response.usage.completion_tokens == 8


def test_usage_recorded_once_for_whole_collect_baseline(monkeypatch) -> None:
    """Tier 2: baseline — the pre-existing non-streaming path already
    records exactly once (regression guard: ③a must not have changed this
    invariant for the fallback path either)."""
    async def _fake_whole(model: str, messages: list, **kw: Any) -> Any:
        assert not kw.get("stream")
        return litellm.ModelResponse(
            id="resp-2", created=1, model=model, object="chat.completion",
            choices=[{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }],
            usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_whole)
    rec = _Recorder()
    # o1-pro: real litellm capability data denies streaming outright, so this
    # exercises the whole-collect branch specifically.
    asyncio.run(recorded_acompletion(
        model="o1-pro", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=rec, agent="a1",
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
    ))
    assert [c["purpose"] for c in rec.calls] == ["main"]
