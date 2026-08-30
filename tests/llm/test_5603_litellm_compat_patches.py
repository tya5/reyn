"""Tier 2: #5603 — reyn's own local litellm patches
(``src/reyn/llm/_litellm_compat_patches.py``), migrated from a hand-placed
``site-packages`` file (#5568) into a repo module reyn's own startup
chokepoint imports.

Real litellm classes throughout (no mock, no stand-in) — each patch
function is exercised against the SAME synthetic-object reproduction
``tests/scaffold/test_5603_litellm_stream_recovery_defects.py`` uses for
the unpatched defect, confirming the PATCHED side actually recovers it.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest

from reyn.core.events.events import EventLog
from reyn.llm._litellm_compat_patches import (
    apply_all,
    apply_overflow_diagnosis,
    apply_stream_chunk_recovery,
)


@pytest.fixture(autouse=True)
def _reset_litellm_ready_after_reload():
    """Same reset ``tests/scaffold/test_5603_litellm_stream_recovery_
    defects.py`` establishes, for the same reason — every test in this
    file calls ``importlib.reload`` on a litellm submodule (via
    ``_fresh_bridge_handler_class``/``_fresh_responses_config_class``
    below), which replaces the class object a PRIOR test's own
    ``apply_*`` call already patched. Left unreset,
    ``litellm_bootstrap._litellm_ready`` staying ``True`` would make a
    LATER test/caller in the same process trust a stale "already
    applied" belief against the now-unpatched, freshly-reloaded class
    (six questions Q5 — a shared mutable object no test here bounds
    without this). See the scaffold file's own twin fixture for the
    full trace (lead-coder's own #4421-follow-up catch)."""
    yield
    import reyn.llm.litellm_bootstrap as litellm_bootstrap
    litellm_bootstrap._litellm_ready = False
    litellm_bootstrap._ready_registry.clear()


def _fresh_bridge_handler_class():
    from litellm.completion_extras.litellm_responses_transformation import handler as H
    importlib.reload(H)
    return H


def _fresh_responses_config_class():
    from litellm.llms.chatgpt.responses import transformation as T
    importlib.reload(T)
    return T


def test_stream_chunk_recovery_recovers_the_real_defect() -> None:
    """Tier 2: #5603(A) accept — after patching, the SAME synthetic
    scenario the scaffold test pins as "still broken unpatched" now
    returns the real, streamed content instead of an empty ``output``."""
    from litellm.types.llms.openai import ResponsesAPIStreamEvents

    H = _fresh_bridge_handler_class()
    apply_stream_chunk_recovery()
    cls = H.ResponsesToCompletionBridgeHandler

    class _FakeCompleted:
        def __init__(self, response: dict) -> None:
            self.response = response

    class _FakeChunk:
        type = ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE

        def model_dump(self) -> dict:
            return {
                "type": ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                "output_index": 0,
                "item": {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "recovered"}],
                },
            }

    class _FakeStream:
        def __init__(self) -> None:
            self.completed_response = _FakeCompleted({"output": []})
            self._hidden_params = None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield _FakeChunk()

    async def _drive():
        return await cls()._collect_response_from_stream_async(_FakeStream())

    result = asyncio.run(_drive())
    assert result.output == [
        {"type": "message", "content": [{"type": "output_text", "text": "recovered"}]},
    ], result.output


def test_stream_chunk_recovery_is_a_no_op_when_output_already_non_empty() -> None:
    """Tier 2: #5603(A) deny — a completed response that already carries
    real output (the ordinary, non-defective case) is returned
    byte-identical, unpatched or patched — this method never inspects,
    let alone overwrites, a non-empty result."""
    H = _fresh_bridge_handler_class()
    apply_stream_chunk_recovery()
    cls = H.ResponsesToCompletionBridgeHandler

    original_output = [{"type": "message", "content": [{"type": "output_text", "text": "already there"}]}]

    class _FakeCompleted:
        def __init__(self, response: dict) -> None:
            self.response = response

    class _FakeStream:
        def __init__(self) -> None:
            self.completed_response = _FakeCompleted({"output": list(original_output)})
            self._hidden_params = None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            return
            yield  # pragma: no cover — makes this an async generator with zero items

    async def _drive():
        return await cls()._collect_response_from_stream_async(_FakeStream())

    result = asyncio.run(_drive())
    assert result.output == original_output, result.output


def test_stream_chunk_recovery_is_idempotent() -> None:
    """Tier 2: #5603(A) — calling apply twice, then driving the real
    reproduction scenario, still returns exactly ONE recovered item —
    never through the private ``_reyn_5603_patched`` guard's own
    attribute directly (CLAUDE.md testing policy), through the public
    behavior a double-wrap would actually corrupt (two stacked wrappers
    would record the same chunk twice)."""
    from litellm.types.llms.openai import ResponsesAPIStreamEvents

    H = _fresh_bridge_handler_class()
    apply_stream_chunk_recovery()
    apply_stream_chunk_recovery()
    cls = H.ResponsesToCompletionBridgeHandler

    class _FakeCompleted:
        def __init__(self, response: dict) -> None:
            self.response = response

    class _FakeChunk:
        type = ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE

        def model_dump(self) -> dict:
            return {
                "type": ResponsesAPIStreamEvents.OUTPUT_ITEM_DONE,
                "output_index": 0,
                "item": {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "recovered"}],
                },
            }

    class _FakeStream:
        def __init__(self) -> None:
            self.completed_response = _FakeCompleted({"output": []})
            self._hidden_params = None

        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield _FakeChunk()

    async def _drive():
        return await cls()._collect_response_from_stream_async(_FakeStream())

    result = asyncio.run(_drive())
    assert result.output == [
        {"type": "message", "content": [{"type": "output_text", "text": "recovered"}]},
    ], (
        f"a double-apply must recover the SAME single item once, not "
        f"twice (a stacked double-wrap would) — got {result.output!r}"
    )


def test_overflow_diagnosis_converts_the_real_defect() -> None:
    """Tier 2: #5603(B) accept — a raw SSE truncated after
    response.created/response.in_progress (no terminal event, no output)
    is converted to a real ``ContextWindowExceededError`` by the patched
    ``transform_response_api_response`` — real litellm classification
    (``classify_llm_failure``) then correctly reads it as OVERFLOW."""
    import litellm

    from reyn.services.compaction.engine import LLMFailureClass, classify_llm_failure

    T = _fresh_responses_config_class()
    apply_overflow_diagnosis()
    cls = T.ChatGPTResponsesAPIConfig

    class _FakeRawResponse:
        status_code = 200
        text = (
            'data: {"type": "response.created", '
            '"response": {"status": "in_progress", "output": []}}\n\n'
            'data: {"type": "response.in_progress", '
            '"response": {"status": "in_progress", "output": []}}\n\n'
        )

    class _FakeLoggingObj:
        def post_call(self, **kwargs) -> None:
            pass

    with pytest.raises(litellm.ContextWindowExceededError):
        cls().transform_response_api_response(
            model="test-model", raw_response=_FakeRawResponse(), logging_obj=_FakeLoggingObj(),
        )

    try:
        cls().transform_response_api_response(
            model="test-model", raw_response=_FakeRawResponse(), logging_obj=_FakeLoggingObj(),
        )
    except litellm.ContextWindowExceededError as exc:
        assert classify_llm_failure(exc) is LLMFailureClass.OVERFLOW


def test_overflow_diagnosis_is_idempotent() -> None:
    """Tier 2: #5603(B) — calling apply twice does not double-wrap the
    method (the ``_reyn_5603b_patched`` guard)."""
    T = _fresh_responses_config_class()
    apply_overflow_diagnosis()
    cls = T.ChatGPTResponsesAPIConfig
    first = cls.transform_response_api_response
    apply_overflow_diagnosis()
    assert cls.transform_response_api_response is first


def test_apply_all_emits_one_audit_event_per_patch() -> None:
    """Tier 2: #5603 accept (architect's fail-safe ②) — both application
    attempts are visible on a real ``EventLog``, success or failure,
    never silent — the exact gap the original ``.pth``-based patch had
    (stderr only, nothing in reyn's own event trail)."""
    _fresh_bridge_handler_class()
    _fresh_responses_config_class()
    collected: "list" = []
    events = EventLog(subscribers=[lambda e: collected.append(e)])

    apply_all(events)

    fired = [e for e in collected if e.type == "litellm_compat_patch_applied"]
    patches = {e.data["patch"]: e.data["applied"] for e in fired}
    assert patches == {"stream_chunk_recovery": True, "overflow_diagnosis": True}, patches
