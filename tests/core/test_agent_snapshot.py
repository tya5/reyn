"""Tests for AgentSnapshot — new fields and apply_events handlers (PR-state-foundation)."""
import json
from pathlib import Path

from reyn.core.events.agent_snapshot import AgentSnapshot

# ── helpers ─────────────────────────────────────────────────────────────────

def _snap(name: str = "agent_x") -> AgentSnapshot:
    return AgentSnapshot.empty(name)


def _event(kind: str, seq: int = 1, **fields) -> dict:
    return {"kind": kind, "seq": seq, "target": "agent_x", **fields}


# ── new field round-trip ──────────────────────────────────────────────────────

def test_new_fields_default_values():
    """Tier 2: outstanding_interventions defaults to empty dict."""
    snap = AgentSnapshot.empty("agent_new")
    assert snap.outstanding_interventions == {}


def test_new_fields_save_load_roundtrip(tmp_path: Path):
    """Tier 2: outstanding_interventions survives save/load."""
    path = tmp_path / "snapshot.json"
    snap = AgentSnapshot.empty("agent_y")
    snap.outstanding_interventions = {"iv-A": {"question": "ok?"}}

    snap.save(path)
    loaded = AgentSnapshot.load("agent_y", path)

    assert loaded.outstanding_interventions == {"iv-A": {"question": "ok?"}}


def test_old_snapshot_without_new_fields_loads_with_defaults(tmp_path: Path):
    """Tier 2: snapshot written before new fields are absent → default gracefully."""
    path = tmp_path / "snapshot_old.json"
    old_payload = {
        "version": 1,
        "applied_seq": 7,
        "inbox": [],
        "pending_chains": {},
        # active_skill_run_ids and outstanding_interventions intentionally absent
    }
    path.write_text(json.dumps(old_payload), encoding="utf-8")

    snap = AgentSnapshot.load("agent_old", path)
    assert snap.applied_seq == 7
    assert snap.outstanding_interventions == {}


# ── legacy skill-run snapshot field is ignored (② skill-recovery-state removal) ──

def test_old_snapshot_with_legacy_active_skill_run_ids_ignored(tmp_path: Path):
    """Tier 2c: a pre-existing snapshot carrying the removed ``active_skill_run_ids``
    key deserialises without error — the removed field is silently ignored while
    the kept fields still load. Migration-safety for the field removal (the
    skill runtime that populated it is gone; nothing consumes the field)."""
    path = tmp_path / "snapshot_legacy.json"
    legacy_payload = {
        "version": 1,
        "applied_seq": 3,
        "inbox": [],
        "pending_chains": {},
        # removed field still present in an old on-disk snapshot:
        "active_skill_run_ids": ["run-legacy-1", "run-legacy-2"],
        "outstanding_interventions": {"iv-keep": {"q": "?"}},
    }
    path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    snap = AgentSnapshot.load("agent_legacy", path)  # must not raise
    assert not hasattr(snap, "active_skill_run_ids")  # field is gone
    # kept fields load correctly; the removed key is silently dropped
    assert snap.applied_seq == 3
    assert snap.outstanding_interventions == {"iv-keep": {"q": "?"}}


# ── apply_events: intervention_dispatched / intervention_resolved ─────────────

def test_apply_intervention_dispatched_stores_iv():
    """Tier 2: intervention_dispatched stores iv_dict under intervention_id."""
    snap = _snap()
    snap.apply_events([
        _event(
            "intervention_dispatched",
            seq=1,
            intervention_id="iv-001",
            iv_dict={"question": "proceed?"},
        )
    ])
    assert "iv-001" in snap.outstanding_interventions
    assert snap.outstanding_interventions["iv-001"] == {"question": "proceed?"}


def test_apply_intervention_resolved_removes_iv():
    """Tier 2: intervention_resolved removes the entry from outstanding_interventions."""
    snap = _snap()
    snap.apply_events([
        _event(
            "intervention_dispatched",
            seq=1,
            intervention_id="iv-002",
            iv_dict={"q": "x"},
        ),
        _event("intervention_resolved", seq=2, intervention_id="iv-002"),
    ])
    assert "iv-002" not in snap.outstanding_interventions


def test_apply_intervention_resolved_unknown_noop():
    """Tier 2: intervention_resolved for unknown id is a no-op (no KeyError)."""
    snap = _snap()
    snap.apply_events([
        _event("intervention_resolved", seq=1, intervention_id="ghost-iv")
    ])
    assert snap.outstanding_interventions == {}


# ── skill-internal kinds don't mutate agent snapshot ─────────────────────────

def test_skill_kinds_are_snapshot_noop():
    """Tier 2: every skill_* kind (skill_started/phase_advanced/completed/discarded/
    resumed) and step_* is still a valid WAL kind — read by the replay/rewind engine —
    but does NOT mutate agent-snapshot state on reconstruction. So an old-WAL
    skill_started (which previously populated the removed active_skill_run_ids)
    now falls through cleanly: no raise, no mutation. This is the reconstruction-
    safety guarantee for the per-skill run-id-tracking removal."""
    snap = _snap()
    events = [
        _event("skill_started", seq=1, run_id="r"),
        _event("skill_phase_advanced", seq=2, run_id="r", next_phase="p2"),
        _event("step_started", seq=3, run_id="r", op="file/read"),
        _event("step_completed", seq=4, run_id="r", op="file/read", result="ok"),
        _event("step_failed", seq=5, run_id="r", op="mcp/tool", error="timeout"),
        _event("skill_resumed", seq=6, run_id="r"),
        _event("skill_completed", seq=7, run_id="r"),
        _event("skill_discarded", seq=8, run_id="r"),
    ]
    snap.apply_events(events)  # must not raise on any skill_* kind
    # Agent-level state is untouched by every skill_* / step_* kind
    assert snap.outstanding_interventions == {}
    assert snap.pending_chains == {}
    assert snap.applied_seq == 8


# ── recovery gate: truncate-falsify for the active_skill_run_ids removal ──────

def test_truncate_falsify_snapshot_backed_kept_state_survives(tmp_path: Path):
    """Tier 2c: removing active_skill_run_ids does not regress crash-recovery
    (CLAUDE.md recovery gate) — a SNAPSHOT-BACKED kept state survives WAL
    truncation below its source seq, and an old-WAL skill_started falls through.

    Per #2259/#2260 ([[feedback_recovery_source_must_survive_truncation_review_gate]]):
    ONLY snapshot-backed reconstruction survives truncation; a WAL-only state is
    lost. This test's survival assertion is meaningful precisely because the kept
    state (``outstanding_interventions``) is APPLIED BEFORE ``serialize()`` — so
    ``applied_seq`` bakes it INTO the snapshot. The WAL-only control at the end
    proves the assertion is not trivially passing.
    """
    # 1. Apply, BEFORE serialize: a legacy skill_started (dead → no-op) + the KEPT
    #    intervention. Both land at seq<=2 → baked into applied_seq=2 (snapshot).
    snap = _snap()
    snap.apply_events([
        _event("skill_started", seq=1, run_id="legacy-run"),
        _event("intervention_dispatched", seq=2, intervention_id="iv-keep",
               iv_dict={"q": "resume?"}),
    ])
    assert snap.applied_seq == 2
    assert "iv-keep" in snap.outstanding_interventions

    # 2. Serialize → the snapshot carries the intervention + applied_seq=2.
    snap.save(tmp_path / "snap.json")
    reloaded = AgentSnapshot.load("agent_x", tmp_path / "snap.json")
    assert reloaded.applied_seq == 2                       # baked-in seq
    assert "iv-keep" in reloaded.outstanding_interventions  # snapshot-backed

    # 3. TRUNCATE: the source WAL entries at seq<=2 (the legacy skill_started AND
    #    the intervention_dispatched) are gone. Replaying them is a no-op because
    #    apply_events skips seq<=applied_seq — so the intervention survives ONLY
    #    via the snapshot, and the legacy skill_started never raises.
    reloaded.apply_events([
        _event("skill_started", seq=1, run_id="legacy-run"),
        _event("intervention_dispatched", seq=2, intervention_id="iv-keep",
               iv_dict={"q": "resume?"}),
    ])
    assert "iv-keep" in reloaded.outstanding_interventions   # survived truncation
    assert not hasattr(reloaded, "active_skill_run_ids")     # removed field gone

    # 4. WAL-only CONTROL — proves the step-3 survival is snapshot-backed, not
    #    trivial. A chain_register at seq=3 lands AFTER the snapshot (applied_seq=2)
    #    = WAL-only. It is PRESENT when its WAL entry is replayed, but LOST when
    #    reconstruction truncates that WAL entry — the opposite of the snapshot-
    #    backed intervention, which survived the SAME truncation.
    chain_ev = _event("chain_register", seq=3, chain_id="c-walonly",
                      origin_agent="a", origin_depth=0, original_request="x")
    replayed = AgentSnapshot.load("agent_x", tmp_path / "snap.json")  # applied_seq=2
    replayed.apply_events([chain_ev])                 # WAL entry replayed
    assert "c-walonly" in replayed.pending_chains     # present WITH its WAL entry
    truncated = AgentSnapshot.load("agent_x", tmp_path / "snap.json")  # applied_seq=2
    truncated.apply_events([])                         # chain@seq3 WAL entry truncated
    assert "c-walonly" not in truncated.pending_chains  # WAL-only state LOST
