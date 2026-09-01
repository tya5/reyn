"""Tier 2: #5588 — Session.compaction_progress_raw() caches the real #5592
observability fields as they arrive on this session's own audit log.

Real ``Session``, real ``EventLog`` throughout. Driving via a direct
``session._audit_events.emit(...)`` call (not the full retry_loop machinery)
is legitimate here — #5557's own established discriminator: this test's
claim is "the subscriber correctly caches what a real
compaction_shrink_recovered/llm_request(_error) event already carries", not
"production fires this event in scenario X" (that claim belongs to
tests/services/test_5592_observability_fields.py and
tests/runtime/test_5296_pr2_byte_reduction_same_turn_retry.py, which drive
the real ladder end-to-end)."""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from tests._support.agent_session import make_session


def _make_session(tmp_path: Path):
    return make_session(
        agent_name="compaction-progress-wiring-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


def test_raw_starts_with_every_field_none(tmp_path):
    """Tier 2: before any compaction_shrink_recovered/llm_request(_error)/
    recovery_summary_persisted has fired, every cached field reads None —
    never a fabricated 0. The closed-set comparison is what makes a future
    FIFTH field a deliberate change rather than a silent drift (it already
    caught #5578's own ``persisted_covers_through_seq`` being added)."""
    session = _make_session(tmp_path)
    raw = session.compaction_progress_raw()
    assert raw == {
        "is_compacting": False,
        "raw_middle_remaining": None,
        "raw_middle_total": None,
        "upstream_recovery_call_count": None,
        "persisted_covers_through_seq": None,
    }


def test_compaction_shrink_recovered_populates_raw_middle_fields(tmp_path):
    """Tier 2: a real compaction_shrink_recovered event's own
    raw_middle_remaining/raw_middle_total land in compaction_progress_raw()
    verbatim — the exact field names #5592 actually emits (verified against
    engine.py:3081-3082's own emit call), not a renamed/derived copy."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "compaction_shrink_recovered",
        cause="ContextOverflowError", iteration=0, consecutive=1,
        t_max_override=None, raw_middle_remaining=5, raw_middle_total=2469,
    )
    raw = session.compaction_progress_raw()
    assert raw["raw_middle_remaining"] == 5
    assert raw["raw_middle_total"] == 2469


def test_llm_request_populates_call_count_only_when_not_none(tmp_path):
    """Tier 2: upstream_recovery_call_count is None outside a recovery
    episode (#5592's own documented contract) — an ordinary llm_request
    with count=None must NOT clear an already-cached real count back to
    unknown (an interleaved ordinary call must not blank the display)."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "llm_request", model="x", input_chars=100,
        max_input_tokens_applied=8000, upstream_recovery_call_count=3,
    )
    assert session.compaction_progress_raw()["upstream_recovery_call_count"] == 3

    # An ordinary (non-recovery) call interleaves — count is None on THIS
    # event, but the cache must keep the last REAL count.
    session._audit_events.emit(
        "llm_request", model="x", input_chars=50,
        max_input_tokens_applied=8000, upstream_recovery_call_count=None,
    )
    assert session.compaction_progress_raw()["upstream_recovery_call_count"] == 3


def test_llm_request_error_also_populates_call_count(tmp_path):
    """Tier 2: llm_request_error (the failure-path sibling) carries the
    same field and must also update the cache — a rejected request still
    counts toward the call sequence (#5592's own motivating incident: a
    rejected request is still billed and must not vanish from the count)."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "llm_request_error", model="x", error="boom",
        upstream_recovery_call_count=7,
    )
    assert session.compaction_progress_raw()["upstream_recovery_call_count"] == 7


def test_is_compacting_reads_live_not_cached(tmp_path):
    """Tier 2: is_compacting is NEVER cached from an event (unlike the
    other 3 fields) — it forwards live to CompactionController.is_compacting
    on every read, matching #5588's earlier is_compacting property tests."""
    session = _make_session(tmp_path)
    assert session.compaction_progress_raw()["is_compacting"] is False


# ── #5578/#5610: the persisted recovery watermark ──────────────────────


def test_persisted_outcome_caches_the_watermark(tmp_path):
    """Tier 2: a real recovery_summary_persisted event with
    ``outcome="persisted"`` — the ONE outcome that actually advanced the
    durable cover — lands its covers_through_seq in the cache, verbatim.
    Field names verified against compaction_controller.py's own emit and
    events.md's own catalog row, not assumed."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "recovery_summary_persisted",
        outcome="persisted", covers_through_seq=412,
        section_lengths={"topic_arc": 40},
    )
    assert session.compaction_progress_raw()["persisted_covers_through_seq"] == 412


def test_already_covered_outcome_does_not_move_the_watermark(tmp_path):
    """Tier 2: deny side — ``already_covered`` is the idempotent no-op
    (#5578: a repeat call within the same turn). It carries a
    covers_through_seq that did NOT become the durable cover, so caching
    it would display an advance that never happened. The cache must keep
    the last genuinely-persisted value."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "recovery_summary_persisted", outcome="persisted", covers_through_seq=412,
    )
    session._audit_events.emit(
        "recovery_summary_persisted",
        outcome="already_covered", covers_through_seq=999, prev_cover=412,
    )
    assert session.compaction_progress_raw()["persisted_covers_through_seq"] == 412


def test_no_covers_through_seq_outcome_does_not_move_the_watermark(tmp_path):
    """Tier 2: deny side — ``no_covers_through_seq`` is #5498's own guard
    (retry_loop's structurally-0 covers_through_seq must never reach
    history). Nothing was persisted, so nothing may be displayed as
    persisted."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "recovery_summary_persisted", outcome="no_covers_through_seq",
        covers_through_seq=0,
    )
    assert session.compaction_progress_raw()["persisted_covers_through_seq"] is None
