"""Tier 2: OS invariant — retry_loop is wired into the chat overflow path (#1125 Item 2).

PR-N6 built ``retry_loop`` (bounded adaptive-shrink overflow recovery) but the
chat router never called it — the overflow path ran a degraded compact-once and
hard-failed if the first compaction itself overflowed. axis-1's eager 30K
trigger masked the gap by keeping the middle small. This wires the real
mechanism in:

- ``ChatSession._decompose_history_for_retry`` exposes the
  head / raw_middle / tail / summary decomposition retry_loop consumes (the
  structural refactor the prior degraded path's comment punted as "follow-up").
- The router overflow handler hands that decomposition to ``retry_loop`` so it
  can fold raw_middle into the summary and monotonically shrink — the
  "never dead-end" continuity guarantee.

These pin the wiring's *data contract* (the decomposition is retry_loop-shaped
and the session's real engine/learner feed it). retry_loop's shrink mechanics
are covered by test_pr_n6_compaction_overflow_retry.py (Fake engine); the literal
except-block → retry_loop invocation is verified live via dogfood (the real
RouterLoop LLM overflow is not reconstructable in a unit test without the full
router stack). No mocks — real ChatSession + real CompactionEngine + real
retry_loop; ``main_call`` is a retry_loop parameter (a test async fn, as the
PR-N6 tests use), not a mocked collaborator.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from reyn.budget.budget import BudgetTracker, CostConfig
from reyn.chat.session import ChatMessage, ChatSession, _RouterUsageShim
from reyn.config import CompactionConfig
from reyn.events.state_log import StateLog
from reyn.llm.pricing import TokenUsage
from reyn.services.compaction.engine import retry_loop


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path: Path, *, head_size: int = 2, tail_size: int = 2) -> ChatSession:
    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        trigger_total_tokens=100_000,  # never auto-trigger in unit tests
        head_size=head_size,
        tail_size=tail_size,
        body_token_cap=1500,
    )
    return ChatSession(
        agent_name="default",
        agent_role="",
        output_language="en",
        budget_tracker=bt,
        state_log=state_log,
        compaction_config=cfg,
        snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
    )


def _push(session: ChatSession, role: str, text: str) -> None:
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=text, ts=_now()))


# ── _decompose_history_for_retry correctness ─────────────────────────────────


def test_decompose_slices_head_raw_middle_tail(tmp_path) -> None:
    """Tier 2: with len(turns) > head+tail, decomposition splits head/raw_middle/tail."""
    session = _make_session(tmp_path, head_size=2, tail_size=2)
    for i in range(8):
        _push(session, "user" if i % 2 == 0 else "assistant", f"turn-{i}")

    head, raw_middle, tail, summary = session._decompose_history_for_retry()

    assert [m["content"] for m in head] == ["turn-0", "turn-1"]
    assert [m["content"] for m in raw_middle] == ["turn-2", "turn-3", "turn-4", "turn-5"]
    assert [m["content"] for m in tail] == ["turn-6", "turn-7"]
    assert summary is None
    # head + raw_middle + tail is a lossless, in-order partition of the turns
    # (no dropped or duplicated turn — the concatenation reproduces the input).
    assert [m["content"] for m in head + raw_middle + tail] == [
        f"turn-{i}" for i in range(8)
    ]


def test_decompose_no_overlap_when_history_fits_window(tmp_path) -> None:
    """Tier 2: when len(turns) <= head+tail, everything is head — raw_middle/tail empty.

    This avoids the documented Q4-attractor duplication (head/tail overlap); there
    is nothing to elide, and retry_loop's shrink can still trim head.
    """
    session = _make_session(tmp_path, head_size=4, tail_size=4)
    for i in range(3):
        _push(session, "user", f"q-{i}")

    head, raw_middle, tail, summary = session._decompose_history_for_retry()

    assert [m["content"] for m in head] == ["q-0", "q-1", "q-2"]
    assert raw_middle == []
    assert tail == []
    assert summary is None


def test_decompose_extracts_structured_summary(tmp_path) -> None:
    """Tier 2: a persisted summary turn surfaces its structured dict (immutable base)."""
    session = _make_session(tmp_path, head_size=2, tail_size=2)
    structured = {"topic_arc": "greeting", "decisions": []}
    session.history.append(ChatMessage(
        role="summary",
        content="rendered text",
        ts=_now(),
        meta={"structured": structured, "covers_through_seq": 3},
    ))
    for i in range(6):
        _push(session, "user", f"m-{i}")

    _head, _raw_middle, _tail, summary = session._decompose_history_for_retry()
    assert summary == structured


def test_decompose_wire_shape_matches_build_history(tmp_path) -> None:
    """Tier 2: decomposed turns use the same wire serialisation as the normal path.

    retry_loop must rebuild the prompt the normal router send would have produced;
    a divergent wire shape (different role normalisation, missing tool fields)
    would make the recovery prompt differ from the live one.
    """
    session = _make_session(tmp_path, head_size=2, tail_size=2)
    for i in range(8):
        _push(session, "user" if i % 2 == 0 else "assistant", f"t-{i}")

    head, raw_middle, tail, _summary = session._decompose_history_for_retry()
    via_build = session._build_history_for_router()
    decomposed_contents = [m["content"] for m in head + tail]
    # _build_history_for_router (else-branch) = head + [summary bridge?] + tail;
    # no summary here, so its non-bridge turns are exactly head+tail.
    build_contents = [m["content"] for m in via_build]
    assert decomposed_contents == build_contents


def test_recovery_summary_bridge_matches_normal_path(tmp_path) -> None:
    """Tier 2: with a persisted summary, the recovery bridge text == the normal path's.

    The recovery prompt rebuilt by ``_router_main_call`` renders the structured
    summary via ``_render_summary_for_storage`` — the same renderer that produced
    the persisted ``summary.content`` the normal path (``_build_history_for_router``)
    uses for its bridge. So the summary bridge is byte-identical across the two
    paths (pinning the fix for the structured-vs-rendered divergence). retry_loop
    still receives the structured dict as its immutable fold base.
    """
    from reyn.chat.session import _render_summary_for_storage

    session = _make_session(tmp_path, head_size=2, tail_size=2)
    structured = {
        "topic_arc": "planning the trip",
        "decisions": ["book the tuesday flight"],
        "pending": ["confirm hotel"],
        "session_user_facts": [],
        "artifacts_referenced": [],
    }
    # Store the summary exactly as the controller does: content = rendered form.
    rendered = _render_summary_for_storage(structured)
    session.history.append(ChatMessage(
        role="summary",
        content=rendered,
        ts=_now(),
        meta={"structured": structured, "covers_through_seq": 2},
    ))
    for i in range(8):  # > head+tail → else-branch → bridge inserted
        _push(session, "user" if i % 2 == 0 else "assistant", f"t-{i}")

    # Normal path bridge content.
    normal = session._build_history_for_router()
    normal_bridge = next(
        m["content"] for m in normal
        if isinstance(m["content"], str) and m["content"].startswith("[summary")
    )
    # Recovery path: decomposition hands the structured dict; _router_main_call
    # renders it the same way → reproduce that bridge text here.
    _h, _rm, _t, summary_dict = session._decompose_history_for_retry()
    recovery_bridge = (
        "[summary of earlier conversation]\n" + _render_summary_for_storage(summary_dict)
    )
    assert recovery_bridge == normal_bridge


# ── wiring data contract: session decomposition feeds real retry_loop ────────


def test_session_decomposition_feeds_retry_loop(tmp_path) -> None:
    """Tier 2: the session's real decomposition + engine/learner drive retry_loop.

    Proves the wiring data contract end-to-end on the no-overflow path: the
    session's ``_decompose_history_for_retry`` output is retry_loop-shaped, the
    real CompactionEngine's budgets are accessible (non-None by construction),
    and the learner observes via the ``_RouterUsageShim``. retry_loop's shrink /
    engine.compact recovery is covered separately (PR-N6 Fake-engine tests).
    """
    session = _make_session(tmp_path, head_size=4, tail_size=4)
    for i in range(3):  # fits window → empty raw_middle → no engine.compact LLM call
        _push(session, "user", f"hi-{i}")

    head, raw_middle, tail, summary = session._decompose_history_for_retry()
    engine = session._compaction_controller._engine
    new_msg = {"role": "user", "content": "latest"}

    calls: list[dict] = []

    async def _main_call(*, SP, head, summary, tail, new_msg):
        calls.append({"head": head, "tail": tail, "summary": summary})
        return _RouterUsageShim(TokenUsage(prompt_tokens=123))

    shim = asyncio.run(retry_loop(
        SP=session._build_router_system_prompt(),
        head=head,
        summary=summary,
        raw_middle=raw_middle,
        tail=tail,
        new_msg=new_msg,
        cfg=session._compaction,
        model=session.model,
        engine=engine,
        learner=session._token_learner,
        main_call=_main_call,
    ))

    # retry_loop returned the main_call response (the session reads .usage back).
    assert isinstance(shim, _RouterUsageShim)
    assert shim.usage.prompt_tokens == 123
    # No-overflow path → main_call invoked exactly once; unpack enforces that
    # structurally (raises if calls has 0 or >1 entries) and binds the call.
    (only_call,) = calls
    assert [m["content"] for m in only_call["head"]] == ["hi-0", "hi-1", "hi-2"]


def test_router_usage_shim_exposes_usage(tmp_path) -> None:
    """Tier 2: _RouterUsageShim exposes .usage with prompt_tokens (retry_loop's learner contract)."""
    usage = TokenUsage(prompt_tokens=42, completion_tokens=7)
    shim = _RouterUsageShim(usage)
    assert shim.usage is usage
    assert shim.usage.prompt_tokens == 42
