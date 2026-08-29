"""Tier 2: #5382's central selectivity witness for ``LLMStub``'s
``raise_for="compaction"`` mode (architect's witness ③, the one lead-coder
BLOCKING #5461 named — the unit-level tests in
``tests/dev/test_llm_stub_5103.py`` prove the discriminator's own logic
against a HAND-WRITTEN system-message string, which says nothing about
what a REAL router actually places there).

Driven through a REAL ``Session``/turn: ``CompactionController.
force_compact_now()`` (the same real, PUBLIC method
``ContextBudgetAdvisor``'s own pre-frame guard calls before every turn's
main call) is called directly here, catches the raise (emits
``compaction_failed``), and the SAME ``LLMStub`` instance is then driven
through a REAL turn — the main router call is NOT recognized as a
compaction call and must still complete normally.

Isolated in its OWN file, and driven via SEPARATE top-level
``asyncio.run(...)`` calls (never one ``await`` nested inside another
``async def`` this test defines itself) — both deliberately, and both
root-caused via direct instrumentation while building this test:

- A nested ``async def _drive(): await session._compaction_controller.
  force_compact_now()`` wrapper, run via ``asyncio.run(_drive())``, makes
  the SAME exception ``LLMStub._handle`` genuinely raises (confirmed via
  print instrumentation inside ``_handle`` — the raise statement executes)
  simply never reach ``force_compact_now``'s own ``except Exception`` —
  no ``compaction_failed`` event, no error, silently as if compact()
  never raised at all. Calling ``asyncio.run(session._compaction_
  controller.force_compact_now())`` DIRECTLY (the coroutine passed
  straight to ``asyncio.run``, no extra ``async def`` layer this test
  owns) does not have this problem — reproduced identically both inside
  and outside this file, both co-located with other tests and fully
  isolated. Root cause not fully chased into asyncio/litellm internals;
  treated as a real, narrow gotcha of THIS SDK boundary, not a defect in
  the raise-mode design itself. Multiple sequential ``asyncio.run(...)``
  calls (one per production await this test drives) is what avoids it.
- Also (a separate, now-moot-once-the-above-is-avoided finding, kept for
  the next reader): the SAME nested-``async def`` pattern additionally
  failed when this test was co-located inside ``test_llm_stub_5103.py``
  even outside the nesting issue — moved to its own file regardless.
"""
from __future__ import annotations

import asyncio

import pytest


@pytest.mark.llm_stub(raise_for="compaction", cause="rate_limit")
def test_raise_for_compaction_leaves_the_main_router_call_untouched_end_to_end(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: see module docstring for the full design and the two
    isolation rationales (separate file; separate top-level
    ``asyncio.run`` calls, no nested ``async def``)."""
    import reyn.llm.model_budget as _mb
    from reyn.core.events.state_log import StateLog
    from reyn.runtime.chat_message import ChatMessage
    from tests._support.agent_session import make_session

    monkeypatch.chdir(tmp_path)
    # A small, forced model window (mirrors test_5296_pr2_...'s own `t_max`
    # idiom) — the real (fallback ~128k-token) window would absorb every
    # candidate below into head/tail trimming, leaving zero candidates for
    # force_compact_now to ever call compact() over at all.
    monkeypatch.setattr(_mb, "get_max_input_tokens", lambda model, **kw: 3_000)
    session = make_session(
        agent_name="selectivity-agent",
        state_log=StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl"),
        snapshot_path=tmp_path / ".reyn" / "agents" / "selectivity-agent" / "state" / "snapshot.json",
    )

    # Many, sizeable turns — a handful of short ones is absorbed entirely
    # by head/tail trimming (_select_candidates finds zero candidates,
    # force_compact_now returns before ever calling compact()); measured
    # directly while building this test.
    for _i in range(50):
        session._append_history(ChatMessage(role="user", content=f"turn {_i} content " * 50))

    events: list = []
    session.subscribe_audit_events(events.append)

    asyncio.run(session._compaction_controller.force_compact_now())

    kinds = [e.type for e in events]
    assert "compaction_failed" in kinds, (
        f"test setup sanity: expected force_compact_now to have attempted "
        f"and caught a real compact() raise — got: {kinds!r}"
    )

    asyncio.run(session._put_inbox("user", {"text": "hi", "chain_id": "c1"}))
    result = asyncio.run(session.run_one_iteration())

    assert result is True
    # The main call's own response landed — it was NOT touched by the
    # compaction-only raise above.
    assert session.history[-1].role == "assistant", (
        f"expected the main router call to have completed normally after "
        f"the compaction call raised — last entry: {session.history[-1]!r}"
    )
