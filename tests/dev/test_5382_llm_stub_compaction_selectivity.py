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

Isolated in its OWN file, and reads the collected event list only after
``await settle(session)`` — both deliberately.

The settle() requirement is #4961 C (architect ruling, see
``tests/_support/events.py``'s own module docstring), not a new finding
of this test's own: an earlier revision of this test read a raw
``events`` list synchronously, right after an ``await``-triggering call,
with no yield in between — a shape #4961 C already names as broken
(dispatch to a collected list runs on a background consumer task; a
synchronous read immediately after the triggering await can race it and
miss the event). Root-caused with a targeted experiment (architect/
lead-coder, #5461 review): a synchronous, non-event marker placed INSIDE
``force_compact_now``'s own ``except Exception`` block was present after
a run that otherwise showed 0 ``compaction_failed`` events — proving the
exception WAS caught (control flow is fine) and the miss was purely
event-DELIVERY, exactly #4961 C's shape, not a distinct control-flow
defect in ``LLMStub``'s raise mode.

Isolated in its OWN file for an unrelated, still-open reason: the SAME
test also silently failed the same way when co-located inside
``test_llm_stub_5103.py``, even with ``settle()`` correctly reasoned
about — moved here regardless of the settle() fix.
"""
from __future__ import annotations

import asyncio

import pytest

from tests._support.events import settle


@pytest.mark.llm_stub(raise_for="compaction", cause="rate_limit")
def test_raise_for_compaction_leaves_the_main_router_call_untouched_end_to_end(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: see module docstring for the full design and the settle()
    rationale (#4961 C)."""
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

    async def _drive() -> bool:
        await session._compaction_controller.force_compact_now(spill_fn=lambda _candidates: [])
        # #4961 C: dispatch to `events` runs on a background consumer
        # task — settle() before the synchronous read below, right at
        # the spot that depends on delivery having already happened.
        await settle(session)
        kinds = [e.type for e in events]
        assert "compaction_failed" in kinds, (
            f"test setup sanity: expected force_compact_now to have "
            f"attempted and caught a real compact() raise — got: "
            f"{kinds!r}"
        )
        await session._put_inbox("user", {"text": "hi", "chain_id": "c1"})
        return await session.run_one_iteration()

    result = asyncio.run(_drive())

    assert result is True
    # The main call's own response landed — it was NOT touched by the
    # compaction-only raise above.
    assert session.history[-1].role == "assistant", (
        f"expected the main router call to have completed normally after "
        f"the compaction call raised — last entry: {session.history[-1]!r}"
    )
