# scaffold: triggered_by="#5603 — reyn's own litellm Responses-API workarounds (src/reyn/llm/_litellm_compat_patches.py)"
# scaffold: removed_by="the litellm defects these tests reproduce are fixed upstream — see each test's own docstring"
"""Tier scaffold: #5603 — "does the upstream litellm defect this repo
works around still exist" tests, run against the RAW, UNPATCHED litellm
classes (never reyn's own patched versions).

## The red/green meaning is INVERTED here (read before touching)

- **GREEN = the defect still reproduces** = reyn's own workaround
  (`src/reyn/llm/_litellm_compat_patches.py`) is still needed. This is
  the expected, ONGOING state.
- **RED = the defect no longer reproduces** = litellm fixed it upstream.
  **This is GOOD NEWS, not a regression.** Remove
  `src/reyn/llm/_litellm_compat_patches.py`, its own call site in
  `litellm_bootstrap.py`, and THIS FILE, in the same PR (this file's own
  ``removed_by`` trigger).

Each test's own failure message repeats this so a reader who has not seen
this docstring is not misled by a red CI run.

## Why this file exists at all (architect's own design, #5603)

The mechanism that "notices litellm fixed something" and the mechanism
that "notices reyn's own patch silently stopped applying (upstream
RENAMED, not REMOVED, a symbol the patch depends on)" are architect's own
prescribed SAME ONE test, not two: this file tests the DEFECT ITSELF
(litellm's own observable behavior), never "did reyn's patch get
applied" — a version bump, a private-symbol rename, or a genuine upstream
fix are all, from this file's own point of view, indistinguishable
inputs that all either reproduce the defect (green) or don't (red). No
version-ceiling constant is declared anywhere (owner's own standing
instruction against an unjustified number with no rationale/config
knob) — this file itself IS the up-to-date-with-whatever's-installed
check the ceiling would have tried to approximate.

Both defects are reproduced directly against synthetic objects — no
network, no provider, no ``logging_obj`` — confirmed exactly as #5603's
own falsify point ① asked: ``importlib.reload`` on the owning module
gives back a genuinely fresh, unpatched class even after reyn's own
patch has already mutated the module-level class object earlier in this
same process (this repo's own test suite may have already called
``ensure_litellm_ready()`` — real, verified directly, not assumed).
"""
from __future__ import annotations

import asyncio
import importlib


def test_defect_a_stream_chunks_discarded_when_completed_output_empty() -> None:
    """Tier 2: #5603(A) — GREEN means STILL BROKEN (see this file's own module
    docstring for why red/green is inverted here).

    litellm's unpatched ``ResponsesToCompletionBridgeHandler.
    _collect_response_from_stream_async`` (the ``stream:false`` branch of
    the Responses→chat-completions bridge) discards every streamed chunk
    unconditionally (``async for _ in stream_iter: pass``) and returns
    ONLY whatever the terminal ``completed_response.response`` itself
    reports. When that terminal event's own ``output`` is empty — a real,
    already-delivered ``OUTPUT_ITEM_DONE`` chunk notwithstanding — the
    real content is silently discarded.

    ``importlib.reload`` gets a genuinely fresh, unpatched class even if
    reyn's own patch already mutated the module-level class object
    earlier in this same process (falsified directly, #5603's own point
    ①) — this test's own reproduction is therefore never accidentally
    exercising reyn's patched version instead of litellm's real one."""
    from litellm.completion_extras.litellm_responses_transformation import handler as H
    from litellm.types.llms.openai import ResponsesAPIStreamEvents

    importlib.reload(H)
    cls = H.ResponsesToCompletionBridgeHandler

    class _FakeCompleted:
        def __init__(self, response: dict) -> None:
            self.response = response

    class _FakeChunk:
        """A real ``OUTPUT_ITEM_DONE`` chunk carrying genuine content —
        exactly the shape ``record_output_item_chunk`` (litellm's own
        helper) can recover, if only the unpatched method looked at
        chunks at all."""

        type = ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE

        def model_dump(self) -> dict:
            return {
                "type": ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                "output_index": 0,
                "item": {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hello real content"}],
                },
            }

    class _FakeStream:
        def __init__(self) -> None:
            # The terminal event itself reports an EMPTY output — the
            # real incident's own shape (litellm's own known gap: the
            # terminal `response.completed` event does not always carry
            # everything the stream already delivered piece by piece).
            self.completed_response = _FakeCompleted({"output": []})
            self._hidden_params = None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield _FakeChunk()

    async def _drive() -> object:
        instance = cls()
        return await instance._collect_response_from_stream_async(_FakeStream())

    result = asyncio.run(_drive())

    assert result.output == [], (
        "GOOD NEWS, not a regression: litellm's raw "
        "_collect_response_from_stream_async no longer discards a real "
        "streamed chunk when the terminal event's own output is empty "
        "(got a non-empty output back from the UNPATCHED method — the "
        "defect #5603(A) works around appears fixed upstream). Remove "
        "src/reyn/llm/_litellm_compat_patches.py's "
        "apply_stream_chunk_recovery, its call site in "
        "litellm_bootstrap.py, and this test file, in the same PR."
    )


def test_defect_b_truncated_overflow_stream_yields_no_error_message() -> None:
    """Tier 2: #5603(B) — GREEN means STILL BROKEN (see this file's own module
    docstring for why red/green is inverted here).

    A provider ending a Responses-API SSE stream after only
    ``response.created``/``response.in_progress`` (a genuine context-
    length overflow whose error event carries no message — OpenAI's own
    public streaming-events spec documents ``response.created`` as
    status ``"in_progress"`` with an empty ``output``; the SAME shape is
    independently reported by other client implementations against this
    upstream behavior, maximhq/bifrost#4413) makes litellm's own PURE
    ``_extract_completed_response_from_sse`` (no network, no
    ``logging_obj`` — a real function call, not a mock) return
    ``(None, None)``: no completed response AND no error message. The
    caller then raises a generic ``OpenAIError`` carrying the ENTIRE raw
    SSE body as its message (real incident: 163,835 bytes) — reyn's own
    ``is_context_overflow_error``/``classify_llm_failure`` both fail to
    recognise this as a context overflow (no real
    ``ContextWindowExceededError`` type, no keyword anywhere in the raw
    SSE blob)."""
    from litellm.llms.chatgpt.responses import transformation as T

    importlib.reload(T)
    cls = T.ChatGPTResponsesAPIConfig

    # A genuine SSE stream that started and was cut off — no
    # response.completed / response.incomplete / response.failed, no
    # output_text / output_item event anywhere.
    truncated_sse = (
        'data: {"type": "response.created", '
        '"response": {"status": "in_progress", "output": []}}\n\n'
        'data: {"type": "response.in_progress", '
        '"response": {"status": "in_progress", "output": []}}\n\n'
    )

    instance = cls()
    completed, error_message = instance._extract_completed_response_from_sse(truncated_sse)

    assert completed is None and error_message is None, (
        "GOOD NEWS, not a regression: litellm's raw "
        "_extract_completed_response_from_sse no longer returns "
        "(None, None) for a stream truncated after response.created/"
        "response.in_progress with no terminal event (got "
        f"completed={completed!r}, error_message={error_message!r} from "
        "the UNPATCHED method — the defect #5603(B) works around appears "
        "fixed or changed upstream). Remove "
        "src/reyn/llm/_litellm_compat_patches.py's "
        "apply_overflow_diagnosis, its call site in "
        "litellm_bootstrap.py, and this test file, in the same PR."
    )
