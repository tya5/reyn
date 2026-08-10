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


def test_chain_register_replay_carries_origin_sid_and_task_kind():
    """Tier 2: #3978 P4 — a chain_register event's ``origin_sid`` (#2130) and
    ``task_kind`` (P4) fields must survive PURE WAL REPLAY, not just a
    snapshot save/load round-trip. Before this fix, ``apply_events``'s own
    ``chain_register`` branch hardcoded a 5-field dict that silently dropped
    both — a crash recovered via replay (rather than a loaded snapshot file)
    would reconstruct a pending_chains entry missing them, which
    ``ChainManager.restore()`` then reads back as ``None`` regardless of
    what was actually registered. This directly affects P4's
    ``_PendingChain.requester`` property (derived from
    ``origin_agent``/``origin_sid``) and ``describe_task``/``list_tasks``
    (which read ``kind``): a wrong ``origin_sid`` silently mis-attributes
    the requester to "main"."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-kind",
            origin_agent="a", origin_depth=0, original_request="x",
            origin_sid="sid-7", task_kind="pipeline",
        ),
    ])
    chain = snap.pending_chains["c-kind"]
    assert chain["origin_sid"] == "sid-7"
    assert chain["task_kind"] == "pipeline"


def test_chain_register_replay_defaults_origin_sid_and_task_kind_to_none():
    """Tier 2: falsification pair — an event carrying neither field (a
    pre-#2130/pre-P4 WAL entry) still replays cleanly, defaulting both to
    ``None`` rather than raising a ``KeyError`` (both are ``event.get``,
    not ``event[...]``)."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-old",
            origin_agent="a", origin_depth=0, original_request="x",
        ),
    ])
    chain = snap.pending_chains["c-old"]
    assert chain["origin_sid"] is None
    assert chain["task_kind"] is None


def test_chain_update_replay_mirrors_a_non_waiting_on_field():
    """Tier 2: #4108 bug ① on the WAL-REPLAY side — apply_events's
    chain_update branch was hardcoded to write back ``waiting_on`` only.
    A field-name-independent mirror means a new field (e.g. proposal 0067
    P8's ``arm_at``) survives pure WAL replay with no new hardcode."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-arm",
            origin_agent="a", origin_depth=0, original_request="x",
        ),
        _event("chain_update", seq=2, chain_id="c-arm", arm_at=99.5),
    ])
    assert snap.pending_chains["c-arm"]["arm_at"] == 99.5


def test_chain_update_replay_omitting_waiting_on_does_not_destroy_it():
    """Tier 2: #4108 bug ② on the WAL-REPLAY side — the old code read
    ``event.get("waiting_on", [])`` unconditionally, so a chain_update
    event that didn't carry ``waiting_on`` at all still overwrote it to
    ``[]`` on replay, destroying state set by a PRIOR event."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-preserve",
            origin_agent="a", origin_depth=0, original_request="x",
            waiting_on=["p", "q"],
        ),
        _event("chain_update", seq=2, chain_id="c-preserve", arm_at=1.0),
    ])
    assert snap.pending_chains["c-preserve"]["waiting_on"] == ["p", "q"]


def test_chain_update_replay_does_not_leak_routing_meta_keys():
    """Tier 2: falsification pair for the field-independent rewrite — the
    routing/meta keys every WAL event carries (kind/seq/target/agent/
    session_id/chain_id) must NOT be mirrored into the pending_chains
    entry as if they were chain state (they are excluded by
    ``_CHAIN_EVENT_META_KEYS``, not merely absent from this probe)."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-meta",
            origin_agent="a", origin_depth=0, original_request="x",
        ),
        _event("chain_update", seq=2, chain_id="c-meta", waiting_on=["z"]),
    ])
    chain = snap.pending_chains["c-meta"]
    for meta_key in ("kind", "seq", "target", "agent", "session_id"):
        assert meta_key not in chain


def test_truncate_falsify_chain_update_field_survives_wal_truncation(tmp_path: Path):
    """Tier 2c: CLAUDE.md's recovery-feature PR gate — #4108 fixes a PR that
    makes a ``chain_update``-carried field (any field, not just
    ``waiting_on``) actually reach the reconstruction source; the gate
    requires a truncate-falsify test in the SAME PR, not a follow-up. Same
    shape as ``test_truncate_falsify_snapshot_backed_kept_state_survives``
    above (#2259's own precedent): set a field via a real
    chain_register+chain_update pair, bake BOTH into a saved snapshot
    (``applied_seq`` past both), then reload and replay an EMPTY tail
    (simulating the source WAL events truncated below the snapshot's own
    seq) — the field must still be correct, because it survives via the
    snapshot, not via replaying the (now-gone) WAL entries. The WAL-only
    control at the end proves this isn't trivially passing (an un-fixed
    field would ALSO be "gone" whether truncated or not, for the wrong
    reason — never wrote back at all)."""
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-ttl",
            origin_agent="a", origin_depth=0, original_request="x",
        ),
        _event("chain_update", seq=2, chain_id="c-ttl", arm_at=42.0),
    ])
    assert snap.applied_seq == 2
    assert snap.pending_chains["c-ttl"]["arm_at"] == 42.0

    # Serialize → the snapshot carries arm_at + applied_seq=2.
    snap.save(tmp_path / "snap-ttl.json")

    # TRUNCATE: replay an EMPTY tail (both source WAL events, seq 1 and 2,
    # are gone below the truncation floor) — apply_events skips seq<=applied_seq
    # regardless, so this simulates "the events aren't even offered" faithfully.
    reloaded = AgentSnapshot.load("agent_x", tmp_path / "snap-ttl.json")
    reloaded.apply_events([])
    assert reloaded.pending_chains["c-ttl"]["arm_at"] == 42.0, (
        "arm_at must survive WAL truncation below its chain_update's seq — "
        "it is baked into the SAVED snapshot, not replayed from the (now-gone) event"
    )

    # WAL-only CONTROL: a DIFFERENT chain, registered+updated entirely AFTER
    # the snapshot's applied_seq, is WAL-only — present when its events are
    # replayed, LOST when they're truncated instead. Proves the assertion
    # above is snapshot-backed survival, not something that always passes.
    later_events = [
        _event(
            "chain_register", seq=3, chain_id="c-walonly",
            origin_agent="a", origin_depth=0, original_request="y",
        ),
        _event("chain_update", seq=4, chain_id="c-walonly", arm_at=7.0),
    ]
    replayed = AgentSnapshot.load("agent_x", tmp_path / "snap-ttl.json")
    replayed.apply_events(later_events)
    assert replayed.pending_chains["c-walonly"]["arm_at"] == 7.0  # present WITH its events
    truncated = AgentSnapshot.load("agent_x", tmp_path / "snap-ttl.json")
    truncated.apply_events([])  # seq 3/4 events truncated
    assert "c-walonly" not in truncated.pending_chains  # WAL-only state LOST


def test_chain_update_replay_writes_only_real_pending_chain_fields():
    """Tier 2: architect's #4110 co-vet, non-blocking residue — closes it in
    this same PR rather than deferring to P8.

    ``test_chain_update_replay_does_not_leak_routing_meta_keys`` above checks
    against ``_CHAIN_EVENT_META_KEYS`` itself — a deny-list a future chokepoint
    change could grow without this probe ever noticing, since the probe and the
    code it's checking would drift in lockstep. This test checks against a
    DIFFERENT, independent source of truth instead: ``_PendingChain``'s own
    dataclass field names, read via ``dataclasses.fields`` — the one place the
    real schema is declared. ``core/events/`` (where ``agent_snapshot.py``
    lives) cannot import ``runtime/services/chain_manager.py`` at the MODULE
    level (the established layering direction is the reverse — see
    ``_CHAIN_EVENT_META_KEYS``'s comment), but a TEST has no such constraint,
    so the allow-list inversion architect proposed for the module is done here
    instead, at the seam where it's actually reachable."""
    import dataclasses

    from reyn.runtime.services.chain_manager import _PendingChain

    # `status`/`cancel` are VOLATILE (chain_manager.py's own field comments:
    # "deliberately NOT threaded through register()'s persisted `fields`
    # dict") — they never reach a WAL event or a pending_chains snapshot
    # entry, so a real replay can never write them; excluding them keeps this
    # an allow-list of what CAN legitimately appear, not "every declared
    # field regardless of whether it's ever persisted".
    # `kind` persists under the dict key "task_kind" (agent_snapshot.py's own
    # chain_register branch, same collision note as `_CHAIN_EVENT_META_KEYS`:
    # "kind" is already the WAL event's own type-discriminator key).
    allowed = {
        ("task_kind" if f.name == "kind" else f.name)
        for f in dataclasses.fields(_PendingChain)
        if f.name not in ("status", "cancel")
    }
    snap = _snap("agent_x")
    snap.apply_events([
        _event(
            "chain_register", seq=1, chain_id="c-schema",
            origin_agent="a", origin_depth=0, original_request="x",
        ),
        _event("chain_update", seq=2, chain_id="c-schema", waiting_on=["z"]),
    ])
    chain = snap.pending_chains["c-schema"]
    leaked = set(chain) - allowed
    assert not leaked, (
        f"chain_update replay wrote key(s) {leaked} into pending_chains that "
        "_PendingChain has no field for — a real schema mismatch, not just "
        "absence from the deny-list"
    )
