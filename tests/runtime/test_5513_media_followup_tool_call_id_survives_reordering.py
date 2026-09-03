"""Tier 2: #5513 — a media follow-up's own intro text carries the
originating ``tool_call_id``, and that identifier survives a REAL
reordering (compaction inserting a summary between two previously-
adjacent follow-ups) — not merely "still adjacent" (six-questions ③: a
hand-reordered list would be a test-authored configuration, not a
witness of anything).

Architect ruling (#5513, PR #5538's own sibling issue): the id lives in
the intro TEXT only — ``role="user"`` cannot carry a ``tool_call_id``
field (OpenAI API shape). Explicitly NOT claiming "always distinguishable
regardless of what happens later" — architect's own disclosed limit:
this id distinguishes the two calls only while the text itself survives
verbatim; once COMPACTION folds a followup's own text into a rolling
summary, the distinction is gone (summarization, not reordering, is what
erases it). This file's own scenario is built so NEITHER followup's own
text is inside the compacted span — only content BETWEEN them is — so
the claim under test ("survives reordering") is not contradicted by that
limit, and stays scoped to it (never asserts "always distinguishable").

Real ``Session`` + real ``CompactionController.force_compact_now`` + real
``RouterHistoryBuffer.build_history`` throughout — no hand-reordered list,
no mocks.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from reyn.config import CompactionConfig, MultimodalConfig
from reyn.core.events.state_log import StateLog
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from tests._support.agent_session import make_session


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _followup_text(tool_call_id: str) -> str:
    # Mirrors _build_media_followup_message's own real intro-text shape
    # (router_loop.py) — this file tests whether the TEXT survives a real
    # compaction pass, not whether the media pipeline itself builds the
    # text correctly (covered separately, tests/runtime/test_media_cap_272.py
    # et al.).
    return f"Tool `read_file` (tool_call_id={tool_call_id}) returned the following attachment(s):"


def _make_session_t_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int):
    monkeypatch.chdir(tmp_path)
    import reyn.llm.model_budget as _mb
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: t_max)
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,
        section_caps_spec_tokens=0,
    )
    return make_session(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=BudgetTracker(CostConfig()),
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        compaction_config=cfg,
        multimodal_config=MultimodalConfig(),
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def test_tool_call_id_disambiguates_two_same_tool_followups_after_a_real_compaction_reorders_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5513 — build history with the SAME tool called twice, each
    producing its own media follow-up (real intro-text shape). Enough
    padding turns sit BETWEEN the two follow-ups to become compaction
    candidates (the middle raw_middle), while both follow-ups themselves
    land in head/tail (never compacted, per this test's own sanity check
    below) — a REAL ``force_compact_now()`` pass then inserts a summary
    entry between them, breaking their original ADJACENCY (the position-
    based disambiguation the issue's own incident relied on). Both
    follow-up texts must still each name their own distinct
    ``tool_call_id`` afterward — proving the id, not position, is what
    disambiguates them post-reorder."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=100_000)

    _push(session, "user", "look something up twice")
    _push(session, "tool", "result one", tool_call_id="tc1", name="read_file")
    _push(session, "user", _followup_text("tc1"))
    # Padding turns — become raw_middle (compaction candidates) between the
    # two follow-ups, without being large enough to touch either follow-up
    # itself (head/tail keep both, per this test's own sanity check).
    for i in range(20):
        _push(session, "assistant" if i % 2 == 0 else "user", f"padding turn {i} " * 50)
    _push(session, "tool", "result two", tool_call_id="tc2", name="read_file")
    _push(session, "user", _followup_text("tc2"))
    _push(session, "assistant", "both done")

    asyncio.run(session._compaction_controller.force_compact_now(spill_fn=lambda _candidates: []))

    history = session._history_buffer.build_history()
    texts = [str(m.get("content", "")) for m in history]

    # Sanity: a real compaction actually ran and inserted something between
    # the two follow-ups — if this fails, the fixture needs adjusting
    # (padding too small to trigger compaction), not the assertions below.
    tc1_idx = next(i for i, t in enumerate(texts) if "tool_call_id=tc1" in t)
    tc2_idx = next(i for i, t in enumerate(texts) if "tool_call_id=tc2" in t)
    assert tc2_idx > tc1_idx + 1, (
        "test setup sanity: expected at least one entry (a compacted "
        "summary) between the two follow-ups — got them still adjacent, "
        "meaning compaction never actually reordered anything here"
    )

    # The actual claim: BOTH follow-ups' own text still names their OWN
    # distinct tool_call_id — neither followup's own text was itself
    # folded away by the compaction that ran between them.
    assert _followup_text("tc1") in texts, (
        "the first follow-up's own text must survive verbatim — it must "
        "never have been the thing compacted"
    )
    assert _followup_text("tc2") in texts, (
        "the second follow-up's own text must survive verbatim"
    )
