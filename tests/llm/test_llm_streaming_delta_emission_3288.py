"""Tier 3a / Tier 1: #3288 ③b — the ``on_content_delta`` callback wiring inside
``recorded_acompletion`` / ``call_llm_tools`` (``src/reyn/llm/llm.py``).

③a (merged, #3304) added a capability-gated streaming loop that reconstructs
the SAME whole response internally — callers saw no behavioral difference.
③b's job is to carry the deltas OUT to a caller-supplied callback while
leaving that reconstruction (and everything callers already observed)
untouched. This file is the llm.py-level half of that; the audit-event/
transport half lives in ``tests/interfaces/test_agent_delta_audit_event_3288.py``.

#5261: ``on_content_delta`` now fires once per MERGED batch, not once per
raw provider chunk — the merge boundary is emergent from cooperative
scheduling (see ``_stream_and_reconstruct``'s own #5261 docstring/comments),
never a fixed count or fixed time window, so this file must never pin HOW
MANY calls happen or WHERE the split falls (CLAUDE.md: never pin algorithm-
level behaviour). What stays invariant regardless of how the scheduler
happens to batch this run: every batch's text concatenated in order still
reconstructs the whole response, the ``raw_chunk_count`` values sum to the
true number of content-bearing chunks, and each batch's
``first_arrival <= last_arrival``.

Uses a scripted ``litellm.acompletion`` replacement (a real async callable —
same idiom as ``test_llm_streaming_equivalence_3288.py``), never a
``unittest.mock`` double. ``chunk_witness`` proves REAL per-chunk consumption
drove the callback invocations (not that the whole-collect fallback silently
produced a plausible-looking result while never actually streaming).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import litellm
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage

from reyn.llm.llm import call_llm_tools, recorded_acompletion

_CONTENT = "Hello world"
_USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


class _DeltaRecorder:
    """#5261: records each MERGED ``on_content_delta`` call — (text,
    raw_chunk_count, first_arrival, last_arrival) — without assuming how
    many calls there are or where the merge boundary falls."""

    def __init__(self) -> None:
        self.calls: "list[tuple[str, int, datetime, datetime]]" = []

    def __call__(
        self, text: str, *, raw_chunk_count: int,
        first_arrival: datetime, last_arrival: datetime,
    ) -> None:
        self.calls.append((text, raw_chunk_count, first_arrival, last_arrival))

    @property
    def merged_text(self) -> str:
        return "".join(text for text, _, _, _ in self.calls)

    @property
    def total_raw_chunk_count(self) -> int:
        return sum(n for _, n, _, _ in self.calls)


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


def test_on_content_delta_fires_with_merged_text_covering_every_content_chunk(
    monkeypatch,
) -> None:
    """Tier 3a: ``on_content_delta`` fires — possibly split across several
    merged batches, per #5261's emergent-scheduling boundary — with the
    concatenation of every batch's text reconstructing the full response,
    and every content-bearing chunk accounted for exactly once across the
    ``raw_chunk_count`` values. Witnessed via real chunk consumption
    (``chunk_witness``), not merely that ``stream=True`` was requested (see
    ``_stream_and_reconstruct``'s non-aiter-able fallback)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas = _DeltaRecorder()
    result = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
        on_content_delta=deltas,
    ))

    # All 3 scripted chunks (2 content + 1 usage-only terminal) were really
    # consumed off the async generator.
    assert chunk_witness == [1, 1, 1]
    # The terminal usage-only chunk (empty delta.content) never entered a
    # batch — only the 2 content-bearing chunks are accounted for, however
    # they happened to be split/merged across calls.
    assert deltas.total_raw_chunk_count == 2
    assert deltas.merged_text == _CONTENT
    for _text, _count, first, last in deltas.calls:
        assert first <= last
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
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
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

    def _boom(text: str, **_kw: Any) -> None:
        calls.append(text)
        raise RuntimeError("display sink exploded")

    result = asyncio.run(recorded_acompletion(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
        on_content_delta=_boom,
    ))

    # The callback WAS invoked (and raised, and was swallowed) for at least
    # one batch — a vacuous "never called" would let this test pass without
    # exercising the guard at all. #5261: however many batches, their
    # concatenation still covers the whole content.
    assert "".join(calls) == _CONTENT
    assert result.choices[0].message.content == _CONTENT


def test_call_llm_tools_forwards_on_content_delta_through_to_the_stream(monkeypatch) -> None:
    """Tier 3a: ``call_llm_tools`` threads ``on_content_delta`` straight
    through its ``recorded_acompletion`` chokepoint call — the SAME callback
    contract, one layer up (no tools attached; ``has_tools=False`` still
    selects the streaming branch for a capable model)."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas = _DeltaRecorder()
    result = asyncio.run(call_llm_tools(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
        tools=[], on_content_delta=deltas,
    ))

    assert chunk_witness == [1, 1, 1]
    assert deltas.total_raw_chunk_count == 2
    assert deltas.merged_text == _CONTENT
    assert result.content == _CONTENT


def _make_fake_acompletion_no_content_deltas(chunk_witness: "list[int] | None" = None):
    """Co-vet recommendation (c): a real, scripted stand-in whose streaming
    branch yields chunks that NEVER carry ``delta.content`` (only role/
    tool_call/usage-only chunks) — simulating a provider chunk shape this
    parsing does not recognize. Content still reaches the FINAL reconstructed
    response via ``stream_chunk_builder``'s own accumulation from
    ``ChatCompletionDeltaToolCall``-free plain chunks is not exercised here;
    this fake targets ONLY the "delta never observed" silent-dead-mode guard,
    not stream≡whole equivalence (already covered elsewhere)."""

    async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
        if not kw.get("stream"):
            return litellm.ModelResponse(
                id="resp-2", created=1, model=model, object="chat.completion",
                choices=[{
                    "index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": _CONTENT},
                }],
                usage=dict(_USAGE),
            )

        def _chunk(delta: Delta, finish_reason: "str | None" = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-2", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            # A role-only chunk (no content) and a terminal usage-only chunk —
            # neither ever exposes delta.content, but chunks ARE consumed.
            pieces = [_chunk(Delta(role="assistant"))]
            last = _chunk(Delta(), finish_reason="stop")
            last.usage = Usage(**_USAGE)
            pieces.append(last)
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_acompletion


def test_silent_zero_delta_stream_logs_once_per_stream(monkeypatch, caplog) -> None:
    """Tier 1: co-vet fix (recommendation (c)) — when a stream produces at
    least one chunk but ``on_content_delta`` never fires (no chunk exposed
    ``delta.content``), exactly ONE WARNING log line is emitted for the
    whole stream (not per chunk) — the cheap observability guard against a
    silent functional-dead-mode where deltas quietly never happen for a
    given provider's chunk shape while L9's final text keeps working,
    masking it.

    #4805: captured at WARNING, not DEBUG, deliberately — the interactive
    CUI's own production floor discards anything below WARNING, so a
    DEBUG-level capture here would stay green even if this guard were
    invisible in real production (this guard used to be `logger.debug`,
    passing this same test under a DEBUG capture while genuinely never
    reaching a real operator — the exact defect #4805 exists to close).
    A guard whose firing nobody can see is the same as no guard."""
    import logging

    chunk_witness: list[int] = []
    monkeypatch.setattr(
        litellm, "acompletion", _make_fake_acompletion_no_content_deltas(chunk_witness),
    )

    deltas: list[str] = []
    with caplog.at_level(logging.WARNING, logger="reyn.llm.llm"):
        result = asyncio.run(recorded_acompletion(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
            on_content_delta=deltas.append,
        ))

    assert chunk_witness == [1, 1]  # both scripted chunks really consumed
    assert deltas == []  # the callback genuinely never fired
    guard_records = [
        r for r in caplog.records
        if "on_content_delta never fired" in r.message
    ]
    # Exactly one record for the whole stream — tuple-unpack (not a chunk
    # count of 1 or per-chunk repeats) raises if there are zero or more than
    # one, the behavioral idiom for "exactly once" (see testing.md's
    # len(...) == N → behavioral-assertion fix idiom).
    (only_guard_record,) = guard_records
    assert "2 chunk(s)" in only_guard_record.message
    # L9 is unaffected: the reconstruction still returns a valid response
    # object (content is empty here only because THIS fake's chunks never
    # carried any — the guard's job is observability, not content recovery).
    assert result.usage.prompt_tokens == _USAGE["prompt_tokens"]


def test_default_shaped_gemini_call_actually_enters_the_streaming_branch(monkeypatch) -> None:
    """Tier 3a: #3288 follow-up — a default-shaped call (tools +
    reasoning_effort attached, Gemini model — exactly what `RouterLoop`'s
    primary reply sends under reyn.yaml's default model classes) genuinely
    drives the streaming loop, witnessed via REAL chunk consumption
    (`chunk_witness`), not merely that `_streaming_enabled` returns True in
    isolation (a terminal-state assertion that would pass even if this call
    never reached the streaming branch at all — see verification-hazards.md
    §10).

    Historical note (#3288 comment thread, #3325): before #3325, reyn's own
    `/v1/responses` bridge (#1678) had no provider check and fired for EVERY
    tools+reasoning_effort call, silently rewriting this exact Gemini call
    to `responses/gemini-2.5-flash-lite`, which `_streaming_capability` could
    not recognize (unmapped in litellm's model map) — the default-config
    streaming regression this test guards against. #3325 fixed that with a
    provider gate; the #3288 follow-up investigation then deleted reyn's
    manual bridge entirely (litellm >= 1.89.3 delegates this natively), so
    reyn no longer rewrites the model string at all — this test now also
    covers that the model passes through UNCHANGED for a non-OpenAI model,
    which is a stronger guarantee than "correctly gated" was."""
    chunk_witness: list[int] = []
    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion(chunk_witness))

    deltas = _DeltaRecorder()
    result = asyncio.run(recorded_acompletion(
        model="gemini/gemini-2.5-flash-lite", messages=[{"role": "user", "content": "hi"}],
        purpose="main", recorder=None,
        model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
        on_content_delta=deltas,
        # The default-config primary-reply shape: tools attached AND
        # reasoning_effort set (an operator's own reyn.yaml model classes
        # commonly carry reasoning_effort for Gemini reasoning models —
        # #1654; #4349 removed reyn's own built-in catalog that used to
        # default this, so it's an explicit per-kwarg choice here).
        extra_kwargs={"tools": [{"type": "function"}], "reasoning_effort": "low"},
    ))

    # Real per-chunk consumption off the async generator — proves the
    # streaming branch was actually entered and driven, not just that the
    # capability query returned True.
    assert chunk_witness == [1, 1, 1]
    assert deltas.total_raw_chunk_count == 2
    assert deltas.merged_text == _CONTENT
    assert result.choices[0].message.content == _CONTENT


def test_silent_zero_delta_guard_does_not_fire_when_deltas_do(monkeypatch, caplog) -> None:
    """Tier 1: non-vacuity companion — the SAME guard does NOT fire on a
    normal stream where deltas DO arrive, proving the previous test's log
    line is specifically about the zero-delta case, not emitted on every
    stream unconditionally."""
    import logging

    monkeypatch.setattr(litellm, "acompletion", _make_fake_acompletion())

    with caplog.at_level(logging.DEBUG, logger="reyn.llm.llm"):
        asyncio.run(recorded_acompletion(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
            on_content_delta=lambda _t, **_kw: None,
        ))

    guard_records = [
        r for r in caplog.records
        if "on_content_delta never fired" in r.message
    ]
    assert guard_records == []


def _make_fake_acompletion_tool_calls_only(chunk_witness: "list[int] | None" = None):
    """#4805 review (lead-coder catch): a real, scripted stand-in whose
    streaming branch yields chunks carrying ONLY ``delta.tool_calls`` —
    never ``delta.content`` — reyn's own MOST COMMON round shape (the
    assistant calls a tool with no accompanying text). This never exposes
    a content delta either, same surface symptom as the genuine dead-mode
    case above, but for a completely different (and completely healthy)
    reason."""

    async def _fake_acompletion(model: str, messages: list, **kw: Any) -> Any:
        if not kw.get("stream"):
            return litellm.ModelResponse(
                id="resp-3", created=1, model=model, object="chat.completion",
                choices=[{
                    "index": 0, "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None, "tool_calls": []},
                }],
                usage=dict(_USAGE),
            )

        def _chunk(delta: Delta, finish_reason: "str | None" = None) -> ModelResponseStream:
            return ModelResponseStream(
                id="resp-3", created=1, model=model, object="chat.completion.chunk",
                choices=[StreamingChoices(index=0, delta=delta, finish_reason=finish_reason)],
            )

        async def _gen():
            pieces = [
                _chunk(Delta(role="assistant", tool_calls=[
                    {"index": 0, "id": "call_1", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"path"'}},
                ])),
                _chunk(Delta(tool_calls=[
                    {"index": 0, "function": {"arguments": ': "x"}'}},
                ])),
            ]
            last = _chunk(Delta(), finish_reason="tool_calls")
            last.usage = Usage(**_USAGE)
            pieces.append(last)
            for piece in pieces:
                if chunk_witness is not None:
                    chunk_witness.append(1)
                yield piece

        return _gen()

    return _fake_acompletion


def test_a_tool_only_round_never_trips_the_dead_mode_guard(monkeypatch, caplog) -> None:
    """Tier 1: accept-side — #4805 review's own catch. A tool-only round
    (the assistant calls a tool with no text — reyn's most common round
    shape, per lead-coder's live #4691 observation) legitimately never
    exposes ``delta.content`` on ANY chunk, the exact same surface symptom
    as the genuine dead-mode case ``test_silent_zero_delta_stream_logs_
    once_per_stream`` covers. Without excluding a chunk that carried
    ``tool_calls``, the WARNING-severity guard (#4805) would false-alarm
    on EVERY ordinary tool call — the same "fires on a healthy path too"
    defect this PR explicitly avoided for `message_handler.py`'s own
    case, caught here for `llm.py` on review."""
    import logging

    chunk_witness: list[int] = []
    monkeypatch.setattr(
        litellm, "acompletion", _make_fake_acompletion_tool_calls_only(chunk_witness),
    )

    with caplog.at_level(logging.WARNING, logger="reyn.llm.llm"):
        asyncio.run(recorded_acompletion(
            model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}],
            purpose="main", recorder=None,
            model_class=None,  # #4206 T1: not subject to the axis (pre-existing call)
            on_content_delta=lambda _t, **_kw: None,
        ))

    assert chunk_witness == [1, 1, 1]  # real chunk consumption, not vacuous
    guard_records = [
        r for r in caplog.records
        if "on_content_delta never fired" in r.message
    ]
    assert guard_records == [], (
        "a tool-only round must never trip the dead-mode guard — it is "
        "reyn's most common round shape, not a defect"
    )
