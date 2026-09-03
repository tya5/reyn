"""Tier 2: #5699 — owner real-machine incident: compaction stopped freeing
any space and the next turn crashed with a raw
``litellm.BadRequestError``/``code: context_length_exceeded``, in the
company environment (2026-09-03).

Two independent, sequentially-compounding defects, both introduced without
each other's knowledge (#5688/#5678 widened the live WINDOW; #5693 was
unrelated) — the owner's own symptom needed BOTH to be present:

1. **Window/fold parity** (this file's own name for it): #5678/#5688
   widened ``router_history_buffer.py``'s two window-building filters
   (``_elide_candidate_turns``/``decompose_history_for_retry``) to admit a
   ``role="system"`` entry declared ``Disclosure.MODEL`` — but the TWO
   candidate-selection filters that decide what ``/compact`` and the
   durable post-recovery fold (``router_loop_driver.py``'s own
   ``force_compact_now`` call after an ``UnrecoveredError``) can actually
   FOLD were never widened to match. Such an entry could enter the live
   window forever while staying permanently un-foldable through those two
   paths — accumulating without bound, "Nothing was compacted this pass"
   (owner's own words) on every ``/compact``.
2. **Missing structured signal** (:mod:`reyn.services.compaction.engine`):
   ``is_context_overflow_error`` checked ``status_code``, the litellm
   typed exception, and a keyword match on the stringified exception — but
   never the structured ``error.code`` field openai's own SDK already
   parses off the response body, even though it is exactly the kind of
   type-adjacent, definitive signal ``status_code == 413`` already is. A
   provider/proxy that flattens its typed error to a bare
   ``BadRequestError`` (this predicate's own docstring already names this
   case) AND whose free-text message does not happen to contain any of
   ``_CONTEXT_OVERFLOW_KEYWORDS`` fell all the way through undetected —
   ``is_shrinkable_overflow`` (the actual production gate
   ``router_loop_driver.py``'s 2 call sites use) returned False, so the
   raw exception propagated unrecovered instead of ever entering the
   shrink ladder.

Both are pinned here from the PUBLIC surface (``force_compact_now`` via its
own ``compaction_check`` audit-event, ``decompose_history_for_retry``,
``is_context_overflow_error``/``is_shrinkable_overflow``) — never a private
field. Real ``Session``/``CompactionController`` throughout for (1); the
constructed ``litellm.BadRequestError`` for (2) is the SAME shape this
issue's own investigation built and ran to first confirm the gap, not a
synthetic shortcut.
"""
from __future__ import annotations

from datetime import datetime, timezone

import litellm
import pytest

from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage, Disclosure, is_compaction_eligible
from reyn.services.compaction.engine import (
    classify_llm_failure,
    is_context_overflow_error,
    is_shrinkable_overflow,
)
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path, monkeypatch):
    """Same small-``T_max`` shape ``test_slash_compact_191.py`` uses to
    force non-empty candidates — see that file's own ``_make_session``
    docstring for the exact budget derivation."""
    import reyn.llm.model_budget as _mb

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2800)
    return make_session(
        agent_name="default",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=CompactionConfig(
            use_chars4_estimate=True, section_caps_spec_tokens=0,
        ),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


# ---------------------------------------------------------------------------
# Defect 1: window/fold parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_compact_now_folds_a_model_visible_backlog(tmp_path, monkeypatch):
    """Tier 2: the owner's own incident shape, reproduced — a history
    dominated by large ``role="system"``/``Disclosure.MODEL`` entries
    (the kind #5678's mid-turn-injection/template-push producers create)
    that ``force_compact_now`` must be able to select as candidates, not
    report zero ("Nothing was compacted this pass").

    Strip-falsify (run manually against this file's own PR, per this
    repo's no-committed-red-test convention — see
    ``test_4470_compaction_gap_does_not_clear_narrowing.py``'s own
    precedent for this pattern): reverting ``compaction_controller.py``'s
    own candidate filter to its pre-#5699 ``m.role in (...)``-only form
    (dropping ``or is_compaction_eligible(m)``) makes this test's own
    ``candidate_count`` assertion fail — 0, not > 0 — over the exact same
    fixture, confirmed directly against the real code before this test
    was written.
    """
    s = _make_session(tmp_path, monkeypatch)
    s._append_history(ChatMessage(role="user", content="hi", ts=_now()))
    for i in range(10):
        s._append_history(ChatMessage(
            role="system", content="x" * 4000, ts=_now(), disclosure=Disclosure.MODEL,
        ))
    s._append_history(ChatMessage(role="user", content="continue", ts=_now()))

    events: list = []
    orig_emit = s._audit_events.emit

    def _capture(kind, **kw):
        events.append((kind, kw))
        return orig_emit(kind, **kw)

    s._audit_events.emit = _capture

    await s._compaction_controller.force_compact_now()

    (check,) = [kw for kind, kw in events if kind == "compaction_check"]
    assert check["outcome"] == "forced_sync"
    assert check["candidate_count"] > 0, (
        "force_compact_now must select the MODEL-visible system entries as "
        "candidates — 0 candidates reproduces the owner's own 'Nothing was "
        "compacted this pass' incident"
    )


def test_window_and_fold_agree_on_which_entries_are_model_visible(tmp_path, monkeypatch):
    """Tier 2: deny-side — no entry can reach the live window
    (``decompose_history_for_retry``, the reactive overflow ladder's own
    candidate builder) without ALSO being selectable by
    ``force_compact_now``'s own filter. Driven by constructing entries of
    every role/disclosure combination and checking both filters agree via
    the SAME shared predicate, rather than by re-deriving the condition
    text a second time in this test (which would just be a transcription,
    testing nothing about whether the two SOURCES actually agree)."""
    s = _make_session(tmp_path, monkeypatch)
    entries = [
        ChatMessage(role="user", content="u", ts=_now()),
        ChatMessage(role="assistant", content="a", ts=_now()),
        ChatMessage(role="tool", content="t", ts=_now(), tool_call_id="tc1", name="x"),
        ChatMessage(role="system", content="internal", ts=_now(), disclosure=Disclosure.INTERNAL),
        ChatMessage(role="system", content="operator", ts=_now(), disclosure=Disclosure.OPERATOR),
        ChatMessage(role="system", content="model-visible", ts=_now(), disclosure=Disclosure.MODEL),
    ]
    for m in entries:
        s._append_history(m)

    _, raw_middle, _, _, _ = s._history_buffer.decompose_history_for_retry()
    window_contents = {(m["role"], m.get("content")) for m in raw_middle}

    # The SAME production predicate compaction_controller.py's own
    # candidate filter calls (chat_message.is_compaction_eligible) —
    # never a second, hand-typed transcription of the condition, which
    # would test only that this test's own copy agrees with itself.
    fold_eligible = {
        (m.role, m.content) for m in s.history if is_compaction_eligible(m)
    }

    for m in entries:
        in_window = (m.role, m.content) in window_contents
        in_fold = (m.role, m.content) in fold_eligible
        if in_window:
            assert in_fold, (
                f"role={m.role!r} disclosure={getattr(m, 'disclosure', None)!r} "
                "reached the window but is not fold-eligible — an entry the "
                "model can see but /compact can never retire"
            )


# ---------------------------------------------------------------------------
# Defect 2: structured error.code signal
# ---------------------------------------------------------------------------


def _proxy_flattened_context_overflow() -> "litellm.BadRequestError":
    """The exact shape this issue's own investigation built to first
    confirm the gap: a real ``litellm.BadRequestError`` (litellm's own
    class, not a stand-in) carrying the structured OpenAI ``error.code``
    field but a free-text ``message`` that contains NONE of
    ``_CONTEXT_OVERFLOW_KEYWORDS`` — the shape a proxy that strips the
    provider's own readable overflow message but preserves the
    machine-readable ``code`` would produce."""
    message = "Input rejected by upstream service."
    return litellm.BadRequestError(
        message=message, model="gpt-4", llm_provider="openai",
        body={"code": "context_length_exceeded", "message": message,
              "type": "invalid_request_error", "param": None},
    )


def test_is_context_overflow_error_reads_the_structured_code_field():
    """Tier 2: accept-side — a ``code: context_length_exceeded`` exception
    whose message carries no overflow keyword is still detected, via the
    structured field, not the string fallback.

    Strip-falsify (run manually): removing the ``.code`` check from
    ``is_context_overflow_error`` (leaving only status_code/type/keyword)
    makes this assertion fail — False, not True — confirmed directly
    against the real code before this test was written."""
    exc = _proxy_flattened_context_overflow()
    assert exc.code == "context_length_exceeded", "sanity: the field really is set"
    assert not any(
        kw in str(exc).lower()
        for kw in ("context", "token", "length", "limit", "too long", "too large")
    ), "sanity: the message string carries none of the keyword fallback's terms"
    assert is_context_overflow_error(exc)


def test_is_shrinkable_overflow_enters_the_ladder_for_a_code_only_signal():
    """Tier 2: the actual production gate (``router_loop_driver.py``'s 2
    call sites) — a code-only overflow signal must make it past
    ``classify_llm_failure``'s own FATAL/RETRYABLE checks AND
    ``is_context_overflow_error`` to reach the shrink ladder at all. This
    is the function whose False return, pre-fix, meant the raw exception
    propagated to the TUI unrecovered — the owner's own symptom."""
    exc = _proxy_flattened_context_overflow()
    assert classify_llm_failure(exc).value == "overflow"
    assert is_shrinkable_overflow(exc)


def test_a_message_with_the_keyword_but_a_different_code_is_still_detected():
    """Tier 2: non-regression — the pre-existing keyword fallback must
    still work for a shape carrying no ``code`` at all (or a
    ``code`` this allowlist does not name), so the #5699 fix is additive,
    not a replacement of the keyword path."""
    exc = litellm.BadRequestError(
        message="This model's maximum context length is 128000 tokens.",
        model="gpt-4", llm_provider="openai",
    )
    assert getattr(exc, "code", None) != "context_length_exceeded"
    assert is_context_overflow_error(exc)
