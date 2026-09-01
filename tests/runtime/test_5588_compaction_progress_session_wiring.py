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


def test_compaction_shrink_recovered_figures_read_unknown_outside_an_episode(tmp_path):
    """Tier 2: #5618 rewrote what these 3 tests can claim. The subscriber
    still caches a real compaction_shrink_recovered event's own
    raw_middle_remaining/raw_middle_total verbatim (the exact field names
    #5592 emits, verified against engine.py:3081-3082's own emit call) —
    but ``compaction_progress_raw()`` now JOINS the cache against the
    driver's current recovery-episode number before returning it, and this
    session has no episode running, so the honest answer is unknown.

    Emitting these events with no episode in flight is not a state
    production can reach at all: the engine emits
    compaction_shrink_recovered from inside retry_loop, which only runs
    inside an episode. The accept side — the same fields cached and
    RETURNED during a real recovery — is therefore witnessed where it
    actually happens, by the ladder-driven tests in
    test_5618_recovery_episode_gate.py, not by a hand-emitted event here.
    What this test still pins is the deny direction of the join, which is
    the whole point of #5618's freshness rule: a figure that belongs to no
    current episode is never reported as if it belonged to this one."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "compaction_shrink_recovered",
        cause="ContextOverflowError", iteration=0, consecutive=1,
        t_max_override=None, raw_middle_remaining=5, raw_middle_total=2469,
    )
    raw = session.compaction_progress_raw()
    assert raw["raw_middle_remaining"] is None, raw
    assert raw["raw_middle_total"] is None, raw


def test_a_call_count_from_no_episode_is_never_reported_as_this_ones(tmp_path):
    """Tier 2: the same #5618 join, on the OTHER producer — the
    upstream_recovery_call_count carried by llm_request / llm_request_error
    (#5592's failure-path sibling: a rejected request is still billed and
    still counts). Both event types are exercised because they are two
    separate branches into the same cache, and a join applied to only one
    of them would leave the other reporting a stale count.

    #5592's own "an interleaved ordinary call (count=None) must not blank a
    real count" contract still holds in the subscriber, but it is no longer
    observable from outside without an episode to join against — it is
    witnessed on the production path instead, by the ladder-driven accept
    test in test_5618_recovery_episode_gate.py, where the count is seen to
    PROGRESS across a real recovery (a blanking write would show up there
    as the count dropping back to unknown mid-episode)."""
    session = _make_session(tmp_path)
    session._audit_events.emit(
        "llm_request", model="x", input_chars=100,
        max_input_tokens_applied=8000, upstream_recovery_call_count=3,
    )
    assert session.compaction_progress_raw()["upstream_recovery_call_count"] is None

    session._audit_events.emit(
        "llm_request_error", model="x", error="boom",
        upstream_recovery_call_count=7,
    )
    assert session.compaction_progress_raw()["upstream_recovery_call_count"] is None


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
