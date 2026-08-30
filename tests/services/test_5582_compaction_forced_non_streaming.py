"""Tier 2: #5582 — compaction's own LLM call is always non-streaming.

Owner proposal (verbatim, 2026-08-30): "compact はつねに stream false にす
る対応も入れた方が良いんじゃないの？". Before this fix, ``CompactionEngine.
_acompletion`` (engine.py) passed no ``stream_override`` at all to
``recorded_acompletion``, landing on ``_streaming_enabled``'s own
``override=None`` branch — catalog-driven, defaults to streaming for any
model the catalog doesn't explicitly mark non-streaming.

Two reasons streaming buys compaction nothing (lead-coder, #5582):
1. Compaction produces exactly ONE summary and never passes
   ``on_content_delta`` — nobody observes a delta from this call.
2. ``compact()`` is itself one of ``retry_loop``'s two overflow-ladder
   entry points (#5531 §9.6) — a stream this call does not need can
   misdiagnose the very ladder meant to recover it (#5581's own shape).

Scoped to compaction ONLY (lead-coder ruling, issuecomment-5467599134) —
the owner's own wording named ``compact`` specifically; extending this to
``main_call``'s in-ladder retries (real, user-visible delta display) was
split into a separate issue as a materially larger, costed decision.

Accept criteria (this session's own proposal, lead-coder-approved):
①' a compaction call (``purpose="compaction"``) is non-streaming even
  against a model the catalog marks streaming-capable.
②' (deny side) that same model, called OUTSIDE compaction with no
  override, still streams — proving this fix is compaction-scoped, not a
  blanket "reyn never streams" regression.

Real ``litellm.acompletion`` capture via a plain async function (not
``unittest.mock``) — same Tier 1 framework-boundary idiom
``tests/llm/test_llm_tools.py::test_stream_is_capability_gated_not_
forced_false`` already establishes for inspecting the ``stream`` kwarg
litellm actually receives (LLMReplay cannot see kwargs at this level).
Real ``CompactionEngine`` construction mirrors
``tests/services/test_4703_compaction_spend_visible.py``'s own pattern.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import litellm
import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.llm.llm import recorded_acompletion
from reyn.services.compaction.engine import CompactionEngine, HistoryChunkToCompact

# gemini-2.5-flash-lite: real litellm capability data says this model
# supports native streaming (same model test_llm_tools.py's own sibling
# test already relies on for that same fact) — the baseline this fix must
# override for compaction specifically.
_STREAMING_CAPABLE_MODEL = "gemini/gemini-2.5-flash-lite"

_SUMMARY_CONTENT = {
    "topic_arc": "arc", "new_turn_seqs": [1],
    "decisions": [], "pending": [], "session_user_facts": [], "artifacts_referenced": [],
}


def _resp(content: dict) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(content)))],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10),
    )


def _chunk() -> HistoryChunkToCompact:
    return HistoryChunkToCompact(
        messages=[{"role": "user", "text": "hi", "seq": 1}],
        section_token_caps={},
    )


def test_compaction_call_is_non_streaming_even_for_a_streaming_capable_model(monkeypatch):
    """Tier 2: #5582 accept ①' — compact()'s own LLM call never streams,
    even against a model litellm's real capability data marks
    streaming-capable."""
    captured: dict = {}

    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        captured.update(kw)
        return _resp(_SUMMARY_CONTENT)

    monkeypatch.setattr(litellm, "acompletion", _fake)

    engine = CompactionEngine(
        model=_STREAMING_CAPABLE_MODEL, events=EventLog(),
        cfg=CompactionConfig(use_chars4_estimate=True),
    )
    asyncio.run(engine.compact(_chunk(), covers_through=1))

    # Non-streaming path: recorded_acompletion's whole-collect branch never
    # sets a "stream" key at all (see llm.py — only the streaming
    # reconstruction path's OWN local stream_kwargs sets it). Either
    # "absent" or explicitly not True proves the streaming branch was
    # never entered.
    assert captured.get("stream") is not True, (
        f"compaction's litellm.acompletion call carried stream={captured.get('stream')!r} "
        "— compact() must never stream (#5582)."
    )


def test_the_same_model_still_streams_outside_compaction(monkeypatch):
    """Tier 2: #5582 accept ②' (deny side) — the SAME streaming-capable
    model, called with no stream_override (compaction's OWN prior default),
    still streams. Proves #5582's fix is compaction-scoped: it forces
    stream_override=False at ONE call site, not a blanket policy change
    that would also silently defeat #3288 ③a's own capability-gated
    streaming for every other caller."""
    async def _fake(model, messages, **kw):  # noqa: ANN001, ANN003
        raise _StreamProbe(kw.get("stream"))

    class _StreamProbe(Exception):
        def __init__(self, stream_value):
            self.stream_value = stream_value

    monkeypatch.setattr(litellm, "acompletion", _fake)

    with pytest.raises(_StreamProbe) as exc_info:
        asyncio.run(recorded_acompletion(
            model=_STREAMING_CAPABLE_MODEL,
            messages=[{"role": "user", "content": "hi"}],
            purpose="main",
            model_class=None,
            # No stream_override — the same "let the catalog decide" shape
            # compaction's own call had BEFORE #5582's fix.
        ))
    assert exc_info.value.stream_value is True
