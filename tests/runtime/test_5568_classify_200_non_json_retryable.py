"""Tier 2: #5568 — ``classify_llm_failure`` classifies an HTTP 200 whose
body is not parseable as JSON as ``RETRYABLE``, structurally, BEFORE
``is_context_overflow_error``'s own keyword-string fallback ever runs.

Owner's real-machine incident (reyn-self ``coder-brown``, 2026-08-30):
litellm's ``OpenAIResponsesAPIConfig.transform_response_api_response``
does ``raw_response.json()`` inside a bare ``try/except Exception`` and,
on failure, raises ``OpenAIError(message=raw_response.text,
status_code=raw_response.status_code)`` — an HTTP 200 (the request
genuinely succeeded at the transport layer) wrapping the ENTIRE raw
response body (a raw SSE stream the upstream proxy returned despite
``stream: false``) as the exception's own message. Because that body can
coincidentally contain an overflow-shaped keyword, the pre-#5568
fallthrough to OVERFLOW let this reach ``is_context_overflow_error``'s
keyword fallback and enter the shrink ladder — repeatedly halving and
re-sending a 9M-character history against a cause no amount of shrinking
can fix (ADR-0044 I2: never apply an irreversible remedy to a reversible
cause).

architect's own ruling (issue #5568): reyn's correction is classification
ONLY — never teaching litellm to accept the malformed response (that
would make the proxy's own contract violation permanently invisible, the
Q3 "does the repair destroy the evidence" band violation). The upstream
proxy fix (owner's own hand) is out of scope for this PR.

Reachability witness (architect's own explicit requirement, closing the
exact hole #5603 exposed — a patched function production never actually
reaches still shows green): this file's own accept test does NOT call
``classify_llm_failure`` directly. It drives a REAL ``Session`` +
``RouterLoop`` turn end to end and fakes the TRANSPORT boundary
(``litellm.acompletion``, monkeypatched — same idiom
``test_5582_compaction_forced_non_streaming.py`` already establishes for
inspecting/controlling what litellm receives/returns) so
``call_llm_tools`` → ``recorded_acompletion`` → ``litellm.acompletion``
all run for real, exactly as architect's own acceptance text names the
path (``call_llm_tools`` → ``recorded_acompletion``) — the classification
result is read off the SAME observable behavior (0 compaction attempts,
no ``router_context_overflow_detected``, a typed error surfaces) that
``test_5577_unify_overflow_classification_arms.py`` and
``test_5256_quota_not_context_overflow.py`` already use for this exact
family of defect (misclassification at the production except-clause
chain), never from calling the classifier in isolation.
"""
from __future__ import annotations

import asyncio

import litellm
import pytest

from tests._support.agent_session import make_session
from tests._support.events import collect_events, settle


class _FakeOpenAI200NonJsonError(Exception):
    """Real, scripted stand-in for the exact exception shape
    ``OpenAIResponsesAPIConfig.transform_response_api_response`` raises
    when a ``stream: false`` request receives an SSE body instead of
    JSON — an HTTP 200 status carrying the ENTIRE raw stream text as its
    own message (architect's own #5568 trace, quoted above)."""

    def __init__(self, sse_body: str) -> None:
        super().__init__(sse_body)
        self.status_code = 200


# A real-shaped SSE body carrying an ``error`` frame whose own text
# happens to contain an overflow-suggestive keyword ("context window") —
# deliberately NOT an overflow-keyword-free body: this is what makes the
# strip-falsify meaningful. Without this fix, `classify_llm_failure`
# falls through to OVERFLOW, and `is_context_overflow_error`'s own
# keyword fallback (`_CONTEXT_OVERFLOW_KEYWORDS` includes "context") then
# matches THIS text, so the misclassification actually reproduces — a
# keyword-free SSE body would pass this test's own assertions even
# WITHOUT the fix (confirmed directly: reverting the fix and rerunning
# with a keyword-free body stayed green — the exact false-negative this
# body avoids). Genuinely non-JSON (fails json.loads) either way.
_SSE_BODY = (
    'data: {"type": "response.created", '
    '"response": {"status": "in_progress", "output": []}}\n\n'
    'data: {"type": "response.in_progress", '
    '"response": {"status": "in_progress", "output": []}}\n\n'
    'data: {"type": "error", '
    '"error": {"message": "Your input exceeds the context window"}}\n\n'
)


def _drain_outbox(session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


def test_production_path_200_non_json_classifies_retryable_not_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5568 accept + reachability witness.

    A real Session/RouterLoop turn whose ONLY faked boundary is
    ``litellm.acompletion`` (never ``classify_llm_failure`` itself, never
    ``call_llm_tools``, never ``is_shrinkable_overflow`` — every real
    function on the actual production path runs). Never enters the shrink
    ladder (0 compaction attempts, no ``router_context_overflow_detected``)
    and the session survives with a typed error surfaced — never silently
    burning real calls shrinking a cause no shrink can fix."""
    session = make_session(agent_name="classify_200_nonjson_test")
    collected = collect_events(session)

    call_count = 0

    async def _fake_acompletion(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        raise _FakeOpenAI200NonJsonError(_SSE_BODY)

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async def _drive() -> None:
        # Must not raise — the generic catch-all keeps the session alive.
        await session._handle_inbox_text("hi", chain_id="chain-5568-1")
        await settle(session)

    asyncio.run(_drive())

    # ① never shrinkable: exactly one real litellm.acompletion call — a
    # shrink retry, or the compaction ladder's own fold call, would have
    # made a second/third one.
    assert call_count == 1, (
        f"expected exactly 1 litellm.acompletion call (no shrink retry "
        f"entered), got {call_count}"
    )

    kinds = [e.type for e in collected]
    assert "router_context_overflow_detected" not in kinds, (
        "an HTTP 200 + non-JSON body must never be classified as context "
        "overflow — that is precisely #5568's own real-machine incident"
    )
    assert not [e for e in collected if e.type == "compaction_started"], (
        "no compaction pass may have been attempted — this cause is "
        "unshrinkable by construction (a transport/protocol failure, not "
        "an input-size one)"
    )

    # The exception genuinely reached the generic catch-all's own P6
    # instrument, typed correctly — not swallowed, not misreported.
    terminated = [e for e in collected if e.type == "router_loop_terminated_by_exception"]
    assert terminated, "the exception must reach the generic catch-all's own P6 instrument"
    assert terminated[0].data["error_type"] == "_FakeOpenAI200NonJsonError"

    msgs = _drain_outbox(session)
    error_msgs = [m for m in msgs if m.kind == "error"]
    assert error_msgs, "the operator must see something — not a silent end"


def test_production_path_genuine_overflow_still_classifies_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5568 accept-2 (sibling, ruling-out over-narrowing) — a
    REAL context-length overflow, through the SAME production path, must
    still enter the shrink ladder exactly as before. Without this test,
    the accept side above could pass trivially with an implementation
    that stopped classifying ANYTHING as overflow."""
    session = make_session(agent_name="classify_200_nonjson_overflow_sibling_test")
    collected = collect_events(session)

    async def _fake_acompletion(*args, **kwargs):
        raise litellm.ContextWindowExceededError(
            message="This model's maximum context length is 128000 tokens",
            model="gpt-5.6-luna", llm_provider="openai",
        )

    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)

    async def _drive() -> None:
        await session._handle_inbox_text("hi", chain_id="chain-5568-2")
        await settle(session)

    asyncio.run(_drive())

    kinds = [e.type for e in collected]
    assert "router_context_overflow_detected" in kinds, (
        "a genuine context-overflow-shaped exception must still enter "
        "the shrink ladder — #5568's own fix must not have become "
        "'never classify as overflow'"
    )


# ── deny: a 200 whose body IS valid JSON is untouched (byte-identical) ──


def test_classify_llm_failure_200_with_valid_json_message_stays_overflow(
) -> None:
    """Tier 2: #5568 deny — the new branch is scoped EXACTLY to "200 +
    body fails json.loads"; a 200-status exception whose message DOES
    parse as JSON must classify exactly as it did before this fix
    (falls through to OVERFLOW, unchanged) — proving this is not a
    blanket "any 200 is retryable" widening.

    Unit-level on the classifier itself (not the reachability-witness
    concern above): this deny case tests the FUNCTION's own scoping, not
    whether production reaches it — a 200-status exception with a
    well-formed JSON body is not itself a real incident this PR
    addresses; #5568's own defect is specifically the non-JSON case."""
    from reyn.services.compaction.engine import LLMFailureClass, classify_llm_failure

    class _Fake200JsonError(Exception):
        def __init__(self, msg: str) -> None:
            super().__init__(msg)
            self.status_code = 200

    exc = _Fake200JsonError('{"error": {"message": "context length exceeded"}}')
    assert classify_llm_failure(exc) is LLMFailureClass.OVERFLOW, (
        "a 200-status exception whose message DOES parse as JSON must "
        "stay on its pre-#5568 classification path — this fix's own "
        "branch must never fire for it"
    )
