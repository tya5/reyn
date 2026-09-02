"""Tier 2: #5578 — a SUCCESSFUL overflow-recovery's own fold (retry_loop's
internal ``ChatSummary``, engine.py) is persisted to history and the
compaction watermark advanced, WITHOUT a new compaction LLM call.

architect ruling (#5578, issue comment): the recovery's own fold already
happened — the model already answered against it — so the correct action
is to RECORD it (``CompactionController.persist_recovery_summary``), never
to call ``force_compact_now`` again (that would spend a second LLM call
re-folding already-folded content and, per #5296, trigger a NEW
irreversible compaction step this ruling deliberately avoids).

Real ``Session`` + real ``RouterLoopDriver``/``RouterHistoryBuffer``/
``CompactionController``/``CompactionEngine`` throughout (no MagicMock) —
same harness ``test_5498_retry_loop_covers_zero_never_persisted.py`` and
``test_5296_pr2_byte_reduction_same_turn_retry.py`` already use.
``LLMStub`` (no ``raise_for``) supplies the one real LLM call retry_loop's
own internal ``compact()`` makes; a content-driven fake ``loop`` (fails
only the very first call, exactly like #5498's own retry-path scenario)
drives the router's main call so the FIRST attempt overflows (entering
recovery) and every attempt after it — including retry_loop's own
``main_call`` — succeeds.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.dev.testing.llm_stub import LLMStub
from tests._support.events import settle
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _ContentDrivenLoop,
    _make_spill_session,
    _push,
)


def _drive_one_recovering_turn(
    session, *, marker_text: str = "OVERSIZED_MARKER_5578",
) -> "list[dict]":
    """Push enough content that ``t_max`` forces ``raw_middle`` non-empty,
    then drive one turn whose FIRST call overflows (entering
    ``_run_with_shrink``'s recovery path) and every call after succeeds —
    so retry_loop's own internal ``compact()`` genuinely runs and folds,
    and the recovered ``main_call`` genuinely succeeds using that fold.
    Returns the events collected during the drive."""
    budgets = session._compaction_controller._engine.budgets
    head_tokens = budgets.effective_trigger + budgets.tail_budget + 1_000
    _push(session, "user", "H" * (head_tokens * 4))
    _push(session, "tool", marker_text, tool_call_id="tc-marker", name="big_tool")
    per_filler_tokens = max(1, budgets.tail_budget // 4)
    for _i in range(4):
        _push(session, "user", "F" * (per_filler_tokens * 4))

    _seen_first = {"done": False}

    def _fail_only_first(history: list, user_text: str) -> bool:
        if _seen_first["done"]:
            return False
        _seen_first["done"] = True
        return True

    loop = _ContentDrivenLoop(_fail_only_first)

    events: "list" = []
    session._compaction_controller._events.add_subscriber(lambda e: events.append(e))

    stub = LLMStub()
    stub.install()
    try:
        asyncio.run(
            session._loop_driver._run_with_shrink(
                loop, "continue please", chain_id="c1",
            )
        )
        asyncio.run(settle(session))
    finally:
        stub.restore()
    return events


# ── accept: a successful recovery persists the fold and advances the watermark ──


def test_successful_recovery_persists_summary_and_advances_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5578 accept — watermark moved AND the summary's own
    covers_through_seq is real (nonzero) — the exact thing #5498's own
    comment warned would silently persist as 0 if this were wired wrong."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, fold_persist_policy="next_turn",
    )
    events = _drive_one_recovering_turn(session)

    persisted = [
        e for e in events
        if e.type == "recovery_summary_persisted" and e.data.get("outcome") == "persisted"
    ]
    assert persisted, (
        f"expected a recovery_summary_persisted(outcome=persisted) event; "
        f"got: {[(e.type, e.data.get('outcome')) for e in events if e.type == 'recovery_summary_persisted']!r}"
    )
    assert persisted[0].data.get("covers_through_seq", 0) > 0, (
        "the persisted covers_through_seq must be a REAL (nonzero) seq — "
        "retry_loop's own ChatSummary.covers_through_seq is structurally "
        "0 on this path (#5498); this proves the driver derived the real "
        "value via seq_by_id rather than trusting that field directly"
    )

    watermark = session._history_buffer.compaction_watermark()
    assert watermark > 0, "the durable compaction watermark must have advanced"
    assert watermark == persisted[0].data["covers_through_seq"], (
        "the watermark read back from history must equal what was persisted"
    )

    summaries = [m for m in session.history if m.role == "summary"]
    assert summaries, "a real summary entry must exist in history"
    assert summaries[-1].meta.get("structured", {}).get("covers_through_seq") == watermark

    # No NEW compaction LLM call was made to produce this — only the ONE
    # retry_loop's own internal fold, never a second `_run_compaction`
    # pass (`compaction_completed` is `_run_compaction`'s own signal;
    # this path must never emit it).
    assert not [e for e in events if e.type == "compaction_completed"], (
        "persist_recovery_summary must never trigger a NEW compaction "
        "pass (force_compact_now) — the fold already happened"
    )


def test_successful_recovery_shrinks_the_next_turns_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5578 accept, 2nd half — the NEXT turn's own
    ``build_history()`` starts above the new watermark: the original
    filler turns folded into the summary no longer appear verbatim, so
    the built payload is genuinely smaller than the raw pushed content."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, fold_persist_policy="next_turn",
    )
    _drive_one_recovering_turn(session)
    watermark = session._history_buffer.compaction_watermark()
    assert watermark > 0, "sanity: the recovery must have advanced the watermark"

    total_pushed = len(session.history)  # every durable turn, folded + new_msg's own reply if any
    built = session._history_buffer.build_history()

    # build_history() never returns role=="summary" directly — the
    # persisted summary rides a synthetic assistant-role BRIDGE turn
    # (RouterHistoryBuffer.build_history's own docstring: "its content
    # rides the synthetic bridge turn instead"), attached unconditionally
    # once watermark > 0.
    bridge_turns = [
        t for t in built
        if t.get("role") == "assistant"
        and "[summary of earlier conversation]" in str(t.get("content", ""))
    ]
    assert bridge_turns, (
        "the next turn's own build_history() must carry the persisted "
        "summary's own bridge turn — it is durable, not a same-call-only "
        "artifact"
    )
    assert len(built) < total_pushed, (
        f"build_history() returned {len(built)} turns, not fewer than the "
        f"{total_pushed} durably pushed — the watermark-covered filler "
        f"turns folded into the summary must not ALSO still appear raw: "
        f"{built!r}"
    )


# ── deny: an ordinary, non-overflow turn never touches the watermark ────────


def test_ordinary_turn_never_moves_the_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5578 deny (sibling of the accept side) — a turn that never
    overflows never enters retry_loop at all, so no
    recovery_summary_persisted event fires and the watermark stays at 0.
    Without this sibling, the accept side alone could pass in a world
    where the watermark moves unconditionally on every turn."""
    session = _make_spill_session(
        tmp_path, monkeypatch, fold_persist_policy="next_turn",
    )
    _push(session, "user", "hello")

    events: "list" = []
    session._compaction_controller._events.add_subscriber(lambda e: events.append(e))

    class _NeverOverflowLoop:
        async def run(self, *, user_text: str, history: list) -> "object | None":
            return None

    asyncio.run(
        session._loop_driver._run_with_shrink(
            _NeverOverflowLoop(), "hi again", chain_id="c1",
        )
    )
    asyncio.run(settle(session))

    assert not [e for e in events if e.type == "recovery_summary_persisted"], (
        "a normal, non-overflow turn must never invoke the #5578 persist "
        "path at all — retry_loop is only entered on a real overflow"
    )
    watermark = session._history_buffer.compaction_watermark()
    assert watermark == 0, "the watermark must not move for an ordinary turn"


# ── fold_persist_policy="never" leaves the #5578 path untouched (pre-existing #5498 guard) ──


def test_fold_persist_policy_never_disables_the_persist_path_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5578 — gated the SAME way the pre-existing terminal-failure
    force_compact_now call already is (fold_persist_policy == "next_turn").
    ``"never"`` is the pre-existing #5498 self-test's own configuration —
    this confirms the NEW persist path respects the same opt-out, not
    only the old force_compact_now call."""
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, fold_persist_policy="never",
    )
    events = _drive_one_recovering_turn(session)

    assert not [e for e in events if e.type == "recovery_summary_persisted"], (
        "fold_persist_policy='never' must suppress the #5578 persist path too"
    )
    watermark = session._history_buffer.compaction_watermark()
    assert watermark == 0


# ── truncate-falsify (CLAUDE.md hard rule: recovery-feature PRs need one) ───


def test_persisted_summary_survives_wal_truncation_past_its_own_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5578 truncate-falsify — set X (persist the recovery
    summary) -> truncate history.jsonl (this repo's own durable store,
    the WAL-equivalent CLAUDE.md's hard rule names) past X's own event
    (dropping a LATER turn that was appended after it, simulating a
    crash right after X's own durable write) -> reconstruct (a fresh
    Session over the SAME workspace, genuinely re-reading from disk, not
    the same in-memory object) -> assert X (the watermark) survives.

    Architect's own witness (issue comment): "その summary の WAL event
    より 後ろを 切り詰めて 再構成しても watermark が 生き残る" — this is
    that exact test.
    """
    session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, fold_persist_policy="next_turn",
    )
    events = _drive_one_recovering_turn(session)
    persisted = [
        e for e in events
        if e.type == "recovery_summary_persisted" and e.data.get("outcome") == "persisted"
    ]
    assert persisted, "sanity: the recovery must have persisted a summary"
    watermark_before = session._history_buffer.compaction_watermark()
    assert watermark_before > 0

    # A LATER turn, appended AFTER the persisted summary — this is what
    # must be genuinely dropped by the truncation below (the positive
    # witness that the truncation did something, not a no-op).
    _push(session, "user", "a later turn written after the summary")
    lines_with_later_turn = session.history_path.read_text(
        encoding="utf-8",
    ).splitlines()
    assert any(
        "a later turn written after the summary" in line
        for line in lines_with_later_turn
    ), "sanity: the later turn must genuinely be on disk before truncation"

    # Truncate past the summary's own event: find its line (by its own
    # unique covers_through_seq value) and keep everything UP TO AND
    # INCLUDING it, dropping everything after (the later turn above).
    summary_covers = persisted[0].data["covers_through_seq"]
    needle = f'"covers_through_seq": {summary_covers}'
    idx = next(
        i for i, line in enumerate(lines_with_later_turn) if needle in line
    )
    truncated_lines = lines_with_later_turn[: idx + 1]
    assert len(truncated_lines) < len(lines_with_later_turn), (
        "sanity: the truncation must actually drop something"
    )
    session.history_path.write_text(
        "\n".join(truncated_lines) + "\n", encoding="utf-8",
    )

    # Reconstruct: a FRESH Session over the SAME workspace (same
    # tmp_path/agent_name — monkeypatch.chdir keeps cwd pinned there for
    # the whole test) — genuinely re-reads history.jsonl from disk, never
    # reuses the original session's in-memory state.
    fresh_session = _make_spill_session(
        tmp_path, monkeypatch, t_max=2_500,
        max_shrink_iterations=25, fold_persist_policy="next_turn",
    )
    # #4387 Phase B ①: a Session's own __init__ does NOT hydrate history —
    # every real caller (chat.py, web/deps.py, registry_bootstrap.py, ...)
    # calls load_history() explicitly right after construction. Omitting
    # it here would make this "reconstruction" read an artificially empty
    # history regardless of what the truncation above did — not a
    # genuine restart simulation.
    fresh_session.load_history()

    watermark_after = fresh_session._history_buffer.compaction_watermark()
    assert watermark_after == watermark_before, (
        "the persisted summary's own watermark must survive a truncation "
        "of everything written after it"
    )
    assert not any(
        "a later turn written after the summary" in (m.content or "")
        for m in fresh_session.history
    ), (
        "the later (post-truncation) turn must genuinely be gone from the "
        "reconstruction — otherwise the truncation above was a no-op and "
        "this test proves nothing"
    )
