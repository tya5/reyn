"""Tier 2: #5633 (lead-coder BLOCKING on PR #5661) — a failed ``compact()``
call reached through ``RecoveryLadder``/``retry_loop``'s own internal fold
(``_stage_fold``, ``engine.py``) now emits ``compaction_failed``, closing
the ``compaction_started`` marker the TUI shows.

Real incident this closes: before this PR, ``compaction_failed`` was
emitted from exactly ONE site, ``CompactionController.force_compact_now``
(``compaction_controller.py``) — the CONTROLLER path. The LADDER path
(this file's own scenario) called the SAME ``CompactionEngine.compact()``
but on failure only classified/re-raised the exception
(``_classify_and_wrap_compact_failure``) — never emitted
``compaction_failed`` anywhere. Since #5475, ``compaction_started`` emits
from ``compact()`` itself and fires for BOTH callers — so the ladder path
COULD show "[⟳ compacting N turns]" (this PR's own #5633 marker) with no
event ever closing it. lead-coder's own finding: deleting
``test_compaction_started_is_not_surfaced`` (this PR's earlier revision)
was only half-justified without this witness.

Driven through a REAL Session/RouterLoopDriver/RecoveryLadder, exactly
like ``test_5531_summary_reaches_first_main_call.py``'s own harness (same
``_make_session_t_max``-shaped fixture, same many-small-turns overflow
shape that forces retry_loop to actually reach ``_stage_fold``) — but
with ``LLMStub``'s ``raise_for="compaction"`` mode (#5382) so every
compact() call raises for real, instead of stubbing a success.
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
from tests._support.events import collect_events, settle


class _AlwaysOverflowingLoop:
    """Every attempt reports the wire as still oversized — with every
    ``compact()`` call ALSO raising (via the ``llm_stub`` marker below),
    neither reduction axis in ``_run_with_shrink_and_byte_reduction`` can
    ever progress, so the ladder exhausts and re-raises for real."""

    async def run(self, *, user_text: str, history: "list[dict]") -> "object | None":
        raise _FakeStatusError("request too large", status_code=413)


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _push(session, role: str, text: str, **kw) -> None:
    session._append_history(ChatMessage(role=role, content=text, ts=_now(), **kw))


def _make_session_t_max(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, t_max: int):
    """Mirrors test_5531_summary_reaches_first_main_call.py's own builder:
    default fold_persist_policy/max_shrink_iterations — compaction must
    actually be reachable (attempted), not merely skipped for lack of
    candidates."""
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


@pytest.mark.llm_stub(raise_for="compaction", cause="rate_limit")
def test_a_failed_ladder_path_compact_call_emits_compaction_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: see module docstring. compaction_started fires (retry_loop
    genuinely reached _stage_fold and called compact()), compact() raises
    for real (LLMStub's raise_for="compaction" mode), and — the fix under
    test — compaction_failed now fires too, closing the marker."""
    session = _make_session_t_max(tmp_path, monkeypatch, t_max=2800)

    for i in range(1, 6):
        _push(session, "user" if i % 2 else "assistant", f"turn-{i}", seq=i)
    # Many small turns whose SUM overflows (mirrors #5531/#5367's own
    # fixture) — nothing to spill onto individually, so retry_loop must
    # actually reach the fold stage to make any progress at all.
    texts = [f"turn-{i}:" + ("X" * 320) for i in range(6, 36)]
    for i, text in enumerate(texts, start=6):
        _push(session, "user" if i % 2 else "assistant", text, seq=i)

    events = collect_events(session)
    loop = _AlwaysOverflowingLoop()

    with pytest.raises(Exception):
        asyncio.run(
            session._loop_driver._run_with_shrink_and_byte_reduction(
                loop, "continue please", chain_id="c1",
            )
        )
    asyncio.run(settle(session))

    kinds = [e.type for e in events]
    assert "compaction_started" in kinds, (
        "test setup sanity: expected retry_loop to have genuinely "
        f"reached _stage_fold/compact() — got event kinds: {kinds!r}"
    )
    assert "compaction_failed" in kinds, (
        "expected the failed compact() call to close the "
        "compaction_started marker with compaction_failed — before "
        "#5633's fix, the ladder path (retry_loop's own internal fold) "
        f"never emitted compaction_failed anywhere; got: {kinds!r}"
    )
