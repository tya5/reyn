"""Tier 2: #4472 — compaction's candidate input moves from ``Session.
history`` (resident, byte-cap-evictable — #4387/#4468) to reading
``history.jsonl`` DIRECTLY (durable, never residency-gated). Structural
fix for #4470/#4471: residency now has no influence on what compaction
considers, so the "gap" those issues had to detect and skip around cannot
occur anymore (see ``test_4470_compaction_gap_does_not_clear_narrowing.py``
for the rewritten #4470 scenario proving eviction no longer blocks
coverage).

This file covers the NEW risk architect's #4472 design review named
explicitly (point ①), which a naive "just read the raw file" fix would
have reintroduced: ``self.history`` is not the raw file — it is already
filtered to the conversation's ACTIVE branch (``_active_branch_history``'s
own job, #2360's WAL-anchor filtering). Reading ``history.jsonl`` directly
without that same filter would fold an ABANDONED-branch turn (post-rewind)
into a summary and advance ``covers_through_seq`` past it — a turn that
was never really part of the conversation the user is looking at, leaked
into a "covered" claim that can never be revisited.

Fixed via ``Session._durable_active_history_after``, which applies the
SAME ``_filter_visible_on_active_branch`` helper ``_active_branch_history``
uses (factored out so both consumers can never silently diverge), over a
durable-store read instead of the resident array.

Real ``Session`` + real ``StateLog`` + the real ``checkout`` reset-record
primitive (mirrors ``tests/core/test_conversation_rewind_2360.py``'s own
seam) + real ``CompactionController``/engine (only ``litellm.acompletion``
monkeypatched to a scripted summary, matching
``tests/runtime/test_slash_compact_191.py``'s established discipline).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import litellm

from reyn.config import CompactionConfig
from reyn.core.events.snapshot_generations import checkout
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from tests._support.agent_session import make_session

_SUMMARY_JSON = json.dumps({
    "topic_arc": "compacted summary of older turns",
    "decisions": [], "pending": [],
    "session_user_facts": [], "artifacts_referenced": [],
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path, monkeypatch) -> tuple[Session, StateLog]:
    import reyn.llm.model_budget as _mb

    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 2800)
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    session = make_session(
        agent_name="default",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=state_log,
        compaction_config=CompactionConfig(
            use_chars4_estimate=True,
            section_caps_spec_tokens=0,
        ),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )
    return session, state_log


def _script_compaction_llm(monkeypatch, captured_new_turn_seqs: list) -> None:
    """Records the seqs the engine was actually asked to summarize (via the
    input_chunk it builds internally) is not directly observable through
    litellm's own call args, so this records the RENDERED prompt text
    instead — the abandoned-branch marker string's absence/presence in it
    is the real witness (see the test's own assertion)."""
    async def _fake_acompletion(model, messages, **kw):  # noqa: ANN001, ANN003
        # Record what the compactor's own LLM call actually saw, so the
        # test can assert on real prompt CONTENT, not an internal seq list.
        captured_new_turn_seqs.append(json.dumps(messages))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=_SUMMARY_JSON))]
        )
    monkeypatch.setattr(litellm, "acompletion", _fake_acompletion)


async def _turn(session: Session, state_log: StateLog, text: str) -> int:
    await state_log.append("step_completed")
    session._append_history(ChatMessage(role="user", content=text, ts=_now()))
    return session.history[-1].meta["wal_seq"]


def test_compaction_never_folds_an_abandoned_branch_turn_into_the_summary(
    tmp_path, monkeypatch,
):
    """Tier 2: architect's #4472 review, point ① — the exact regression a
    naive "read the raw file" fix would have reintroduced. 10 turns
    appended, a rewind hides turns 4-10, 3 new turns land on the new
    active branch — compaction must fold in ONLY the active-branch
    content (1-3 + the 3 new ones), never the abandoned turns' text, even
    though they're fully readable from history.jsonl."""
    monkeypatch.chdir(tmp_path)
    s, state_log = _make_session(tmp_path, monkeypatch)
    prompt_calls: list = []
    _script_compaction_llm(monkeypatch, prompt_calls)

    # #1128 step 3 token-budget sizing (matches test_slash_compact_191.py's
    # own _make_session/_populate: t_max=2800, use_chars4_estimate=True ->
    # head_budget~74/tail_budget~112 tokens; "x"*4000 = 1000 tokens/turn,
    # so each individually-oversized turn lands as exactly 1 head/1 tail
    # turn, leaving the rest as real middle candidates).
    pad = "x" * 4000
    anchors = []
    for i in range(1, 11):
        anchors.append(
            asyncio.run(_turn(s, state_log, f"ABANDONED-MARKER-turn-{i} {pad}"))
        )

    asyncio.run(checkout(state_log, target_seq=anchors[2]))  # hide turns 4-10

    for i in range(11, 15):
        asyncio.run(_turn(s, state_log, f"active-turn-{i} {pad}"))

    result = asyncio.run(s._compact_now_for_op())
    assert result["summarized_turns"] > 0, "sanity: compaction must have genuinely run"

    prompt_text = " ".join(prompt_calls)
    assert "ABANDONED-MARKER-turn-4" not in prompt_text, (
        "an abandoned-branch turn's content must NEVER reach the "
        "summarizer's own prompt -- it is not part of the conversation "
        "the user is looking at"
    )
    assert "ABANDONED-MARKER-turn-10" not in prompt_text, (
        "same check for the last abandoned turn, not just the first"
    )
    # Sanity: the active-branch content the compactor SHOULD see actually
    # made it into the prompt -- proves this isn't vacuously true because
    # nothing at all reached the summarizer.
    assert "ABANDONED-MARKER-turn-1" in prompt_text or "ABANDONED-MARKER-turn-2" in prompt_text, (
        "sanity: turns 1-3 (still active, never abandoned) must have "
        "reached the summarizer -- if neither made it in, this test isn't "
        "distinguishing 'filtered correctly' from 'nothing got through at all'"
    )


def test_active_branch_history_still_agrees_with_compaction_after_a_rewind(
    tmp_path, monkeypatch,
):
    """Tier 2: accept-side — the durable-store read must not DIVERGE from
    what the LLM-facing view (_active_branch_history) considers visible.
    After the same rewind as above, both consumers must report the exact
    same active-branch turn set for the overlapping seq range."""
    monkeypatch.chdir(tmp_path)
    s, state_log = _make_session(tmp_path, monkeypatch)

    anchors = []
    for i in range(1, 8):
        anchors.append(asyncio.run(_turn(s, state_log, f"turn-{i}")))

    asyncio.run(checkout(state_log, target_seq=anchors[2]))  # hide turns 4-7

    visible_resident = [m.content for m in s._active_branch_history()]
    visible_durable = [m.content for m in s._durable_active_history_after(0)]

    assert visible_resident == visible_durable == ["turn-1", "turn-2", "turn-3"], (
        "the resident (_active_branch_history) and durable "
        "(_durable_active_history_after) branch-visibility filters must "
        "agree exactly -- they share the same underlying filter helper, "
        "so a divergence here means that sharing broke"
    )
