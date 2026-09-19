"""Tier 2: #5888 -- the stage-2 mechanism (``/compact`` runs rung① spill
when fold selected zero candidates, PR #5932) actually fires at a
realistic ~75% context-window boundary, not merely the small stubbed-
budget shape ``test_5888_stage2_forced_spill.py`` already covers.

owner real-machine report the whole arc traces to (2026-09-07, verbatim,
issue #5888): "ctx 75% なのに下記メッセージ出るのも謎。ユーザは圧縮したいの
にできない。" — measured on that same session (issue #5888 comment
2026-09-07): a 2.8 MB ``exec`` tool result sat entirely inside the tail's
own keep-whole group (#2289, ``origin/main:src/reyn/services/compaction/
engine.py:2039`` ``_trim_groups`` -- "a single group alone over budget is
kept WHOLE"), so it was never a fold candidate (head/tail protection holds
it back BY DESIGN, ``origin/main:src/reyn/runtime/services/
compaction_controller.py:600`` ``_measure_and_select``) and never reached
by the reactive (413) ladder either (that only fires on a genuine
overflow). ``/compact`` reported "all protected" and could do nothing.

This file builds that EXACT shape at a boundary derived from the real
context window the compaction engine actually budgets against --
``compute_budgets``'s own ``T_max = get_max_input_tokens(model)``
(``origin/main:src/reyn/services/compaction/engine.py:844``) -- rather
than a hand-picked or stubbed ``ComputedBudgets`` literal, so the test
does not quietly stop meaning anything if a model/window changes: history
totals ~75% of the REAL, freshly-queried ``T_max``, with the tail as ONE
oversized tool-cycle group that ``compute_budgets``'s own real
``tail_budget`` (15% of ``main_pool`` under the default
``component_weights``, ``origin/main:src/reyn/config/chat.py:1075``)
cannot hold -- and a sanity assertion proves that relationship rather
than assuming it.

Discriminator: NOT "the reply did not say nothing to compact" (a weak,
string-shaped assertion -- #5888's own PR #5932 already rejected that
shape for something stronger). This test asserts, like #5932's own
``test_forced_compact_spills_a_protected_tail_group_when_fold_selected_
nothing``, on ``ForceCompactResult.spilled_bytes_freed`` -- the EXACT
character delta the wire drops when the oversized tail group is spilled
-- which is only nonzero if rung① spill genuinely ran against real,
naturally-arising (not stub-forced) zero fold candidates. A regression
that reverts stage 2's ``if not candidates:`` branch back to an
unconditional early return makes this assertion fail with
``spilled_bytes_freed == 0``, not a hang and not a passing-for-the-wrong-
reason green (see the strip-falsify note at the bottom of this docstring
block, executed for this PR).

What this test does NOT establish: it corroborates the mechanism at a
SYNTHETIC ~75%-of-T_max boundary built from plain repeated characters,
never the owner's own organically-grown session (real tool-call shapes,
real model, real accumulated turns, a real terminal). A green here is
evidence the code path fires at the right relative-size boundary; it is
not a substitute for -- and must not be read as closing -- the owner's
own real-machine confirmation #5888 is still waiting on.

It also reaches its 75% boundary under a NON-default token estimator.
``CompactionConfig(use_chars4_estimate=True)`` below selects the
``len(text) // 4`` estimator (``origin/main:src/reyn/config/chat.py:1093``
-- the field's own default is ``False``, meaning the shipped default is
litellm's real ``token_counter`` BPE tokenizer, not chars/4). This
matters here specifically because this test's oversized tail body is a
run of ONE REPEATED CHARACTER (``"z" * n``) -- a real BPE tokenizer
compresses a long repeated-character run far below one token per 4
characters, so counted under the SHIPPED default this same string would
land nowhere near 75% of T_max. chars/4 is not incidental plumbing here;
it is what makes a boundary reachable at all with a body this cheap to
construct (an equally-large body of genuinely varied text would reach
the same boundary under the real tokenizer too, but is far more
expensive to build and does not change what this test proves). So: the
"~75% of T_max" this test reaches is a REAL relative-size boundary
against a REAL T_max, but under the chars/4 estimator, not the shipped
default's actual token count -- the owner's "ctx 75%" is being
corroborated as a claim about the compaction MECHANISM firing at a
realistic relative-size boundary, not as a claim that this exact
synthetic body would count as 75% under production's real tokenizer.

Real ``CompactionController``/``ChatMessage``/``EventLog``/
``CompactionConfig``/``CompactionEngine`` throughout (the REAL engine is
constructed here, not a budget-stubbing subclass -- unlike
``test_5888_stage2_forced_spill.py``'s ``_NeverCalledEngine``, this test
needs ``compute_budgets``'s own real ``T_max`` query to derive its 75%
boundary, so stubbing the budgets would defeat the point). ``compact()``
is never called on this path regardless (zero fold candidates never
reaches ``_run_compaction``), so no engine subclass override is needed
here at all. Only ``spill_fn``/``decompose_for_retry`` are stand-ins --
the SAME real, working spill_fn contract shape (content+spillability-
shaped wire dicts in, ``(index, replacement)`` edits out) and
``decompose_history_for_retry()`` contract shape (head, raw_middle, tail,
summary, seq_by_id) ``test_5888_stage2_forced_spill.py``'s own docstring
already establishes as the acceptable class of fake for this suite (the
two collaborators standing in for the provider LLM/network boundary and
the real ``RouterHistoryBuffer.spill_turn_content`` durable write,
respectively) -- no ``MagicMock``/``AsyncMock``/``patch`` anywhere in
this file.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from reyn.config import CompactionConfig
from reyn.core.events.events import EventLog
from reyn.llm.model_budget import get_max_input_tokens
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.services.compaction_controller import CompactionController
from reyn.services.compaction.engine import CompactionEngine

# openai/gpt-4o: the same real, cataloged (no network call -- litellm's
# local model catalog) model string several sibling tests in this suite
# already construct a real CompactionEngine against (e.g.
# tests/core/test_4883_compaction_schema_validation.py's own _MODEL).
_MODEL = "openai/gpt-4o"


def _make_controller(
    *, history: "list[ChatMessage]", engine: CompactionEngine, events: EventLog,
) -> CompactionController:
    return CompactionController(
        event_log=events,
        config=CompactionConfig(use_chars4_estimate=True),
        history_from_disk=lambda after_seq: (
            [m for m in history if m.seq == 0 or m.seq > after_seq], False,
        ),
        latest_summary=lambda: None,
        compaction_engine_factory=lambda: engine,
        history_appender=history.append,
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


def _decompose_returning(
    head: "list[dict]", raw_middle: "list[dict]", tail: "list[dict]",
) -> "Callable[[], tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]]":
    """Same real ``decompose_history_for_retry()`` contract shape
    ``test_5888_stage2_forced_spill.py``'s own ``_decompose_returning``
    uses -- see this file's own module docstring for why this class of
    stand-in is acceptable here."""
    seq_by_id = {id(w): w["seq"] for face in (head, raw_middle, tail) for w in face}

    def _decompose() -> "tuple[list[dict], list[dict], list[dict], dict | None, dict[int, int]]":
        return head, raw_middle, tail, None, seq_by_id

    return _decompose


def _spill_once_per_call(
    spilled_faces: "list[list[dict]]",
) -> "Callable[..., list[tuple[int, dict]]]":
    """Same real, working spill_fn contract shape
    ``test_5888_stage2_forced_spill.py``'s own ``_spill_once_per_call``
    uses: spills the first non-``never`` candidate offered, records which
    face it was called with."""

    def _spill_fn(
        offered: "list[dict]", *, seq_by_id: "dict[int, int] | None" = None,
    ) -> "list[tuple[int, dict]]":
        spilled_faces.append(offered)
        for idx, item in enumerate(offered):
            if item.get("spillability") == "never":
                continue
            content = item.get("content", "")
            if content.startswith("[spilled]"):
                continue
            return [(idx, {**item, "content": "[spilled]"})]
        return []

    return _spill_fn


def test_forced_compact_spills_the_owners_shape_at_75pct_of_real_T_max() -> None:
    """Tier 2: #5888 -- at ~75% of the REAL, freshly-queried ``T_max``
    (not a stub), a history whose ENTIRE content is one small head plus
    one oversized tail tool-cycle group (the owner's own real-machine
    shape) selects zero fold candidates -- proven, not assumed, via the
    ``candidate_count == 0`` sanity assertion below -- and the stage-2
    rung① spill path still reaches and shrinks it.

    strip witness (executed, not reasoned, for this PR): reverting
    ``compaction_controller.py``'s ``if not candidates:`` branch to its
    pre-#5888-stage-2 form (unconditional early return, never attempting
    spill) makes ``spilled_count``/``spilled_bytes_freed`` both 0 here
    instead of matching the tail body's own exact size -- verified
    directly against this test file, restored after (see this PR's body
    for the exact edit/revert commands run).
    """
    events = EventLog()
    engine = CompactionEngine(_MODEL, events, CompactionConfig(use_chars4_estimate=True))
    budgets = engine.budgets
    t_max = get_max_input_tokens(_MODEL)

    # ~75% of the real T_max this engine actually budgets against --
    # derived, never a hardcoded token count (a model/window change
    # changes t_max, and this scales with it).
    target_total_tokens = int(t_max * 0.75)
    head_turn_tokens = 20  # small, comfortably inside head_budget
    head_messages = [
        ChatMessage(role="user", content="a" * (head_turn_tokens * 4), seq=1),
        ChatMessage(role="assistant", content="b" * (head_turn_tokens * 4), seq=2),
    ]
    tail_body_tokens = target_total_tokens - 2 * head_turn_tokens
    assert tail_body_tokens > int(budgets.tail_budget), (
        "test setup sanity: the tail tool result must alone exceed the "
        "REAL tail_budget this engine computed, or #2289 keep-whole has "
        "nothing to protect and this test is not exercising the owner's "
        f"shape (tail_body_tokens={tail_body_tokens}, "
        f"tail_budget={int(budgets.tail_budget)}, t_max={t_max})"
    )
    big_tool_result = "z" * (tail_body_tokens * 4)
    tail_messages = [
        ChatMessage(
            role="assistant", content="", seq=3,
            tool_calls=[
                {"id": "c1", "type": "function", "function": {"name": "exec", "arguments": "{}"}},
            ],
        ),
        ChatMessage(role="tool", content=big_tool_result, tool_call_id="c1", name="exec", seq=4),
    ]
    history = head_messages + tail_messages
    ctrl = _make_controller(history=history, engine=engine, events=events)

    decompose = _decompose_returning(
        [], [],
        [{"role": "tool", "content": big_tool_result, "spillability": "first_choice", "seq": 4}],
    )
    spilled_faces: "list[list[dict]]" = []

    result = asyncio.run(
        ctrl.force_compact_now(
            spill_fn=_spill_once_per_call(spilled_faces),
            spill_capability_present=True,
            selection="operator",
            decompose_for_retry=decompose,
        ),
    )

    assert result.candidate_count == 0, (
        "test setup sanity: at ~75% of real T_max with the whole oversized "
        "tool result kept whole in the tail, head+tail must consume the "
        "entire history -- there must be nothing left in the unprotected "
        f"middle to fold (got candidate_count={result.candidate_count})"
    )
    assert result.spilled_count == 1, f"expected exactly one spilled result; got {result.spilled_count}"
    assert result.spilled_bytes_freed == len(big_tool_result) - len("[spilled]"), (
        "expected the exact char delta the wire drops when the oversized "
        f"tail group is spilled; got {result.spilled_bytes_freed}"
    )
    assert result.spill_capability_present is True
    assert any(face and face[0].get("content") == big_tool_result for face in spilled_faces), (
        "the oversized tail tool result must have been the exact content "
        "offered to spill_fn"
    )
