"""Tier 1/2: #6174 — the no-compaction-controller fallback must not fabricate
a head/tail split, and an engine-construction failure must not be silently
relabeled as "no budgets".

Real defect (lead-coder, #6174, ``origin/main``): the fallback branch of
``resolve_effective_trigger_and_budgets`` used to split
``effective_trigger // 4`` evenly between ``head_budget``/``tail_budget`` —
a number nothing measured, and one that silently discarded
``component_weights``' own asymmetry (shipped default head=10% / tail=15%,
never 1:1 — ``CompactionEngine.compute_budgets``). Separately, the
``getattr(compaction_controller, "_engine", None)`` lookup swallowed ANY
``AttributeError`` — including one raised from INSIDE ``_engine``'s own
property body (it lazily builds a real ``CompactionEngine`` via
``compaction_engine_factory``, #3671 follow-up) — so a genuine engine-
construction failure looked identical to "no controller wired".

Census (e2e-coder, #6174, reported over broker): the fallback's
``compaction_controller is None`` branch is unreachable in production
(``Session._build_history_compaction_bundle`` patches a real controller
before any turn runs) but IS a deliberate, documented test-double shape
several existing tests rely on
(``test_live_model_budget_consumers_1752.py``'s own docstring: "so the
window comes straight from the model") — so the fix is a type change
(``None``, not a fabricated int) plus a loud guard at the one real
consumer, not a bare ``raise`` inside
``resolve_effective_trigger_and_budgets`` itself (that would break those
legitimate doubles).

Real instances throughout (CLAUDE.md mock ban) — ``RouterHistoryBuffer``/
``CompactionController`` construction mirrors
``tests/runtime/test_build_history_producer_calls_2939.py`` and
``tests/runtime/test_5765_compaction_range_covers_head_gap.py``'s own
minimal patterns.
"""
from __future__ import annotations

import pytest

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.runtime.services.router_history_buffer import (
    RouterHistoryBuffer,
    resolve_effective_trigger_and_budgets,
)


def _controller(factory) -> CompactionController:
    """Real CompactionController — only ``_engine``'s factory varies per test."""
    return CompactionController(
        event_log=EventLog(),
        config=CompactionConfig(),
        history_from_disk=lambda after_seq: ([], False),
        latest_summary=lambda: None,
        compaction_engine_factory=factory,
        history_appender=lambda m: None,
        # Production shape (session.py's own make_summary_message lambda,
        # #5765): covers_from_seq is a REQUIRED keyword-only argument.
        # Never actually called by either test below (both fail before
        # a real compaction pass would invoke it) — kept real-shaped
        # anyway, mirrors test_5765_compaction_range_covers_head_gap.py.
        make_summary_message=lambda rendered, structured, covers, *, covers_from_seq: ChatMessage(
            role="summary", content=rendered, seq=0,
            meta={
                "structured": structured,
                "covers_through_seq": covers,
                "covers_from_seq": covers_from_seq,
            },
        ),
        render_summary=lambda s: str(s),
    )


def test_no_controller_fallback_returns_none_not_a_fabricated_split():
    """Tier 1: no compaction_controller — head/tail budgets are None, not
    ``effective_trigger // 4``.

    Falsification: the pre-#6174 code returned two EQUAL, non-None ints
    here (``effective_trigger // 4`` for both); this asserts both the type
    (None) and that the old same-value pair is not silently resurrected.
    """
    effective_trigger, head_budget, tail_budget = resolve_effective_trigger_and_budgets(
        None, "openai/gpt-4", events=None,
    )
    assert effective_trigger == get_max_input_tokens("openai/gpt-4")
    assert head_budget is None
    assert tail_budget is None


def test_engine_construction_failure_propagates_not_silently_falls_back():
    """Tier 2: a real engine-construction failure must reach the caller, not
    be relabeled "no budgets" by the getattr-with-default lookup.

    Falsification: the pre-#6174 ``getattr(compaction_controller, "_engine",
    None)`` caught this exact ``AttributeError`` (raised from INSIDE the
    property body, not from a missing attribute) and fell through to the
    fallback branch — this test would see a clean ``(effective_trigger,
    None, None)`` return instead of the exception below.
    """
    def _factory():
        raise AttributeError("boom -- simulated engine-construction failure")

    controller = _controller(_factory)
    with pytest.raises(AttributeError, match="boom"):
        resolve_effective_trigger_and_budgets(controller, "openai/gpt-4", events=None)


def test_decompose_history_for_retry_raises_loudly_rather_than_fabricate_a_split():
    """Tier 2: ``decompose_history_for_retry``'s own overflow branch is the
    one real consumer of head_budget/tail_budget (router_history_buffer.py
    :1593-1594) — when they are unknown (no controller) and the history
    genuinely overflows, it must raise, never invent a split to trim by.

    Falsification: the pre-#6174 code passed the fabricated
    ``effective_trigger // 4`` straight into ``trim_head``/``trim_tail``
    here — this test would see a clean 5-tuple return instead of
    ``RuntimeError``.
    """
    model = "openai/gpt-4"  # small real window (8K) — cheap to overflow
    trigger = get_max_input_tokens(model)
    # chars4 estimate: ~4 chars/token — sized well past the trigger, not
    # just past it (a tight margin risked a flaky pass/fail on wire-dict
    # wrapping overhead).
    big_content = "x" * (trigger * 20)
    history = [ChatMessage(role="user", content=big_content, seq=1)]

    buf = RouterHistoryBuffer(
        history_fn=lambda: history,
        compaction=CompactionConfig(use_chars4_estimate=True),
        compaction_controller=None,
        model_fn=lambda: model,
        events=None,
        media_store=None,
        router_host=None,
        universal_wrappers_enabled=False,
        non_interactive=False,
    )

    with pytest.raises(RuntimeError, match="no compaction_controller is wired"):
        buf.decompose_history_for_retry()
