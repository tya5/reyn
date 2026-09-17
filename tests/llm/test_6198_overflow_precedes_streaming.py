"""Tier 2: #6198's own accept④ — the structural premise app.py's
``_call_parent_key`` (and, separately, ``_streaming_replies``'s own
pre-existing keying) relies on: an INTERRUPTED overflow-retry round
never reaches the display, because litellm's own ``acompletion()``
raises before ``recorded_acompletion`` ever obtains a chunk stream to
drain — so ``on_content_delta`` (the ONLY producer of ``agent_delta``,
the sole source ``_streaming_replies``/the flowview stream is built
from) is never invoked for that round.

## Why this is pinned here, not left as a claim

#6198's own issue thread disclosed this structurally (reading
``litellm/main.py:690-698``: its own ``try``/``except`` wraps and
re-raises BEFORE ``return response``), never run-verified. This test
closes that gap for the ONE thing reyn's own code controls: whether
``recorded_acompletion`` calls ``on_content_delta`` before or after
obtaining ``chunk_stream``. It cannot verify litellm's OWN internal
timing (that would need a real provider or litellm's own test suite);
what it CAN verify, and does, is reyn's own call shape — ``on_content_
delta`` is structurally unreachable until AFTER ``await litellm.
acompletion(...)`` returns.

Real ``litellm.acompletion``, replaced with a real async function (not
a mock — the established idiom this module's own sibling tests use,
e.g. ``test_cache_token_capture_1772.py``) that raises immediately,
simulating a context-length-rejected request (the exact litellm
exception-mapping shape ``exception_mapping_utils.py`` produces from a
provider's 400).
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from reyn.core.events.events import EventLog, set_llm_request_event_log
from reyn.llm.llm import recorded_acompletion


def test_a_raising_acompletion_never_invokes_on_content_delta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: LOAD-BEARING — the exact premise #6198's own key relies
    on. strip: move the ``on_content_delta`` wiring to fire BEFORE the
    ``await litellm.acompletion(...)`` call (or have ``_stream_and_
    reconstruct`` retry/synthesize a delta on failure) -- this test
    would then see a non-empty ``deltas`` list and fail."""
    async def _raising_acompletion(**_kwargs):
        raise litellm.exceptions.ContextWindowExceededError(
            message="ContextWindowExceededError: test",
            model="openai/gpt-4o",
            llm_provider="openai",
        )

    monkeypatch.setattr(litellm, "acompletion", _raising_acompletion)
    log = EventLog()
    set_llm_request_event_log(log)

    deltas: "list[str]" = []

    def _on_content_delta(text: str, **_kw) -> None:
        deltas.append(text)

    with pytest.raises(litellm.exceptions.ContextWindowExceededError):
        asyncio.run(recorded_acompletion(
            model="openai/gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
            purpose="main",
            model_class=None,
            recorder=None,
            on_content_delta=_on_content_delta,
            stream_override=True,
        ))

    assert deltas == [], (
        "on_content_delta fired before litellm.acompletion's own "
        "exception propagated -- the interrupted-round-emits-nothing "
        "premise #6198's _call_parent_key relies on is broken"
    )
