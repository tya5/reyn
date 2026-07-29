"""Tests for reyn.hooks.durable_pending_store (#3180) — the crash-durable
``PendingStore`` and the ``durable:`` config flag that routes composers to it.

The truncate-falsify recovery gate lives in its own file
(``test_3180_composer_pending_truncate_falsify.py``); this file covers the
store's own contract (round-trip fidelity, loud-not-silent failure legs,
stale-composer pruning) and the per-composer routing decision.
"""
from __future__ import annotations

import warnings

import pytest

from reyn.hooks.bus import HookBus
from reyn.hooks.composer import (
    ComposerConfigError,
    ComposerDef,
    ComposerInput,
    ComposerOp,
    ComposerPolicy,
    PendingRecord,
    build_composers,
    load_composers,
)
from reyn.hooks.durable_pending_store import STORE_FILENAME, DurablePendingStore
from reyn.hooks.event import HookEvent
from reyn.hooks.event_pattern import EventPattern


def _input(kind: str) -> ComposerInput:
    return ComposerInput(kind=kind, pattern=EventPattern(kind=kind, payload=None))


def _deadline_def(name: str = "job_overdue", *, durable: bool = True) -> ComposerDef:
    return ComposerDef(
        name=name, op=ComposerOp.DEADLINE,
        inputs=(_input("orch:job_started"),),
        until_input=_input("orch:job_done"),
        emit_kind=f"composed:{name}", correlate_by="job_id",
        policy=ComposerPolicy(ttl_seconds=10.0),
        durable=durable,
    )


# ---------------------------------------------------------------------------
# Tier 1: the store's own contract
# ---------------------------------------------------------------------------


def test_record_round_trips_through_the_store_file(tmp_path):
    """Tier 1: every PendingRecord field a Composer reads back — the buffered
    HookEvents (with their own kind/payload/source/chain_id), the matched-input
    set, seq_pos, and both timestamps — survives a write/reload cycle unchanged.

    ``created_at`` in particular is the field a deadline's fire time is computed
    from, so a lossy round-trip here is a wrong deadline, not a missing one."""
    path = tmp_path / STORE_FILENAME
    record = PendingRecord(
        events=[
            HookEvent(
                kind="builtin:external:file_changed",
                payload={"path": "/a", "event_type": "modified"},
                chain_id="chain-7",
            ),
        ],
        matched_inputs={0, 2},
        seq_pos=1,
        created_at=1000.5,
        last_at=1002.25,
    )
    DurablePendingStore(path).put("c", "k", record)

    restored = DurablePendingStore(path).get("c", "k")
    assert restored is not None
    assert restored.created_at == 1000.5
    assert restored.last_at == 1002.25
    assert restored.seq_pos == 1
    assert restored.matched_inputs == {0, 2}
    (event,) = restored.events
    assert event.kind == "builtin:external:file_changed"
    assert event.payload == {"path": "/a", "event_type": "modified"}
    assert event.chain_id == "chain-7"


def test_absent_store_file_is_an_empty_set_not_an_error(tmp_path):
    """Tier 1: the first run of a session (no store file yet) starts empty and
    silent — an absent file is the normal cold-start state, not a fault."""
    store = DurablePendingStore(tmp_path / "never-written" / STORE_FILENAME)
    assert store.keys("c") == []


def test_unreadable_store_file_starts_empty_and_is_logged(tmp_path, caplog):
    """Tier 2: a corrupt store file degrades to an empty armed set AND says so
    at ERROR level. Both halves matter: raising would take down a Composer loop
    that is still serving every other op, but degrading *silently* would leave
    an operator believing a dead-man switch is watching when it is not."""
    path = tmp_path / STORE_FILENAME
    path.write_text("{not json at all", encoding="utf-8")
    with caplog.at_level("ERROR"):
        store = DurablePendingStore(path)
    assert store.keys("c") == []
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_unknown_schema_version_starts_empty_and_is_logged(tmp_path, caplog):
    """Tier 2: a store file written by a future, incompatible build is treated
    exactly like a corrupt one — loud and empty. Best-effort parsing of a shape
    this build does not understand would produce half-restored arm state, which
    is the silent-wrong-deadline failure the durable store exists to remove."""
    path = tmp_path / STORE_FILENAME
    path.write_text('{"version": 999, "records": []}', encoding="utf-8")
    with caplog.at_level("ERROR"):
        store = DurablePendingStore(path)
    assert store.keys("c") == []
    assert any("schema version" in r.getMessage() for r in caplog.records)


def test_retain_composers_drops_records_of_removed_composers(tmp_path):
    """Tier 2: an arm belonging to a composer that no longer exists in config is
    dropped at load. Left in place it would grow the file forever and could
    never be disarmed — nothing consumes a removed composer's key."""
    path = tmp_path / STORE_FILENAME
    store = DurablePendingStore(path)
    store.put("still_configured", "j1", PendingRecord())
    store.put("renamed_away", "j2", PendingRecord())

    reloaded = DurablePendingStore(path)
    assert reloaded.retain_composers({"still_configured"}) == 1
    assert reloaded.keys("renamed_away") == []
    assert reloaded.keys("still_configured") == ["j1"]
    # And the prune is itself durable — not just an in-memory filter.
    assert DurablePendingStore(path).keys("renamed_away") == []


# ---------------------------------------------------------------------------
# Tier 1: the `durable:` config flag
# ---------------------------------------------------------------------------


def test_deadline_is_durable_by_default_and_warns_only_when_opted_out():
    """Tier 1: ``op=deadline`` parses to ``durable=True`` with no warning (the
    #3180 default), while an explicit ``durable: false`` keeps the load-time
    UserWarning that CLAUDE.md's never-a-silent-dead-man-switch rule requires.

    The warning moved from "always" to "only on opt-out" — its trigger is now
    the operator's choice, not the absence of an implementation."""
    raw = {"name": "j", "op": "deadline", "on": "a", "until": {"on": "b"}, "ttl": 60}
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any UserWarning here fails the test
        (durable_def,) = load_composers([raw])
    assert durable_def.durable is True

    with pytest.warns(UserWarning, match="crash-non-durable"):
        (opted_out,) = load_composers([{**raw, "durable": False}])
    assert opted_out.durable is False


def test_non_deadline_ops_are_not_durable_by_default():
    """Tier 1: every other op keeps the free in-process dict — losing a
    debounce buffer costs one notification, so it does not buy an fsync per
    bus event. Opting IN is still available per composer."""
    base = {"name": "d", "op": "debounce", "inputs": [{"kind": "a"}], "emit": {"kind": "composed:d"}}
    (default_def,) = load_composers([base])
    assert default_def.durable is False
    (opted_in,) = load_composers([{**base, "durable": True}])
    assert opted_in.durable is True


def test_non_boolean_durable_fails_loud_at_load_time():
    """Tier 1: a mistyped ``durable: "yes"`` is a load-time config error, never
    a silently-truthy string that makes a non-durable composer look durable."""
    raw = {"name": "j", "op": "deadline", "on": "a", "until": {"on": "b"}, "durable": "yes"}
    with pytest.raises(ComposerConfigError, match="durable"):
        load_composers([raw])


# ---------------------------------------------------------------------------
# Tier 2: routing composers to the store their `durable` flag asks for
# ---------------------------------------------------------------------------


def test_only_durable_composers_write_to_the_durable_store(tmp_path):
    """Tier 2: ``build_composers`` routes a durable composer's pending state to
    the shared durable store and leaves every other composer on its own
    in-memory dict — so a high-churn debounce cannot drag an fsync-per-event
    cost (nor its own state) into the durable file."""
    store = DurablePendingStore(tmp_path / STORE_FILENAME)
    debounce = ComposerDef(
        name="noisy", op=ComposerOp.DEBOUNCE,
        inputs=(_input("orch:noise"),),
        emit_kind="composed:noisy", policy=ComposerPolicy(ttl_seconds=10.0),
    )
    bus = HookBus()
    deadline_composer, debounce_composer = build_composers(
        [_deadline_def(), debounce], bus=bus, durable_store=store,
    )
    deadline_composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    debounce_composer.handle_event(HookEvent(kind="orch:noise", payload={}))

    reloaded = DurablePendingStore(tmp_path / STORE_FILENAME)
    assert reloaded.keys("job_overdue") == ["j1"]
    assert reloaded.keys("noisy") == []


def test_durable_composer_without_a_store_warns_instead_of_downgrading_silently(tmp_path):
    """Tier 2: constructing a durable composer in a context that has no durable
    store (a session with no per-session state dir) falls back to in-memory —
    but says so. A silent downgrade's only symptom is a dead-man monitor that
    never fires after a restart, which is indistinguishable from "nothing went
    wrong"."""
    with pytest.warns(UserWarning, match="no durable store"):
        (composer,) = build_composers([_deadline_def()], bus=HookBus(), durable_store=None)
    composer.handle_event(HookEvent(kind="orch:job_started", payload={"job_id": "j1"}))
    assert not (tmp_path / STORE_FILENAME).exists()
