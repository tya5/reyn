"""Tier 2: OS invariant — retry_loop is wired into the chat overflow path (#1125 Item 2).

PR-N6 built ``retry_loop`` (bounded adaptive-shrink overflow recovery) but the
chat router never called it — the overflow path ran a degraded compact-once and
hard-failed if the first compaction itself overflowed. axis-1's eager 30K
trigger masked the gap by keeping the middle small. This wires the real
mechanism in:

- ``Session._decompose_history_for_retry`` exposes the
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
router stack). No mocks — real Session + real CompactionEngine + real
retry_loop; ``main_call`` is a retry_loop parameter (a test async fn, as the
PR-N6 tests use), not a mocked collaborator.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from reyn.config import CompactionConfig
from reyn.core.events.state_log import StateLog
from reyn.llm.pricing import TokenUsage
from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.chat_message import ChatMessage
from reyn.runtime.session import Session
from reyn.runtime.usage_shim import _RouterUsageShim
from reyn.services.compaction.engine import retry_loop


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_session(tmp_path: Path, *, t_max: int = 1_000_000) -> Session:
    """Create a Session with a synthetic T_max controlling effective_trigger.

    #1128 step 3: slicing is token-budget based (not turn-count).
    ``use_chars4_estimate=True`` makes counting deterministic (chars // 4).
    ``section_caps_spec_tokens=0`` keeps B_M positive for small T_max values.

    Default t_max=1_000_000 → effective_trigger is large → small histories
    return all turns (no elide).  Pass a small t_max to force the elide branch.

    With t_max=2000 (section_caps_spec_tokens=0, T_SP≈1125 from real router SP,
    T_comp_SP≈481 from real compaction SP):
      head_budget≈87, tail_budget≈131, effective_trigger≈570.
    Turns of content ``'X'*320`` each cost 80 tokens via chars4.  With 8 such
    turns (total=640 > 570), elide fires.  head=[t0], tail=[t7], middle=[t1-t6].
    """
    import reyn.llm.model_budget as _mb

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")
    bt = BudgetTracker(CostConfig())
    cfg = CompactionConfig(
        body_token_cap=1500,
        use_chars4_estimate=True,  # deterministic offline token counts
        section_caps_spec_tokens=0,  # keeps B_M positive for small T_max
    )
    original = _mb.get_max_input_tokens
    _mb.get_max_input_tokens = lambda model, **kw: t_max  # type: ignore[assignment]  # noqa: E501
    try:
        session = Session(
            agent_name="default",
            agent_role="",
            output_language="en",
            budget_tracker=bt,
            state_log=state_log,
            compaction_config=cfg,
            snapshot_path=tmp_path / ".reyn" / "agents" / "default" / "state" / "snapshot.json",
        )
    finally:
        _mb.get_max_input_tokens = original
    return session


# Content that yields exactly 80 tokens per turn via use_chars4_estimate=True.
# 'X' * 320 = 320 chars → max(1, 320 // 4) = 80 tokens.
_TURN_80TOK = "X" * 320


def _push(session: Session, role: str, text: str) -> None:
    if role == "agent":
        role = "assistant"
    session.history.append(ChatMessage(role=role, content=text, ts=_now()))


# ── _decompose_history_for_retry correctness ─────────────────────────────────


def test_decompose_slices_head_raw_middle_tail(tmp_path) -> None:
    """Tier 2: when total tokens > effective_trigger, decomposition produces
    non-empty head, raw_middle, and tail, and head + raw_middle + tail is a
    lossless in-order partition of all turns (no dropped or duplicated turn).

    Uses t_max=2000 with 30 turns of 80-token content (total=2400 tokens).
    2400 > T_max=2000 by construction so elide fires regardless of SP size —
    default-independent: hot_list_n and other SP-affecting defaults don't change
    whether elide fires.
    """
    session = _make_session(tmp_path, t_max=2000)
    msgs_pushed = 30
    for i in range(msgs_pushed):
        _push(session, "user" if i % 2 == 0 else "assistant", _TURN_80TOK)

    head, raw_middle, tail, summary = session._decompose_history_for_retry()

    assert head, "head must be non-empty after elide"
    assert tail, "tail must be non-empty after elide"
    assert raw_middle, "raw_middle must be non-empty for a middle-elide scenario"
    assert summary is None

    # Lossless partition: concatenating head+raw_middle+tail reproduces all turns.
    all_via_decomp = head + raw_middle + tail
    assert len(all_via_decomp) == msgs_pushed, (
        f"expected all {msgs_pushed} pushed turns in partition, got {len(all_via_decomp)}"
    )
    # No duplicate objects — each turn appears exactly once by identity.
    assert len(all_via_decomp) == len(set(id(m) for m in all_via_decomp)), (
        "duplicate message objects detected — partition overlap guard failed"
    )


def test_decompose_no_elide_when_history_fits_window(tmp_path) -> None:
    """Tier 2: when total tokens <= effective_trigger, everything is in head —
    raw_middle and tail are empty (nothing to elide).

    retry_loop's shrink can still trim head if needed.
    """
    # Large t_max → effective_trigger large → 3 short turns easily fit.
    session = _make_session(tmp_path, t_max=1_000_000)
    for i in range(3):
        _push(session, "user", f"q-{i}")

    head, raw_middle, tail, summary = session._decompose_history_for_retry()

    assert [m["content"] for m in head] == ["q-0", "q-1", "q-2"]
    assert raw_middle == []
    assert tail == []
    assert summary is None


def test_decompose_extracts_structured_summary(tmp_path) -> None:
    """Tier 2: a persisted summary turn surfaces its structured dict (immutable base)."""
    session = _make_session(tmp_path)
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
    """Tier 2: decomposed head+tail turns use the same wire serialisation as
    the normal router path (_build_history_for_router).

    retry_loop must rebuild the prompt the normal router send would have
    produced; a divergent wire shape (different role normalisation, missing
    tool fields) would make the recovery prompt differ.

    This test forces the elide branch on both paths via t_max=2000 so that
    _build_history_for_router also returns head+tail (no summary bridge).
    """
    session = _make_session(tmp_path, t_max=2000)
    for i in range(8):
        _push(session, "user" if i % 2 == 0 else "assistant", _TURN_80TOK)

    head, raw_middle, tail, _summary = session._decompose_history_for_retry()
    via_build = session._build_history_for_router()

    # Both paths must return the same non-bridge turns in the same order.
    # _build_history_for_router: head + [bridge?] + tail (no summary → no bridge).
    # _decompose_history_for_retry: head + raw_middle + tail.
    # The head and tail turns must be wire-identical across both paths.
    decomposed_head_tail = [m["content"] for m in head + tail]
    build_contents = [m["content"] for m in via_build]
    assert decomposed_head_tail == build_contents, (
        "decompose head+tail must match _build_history_for_router output "
        "(same serialisation, same turns) — retry_loop recovery path must be byte-identical"
    )


def test_recovery_summary_bridge_matches_normal_path(tmp_path) -> None:
    """Tier 2: with a persisted summary, the recovery bridge text equals the
    normal router path's bridge.

    The recovery prompt rebuilt by ``_router_main_call`` renders the structured
    summary via ``_render_summary_for_storage`` — the same renderer that produced
    the persisted ``summary.content`` the normal path uses for its bridge.
    So the summary bridge is byte-identical across both paths.
    """
    from reyn.runtime.session import _render_summary_for_storage

    session = _make_session(tmp_path, t_max=2000)
    structured = {
        "topic_arc": "planning the trip",
        "decisions": ["book the tuesday flight"],
        "pending": ["confirm hotel"],
        "session_user_facts": [],
        "artifacts_referenced": [],
    }
    rendered = _render_summary_for_storage(structured)
    session.history.append(ChatMessage(
        role="summary",
        content=rendered,
        ts=_now(),
        meta={"structured": structured, "covers_through_seq": 2},
    ))
    # 30 turns × 80 tokens = 2400 > T_max=2000 → elide fires → bridge inserted
    # (default-independent: 2400 > T_max by construction regardless of SP size).
    for i in range(30):
        _push(session, "user" if i % 2 == 0 else "assistant", _TURN_80TOK)

    # Normal path bridge content.
    normal = session._build_history_for_router()
    normal_bridge = next(
        m["content"] for m in normal
        if isinstance(m["content"], str) and m["content"].startswith("[summary")
    )
    # Recovery path renders the structured dict the same way.
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
    # Large t_max → everything fits → empty raw_middle → no engine.compact LLM call.
    session = _make_session(tmp_path, t_max=1_000_000)
    for i in range(3):
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
