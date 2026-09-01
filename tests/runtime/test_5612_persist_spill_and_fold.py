"""Tier 2: #5612 — reactive overflow-recovery spill is durably recorded
in ``history.jsonl`` and survives a restart, and ``decompose_history_
for_retry`` / ``build_history`` draw from the SAME watermark-filtered
turn population.

Owner ruling (2026-09-02, verbatim): "永続化というのは llm に見える
ヒストリが元に戻らない ということ。history.jsonl に追記する ということ
だよ" — a spill that succeeds must survive a restart the same way #5610's
own compact-fold persistence does; #5578/#5610 covers the compact half
(``tests/runtime/test_5578_persist_recovery_summary.py``), this file
covers the spill half and the "one projection rule" invariant architect's
own #5612 ruling adds on top: once a summary is a real history entry,
``decompose_history_for_retry`` (retry_loop's own working set) and
``build_history`` (the wire projection) must agree on which turns are
"visible" — both watermark-filtered identically, never two different
populations.

Real ``MediaStore``/``Session``/``RouterHistoryBuffer`` throughout — no
mock. ``spill_turn_content`` (router_history_buffer.py) is driven
directly for the truncate/accept/deny scenarios (the same public surface
``_attempt_reactive_spill`` itself calls), matching this file's own
narrow scope: the DURABILITY of a spill record, not the overflow ladder
that decides to spill in the first place (already covered by
``test_5296_pr2_byte_reduction_same_turn_retry.py``). Two strip
scenarios that would otherwise reach a private attribute
(``_spill_supersede_map``, ``_history_appender``) were removed per
architect co-vet (#5617) and are instead driven by lead-coder through
the real production call path, recorded on the PR.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.runtime.chat_message import Spillability
from tests.runtime.test_5296_pr2_byte_reduction_same_turn_retry import (
    _make_spill_session,
    _push,
)


def _spill_records(session) -> "list":
    return [m for m in session.history if m.role == "spill_record"]


# ── truncate-falsify (CLAUDE.md hard rule) ──────────────────────────────────


def test_persisted_spill_survives_wal_truncation_past_its_own_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5612 truncate-falsify — set X (persist a spill record) ->
    truncate history.jsonl (this repo's own durable store) past X's own
    event, dropping a LATER turn appended after it -> reconstruct (a
    fresh Session + load_history(), genuinely re-reading from disk) ->
    assert X survives: the original oversized turn's own projection is
    STILL the offloaded preview, not the full text, and the truncated
    later turn is genuinely gone."""
    session = _make_spill_session(tmp_path, monkeypatch)
    huge = "Y" * 50_000
    _push(session, "user", "look something up")
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")

    hb = session._loop_driver._history_buffer
    replacement = hb.spill_turn_content(huge, chain_id="c1", tool="tool", seq=1)
    assert replacement is not None and replacement != huge, (
        "sanity: the spill must have genuinely produced a preview"
    )
    assert _spill_records(session), "sanity: a durable spill_record must exist"

    # A LATER turn, appended AFTER the persisted spill record — dropped by
    # the truncation below (the positive witness the truncation did
    # something, not a no-op).
    _push(session, "user", "a later turn written after the spill")
    lines_with_later_turn = session.history_path.read_text(
        encoding="utf-8",
    ).splitlines()
    assert any(
        "a later turn written after the spill" in line
        for line in lines_with_later_turn
    ), "sanity: the later turn must genuinely be on disk before truncation"

    # Truncate past the spill record's own event: find its line (by its
    # own unique content_ref value) and keep everything up to and
    # including it.
    content_ref = _spill_records(session)[0].meta["content_ref"]
    needle = f'"content_ref": "{content_ref}"'
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

    fresh_session = _make_spill_session(tmp_path, monkeypatch)
    fresh_session.load_history()

    fresh_hb = fresh_session._loop_driver._history_buffer
    assert fresh_hb.is_already_spilled(replacement), (
        "the persisted spill record must survive a truncation of "
        "everything written after it"
    )
    built = fresh_hb.build_history()
    tool_turn = next(t for t in built if t.get("tool_call_id") == "tc1")
    assert tool_turn["content"] == replacement, (
        "the reconstructed projection must show the offloaded preview, "
        "not the original full text — otherwise durability did not "
        "survive the restart"
    )
    assert not any(
        "a later turn written after the spill" in (m.content or "")
        for m in fresh_session.history
    ), (
        "the later (post-truncation) turn must genuinely be gone from "
        "the reconstruction — otherwise the truncation above was a "
        "no-op and this test proves nothing"
    )


# #5617 (architect co-vet, in-PR-required ②): a strip test monkeypatching
# ``_spill_supersede_map`` (private state — testing policy: "a test must
# not depend on private state") was removed here. Its strip witness (the
# exact pre-#5612 regression this whole PR closes) is instead driven by
# lead-coder through the REAL production call path — removing
# ``history_appender``'s own wiring so a fresh reconstruction re-offers
# the original full text — and recorded on the PR, not as a repo test
# that reaches into a private attribute.


def test_recovery_policy_never_still_persists_spill_but_not_fold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5612/#5617 — architect's own FINAL ruling (PR review,
    superseding an earlier #5612 design-doc sentence that said
    otherwise, verbatim): "spill record は recovery_policy に依らず常に
    永続化する。knob が gate するのは fold だけ". Reasons on the record:
    (1) the SAME artifact this reuses — the write-time cap's own
    ``SPILLED_META_KEY`` entry — already persists unconditionally, with
    no ``recovery_policy`` reader anywhere; making durability depend on
    WHEN the identical operation happened would split one artifact into
    two durability classes. (2) spill is reversible for the agent (body
    lives in ``MediaStore``, the preview names the read-back path) —
    ``recovery_policy`` exists specifically to gate the IRREVERSIBLE
    fold step, a rationale that never reaches spill.

    Accept: ``recovery_policy="never"`` still durably appends exactly
    ONE ``spill_record`` (lead-coder's own drive measurement, now a
    test). Deny sibling (the discriminator for THIS ruling): the SAME
    ``never`` session's fold path stays gated — see
    ``test_5578_persist_recovery_summary.py::
    test_recovery_policy_never_disables_the_persist_path_too`` (existing
    #5578 gate witness, unchanged, referenced not duplicated) — spill
    persists, fold does not, under the identical knob value."""
    session = _make_spill_session(tmp_path, monkeypatch, recovery_policy="never")
    huge = "W" * 50_000
    _push(session, "user", "look something up")
    _push(session, "tool", huge, tool_call_id="tc1", name="tool")

    hb = session._loop_driver._history_buffer
    replacement = hb.spill_turn_content(huge, chain_id="c1", tool="tool", seq=1)
    assert replacement is not None and replacement != huge, (
        "sanity: the in-memory substitution must still happen"
    )
    assert len(_spill_records(session)) == 1, (
        "recovery_policy='never' must still durably append exactly ONE "
        "spill_record — architect's own final gate ruling: the knob "
        "gates fold only, never spill"
    )


# #5617 (architect co-vet, in-PR-required ③): a strip test monkeypatching
# ``_history_appender`` (private state — testing policy: "a test must not
# depend on private state") was removed here. The strip witness for the
# accept case above is instead driven by lead-coder through the REAL
# production call path and recorded on the PR, not as a repo test that
# reaches into a private attribute.


# ── deny: an ordinary spill-free session never appends a spill_record ──────


def test_no_overflow_never_appends_a_spill_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5612 deny (sibling of the accept side) — a session that
    never calls ``spill_turn_content`` at all (an ordinary turn, no
    overflow) must never have a ``role="spill_record"`` entry in its own
    durable history. Without this sibling, the accept side alone could
    pass in a world where a spill record appends unconditionally."""
    session = _make_spill_session(tmp_path, monkeypatch)
    _push(session, "user", "hello", spillability=Spillability.NEVER)
    _push(session, "assistant", "hi there", spillability=Spillability.NEVER)

    assert not _spill_records(session), (
        "an ordinary, spill-free session must never durably record a "
        "spill_record entry"
    )


# ── population equality: decompose and build_history agree ─────────────────


def test_decompose_and_build_history_draw_from_the_same_population(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: #5612 accept — architect's own "one projection rule"
    requirement. With a real durable summary present (watermark > 0),
    ``decompose_history_for_retry``'s own working set (head + raw_middle
    + tail, non-summary turns) and ``build_history``'s own wire
    projection must be built from the EXACT SAME watermark-filtered
    population — never two different populations that could disagree on
    which turns the LLM is allowed to see."""
    session = _make_spill_session(tmp_path, monkeypatch, t_max=7_000)
    budgets = session._compaction_controller._engine.budgets
    turn_tokens = 80
    turn_count = (budgets.head_budget + budgets.tail_budget) // turn_tokens + 10
    # Unique content per turn (not a repeated fixed string) — this test's
    # own deny-sibling check below identifies "the covered turn" by
    # content, which is meaningless if every turn shares the same text.
    for _i in range(turn_count):
        _push(
            session, "user", f"turn-{_i}-" + ("X" * (turn_tokens * 4)),
            spillability=Spillability.NEVER,
        )

    hb = session._loop_driver._history_buffer

    # Directly persist a real durable summary covering the first half of
    # history, matching the SAME shape #5610/#5578 already establish (no
    # need to drive a full overflow episode for this population-equality
    # check — the watermark-filter this test targets applies identically
    # regardless of WHICH mechanism advanced the watermark).
    from reyn.services.compaction.engine import ChatSummary

    half = session.history[len(session.history) // 2]
    import asyncio
    asyncio.run(
        session._compaction_controller.persist_recovery_summary(
            ChatSummary(topic_arc="stub", covers_through_seq=0),
            covers_through_seq=half.seq,
        )
    )
    watermark = hb.compaction_watermark()
    assert watermark == half.seq, "sanity: the summary must have persisted"

    built = hb.build_history()
    head, raw_middle, tail, _summary, _ = hb.decompose_history_for_retry()

    # build_history's own wire dicts don't carry `seq` at all (#2957 PR-B's
    # own canonical-quantity contract) — so the REAL population-equality
    # witness is turn COUNT + each one's own presence-below-watermark,
    # not a seq-set comparison build_history structurally cannot supply.
    # build_history's own bridge turn (the synthetic role="assistant"
    # carrier of the summary's own text, attached once watermark > 0 —
    # see its own docstring) is excluded here by its own content marker,
    # the same way decompose's own role=="summary" turn is excluded —
    # both are the summary's OWN representation, never a real turn.
    non_summary_built = [
        t for t in built
        if t.get("role") != "summary"
        and "[summary of earlier conversation]" not in str(t.get("content", ""))
    ]
    non_summary_decomposed = [
        t for t in head + raw_middle + tail if t.get("role") != "summary"
    ]
    assert len(non_summary_built) == len(non_summary_decomposed), (
        f"build_history() returned {len(non_summary_built)} non-summary "
        f"turns but decompose_history_for_retry() returned "
        f"{len(non_summary_decomposed)} — the two projections drew from "
        f"different populations"
    )

    # ── deny sibling: a raw turn AT/BELOW the watermark appears in NEITHER
    # decompose's own working set (spill candidates / refill source) NOR
    # build_history's own wire projection.
    covered_turn = session.history[0]
    assert covered_turn.seq <= watermark
    covered_content = covered_turn.content
    assert not any(
        t.get("content") == covered_content for t in head + raw_middle + tail
    ), (
        "a watermark-covered raw turn must never appear in decompose's "
        "own working set — it cannot be a spill candidate or a refill "
        "source once a durable summary already covers it"
    )
    assert not any(t.get("content") == covered_content for t in built), (
        "a watermark-covered raw turn must never appear in build_history's "
        "own wire projection either — same population, same exclusion"
    )
