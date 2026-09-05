"""Tier 2: OS invariant -- #5769 stage 3, checkout gains a session scope
(ADR-0047 decision 3/5, the writer + recovery half).

Real `AgentRegistry` + `StateLog` + on-disk agents (no mocks). `scope=
GLOBAL_SCOPE` is byte-identical to the pre-#5769 global cut; `scope=(name,
sid)` cancels/quiesces, appends a reset-record for, and materialises ONLY
that session -- never touching another agent/session, the workspace, or
config generations (decision 4/6).

Covers: the retention guard staying global regardless of scope (architect's
explicit ruling), a scoped checkout leaving an unrelated agent's session
completely untouched (decision 5's own "sessions can diverge" witness),
crash recovery reading `(target_n, scope)` off the latest reset-record and
materialising only that pair, the CLAUDE.md hard-rule truncate-falsify
test for a recovery-feature PR: write a scoped rewind, truncate the WAL past
its own supporting events, reconstruct from the surviving self-contained
snapshot alone, and confirm the scoped rewind's effect survives.

`scope` is a required keyword-only argument on BOTH `checkout` functions
(module-level `snapshot_generations.checkout` and
`AgentRegistry.checkout`) -- no default. An earlier draft of this PR gave
`scope` a `None` default, reasoning (by analogy with the READ-side/WRITE-side
split #5769 stage 1 drew) that "write global" is a safe fallback. Architect's
follow-up review overruled that: unlike a merely-too-broad READ, a forgotten
`scope` here WRITES a real, effectful reset-record that rewinds every session
atomically -- the exact function ADR-0047 decision 3 names as the
session-scoped-rewind boundary -- so the omission is invisible under the
shipped config, lands in the dangerous direction (silently global, not an
error), and every real caller already knows its own answer (`rewind`/
`rewind_to`/the `/rewind` slash command all name `GLOBAL_SCOPE` explicitly).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE, RewindBeyondRetentionError
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


def _inbox_ids(snap: AgentSnapshot) -> list[str]:
    return [m["id"] for m in snap.inbox]


@pytest.mark.asyncio
async def test_checkout_scope_none_writes_the_same_record_shape_as_before(tmp_path):
    """Tier 2: acceptance -- scope=GLOBAL_SCOPE leaves the existing WAL
    entry shape untouched: no `scope` key at all in the reset-record."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")

    result = await reg.checkout(1, scope=GLOBAL_SCOPE)

    entry = next(e for e in log.iter_from(0) if e.get("kind") == "rewind")
    assert "scope" not in entry
    assert "scope" not in result


@pytest.mark.asyncio
async def test_checkout_scoped_writes_the_scope_field(tmp_path):
    """Tier 2: acceptance -- `checkout(seq, scope=(name, sid))` writes that
    pair into the reset-record's own `scope` field, and returns it."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")

    result = await reg.checkout(1, scope=("alpha", "main"))

    entry = next(e for e in log.iter_from(0) if e.get("kind") == "rewind")
    assert entry.get("scope") == ["alpha", "main"]
    assert result["scope"] == ["alpha", "main"]


@pytest.mark.asyncio
async def test_checkout_retention_guard_stays_global_for_scoped_checkout(tmp_path):
    """Tier 2: architect's explicit ruling -- the retention guard is bounded
    by the SAME global WAL floor for a scoped checkout as for a global one.
    A target truncated out of the WAL is rejected either way."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")   # seq 1
    await _put(log, "alpha", "a2")   # seq 2
    await _put(log, "alpha", "a3")   # seq 3
    await log.truncate_below(3)      # drop seq 1, 2; oldest kept = 3
    await log.flush()

    with pytest.raises(RewindBeyondRetentionError):
        await reg.checkout(2, scope=("alpha", "main"))


@pytest.mark.asyncio
async def test_checkout_scoped_touches_only_its_own_session(tmp_path):
    """Tier 2: decision 5's own "sessions can diverge" property, driven
    end to end -- a checkout SCOPED to (alpha, main) rewinds alpha's own
    inbox, while a completely unrelated agent (beta) is left with NO
    on-disk snapshot at all (real witness: beta never had one written --
    a raw WAL append alone never materialises a snapshot; only
    reconstruction does -- so "beta's snapshot file does not exist" both
    BEFORE and AFTER the scoped checkout is the actual, correct
    "untouched" claim, not a stand-in for it)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    await _put(log, "alpha", "a1")   # seq 1 (kept)
    await _put(log, "alpha", "a2")   # seq 2 (abandoned by the scoped checkout)
    await _put(log, "beta", "b1")    # seq 3 (must survive untouched)

    assert not _snap_path(tmp_path, "beta").exists()  # premise: nothing written yet

    await reg.checkout(1, scope=("alpha", "main"))

    alpha_snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(alpha_snap) == ["a1"]           # a2 abandoned FOR ALPHA

    # beta is completely untouched by a checkout scoped to a different
    # agent -- no snapshot was ever written for it, matching its own
    # pre-checkout state exactly.
    assert not _snap_path(tmp_path, "beta").exists()


@pytest.mark.asyncio
async def test_recover_rewind_if_needed_materialises_only_the_scoped_session(tmp_path):
    """Tier 2: ADR-0047 decision 3's recovery half. A crash mid-SCOPED-
    rewind (reset-record fsync'd, materialise not yet run) re-materialises
    ONLY that (name, sid) on restart -- an unrelated agent's own state is
    never touched by the recovery pass either."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    await _put(log, "alpha", "a1")   # seq 1
    await _put(log, "alpha", "a2")   # seq 2
    await _put(log, "beta", "b1")    # seq 3

    # Simulate the crash: append the scoped reset-record directly (mirrors
    # what checkout()'s own step 4 does), but never call _materialize_rewind
    # -- the crash landed right after the fsync, before step 5.
    from reyn.core.events.snapshot_generations import checkout as append_reset_record
    await append_reset_record(
        log, target_seq=1, scope=("alpha", "main"), supersedes=log.current_seq,
    )

    result = await reg.recover_rewind_if_needed()

    assert result is not None
    assert result["scope"] == ["alpha", "main"]
    alpha_snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(alpha_snap) == ["a1"]  # alpha recovered to as-of-scoped-target

    # beta was never touched by this recovery pass -- no snapshot was ever
    # written for it (only alpha's own materialise ran).
    assert not _snap_path(tmp_path, "beta").exists()


@pytest.mark.asyncio
async def test_recover_rewind_if_needed_stays_global_for_a_legacy_unscoped_record(tmp_path):
    """Tier 2: non-regression -- a crash mid-GLOBAL-rewind (no scope field
    at all, exactly what checkout()'s scope=GLOBAL_SCOPE path still writes)
    is still recovered via the full, unchanged materialise-everything path."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log
    await _put(log, "alpha", "a1")
    await _put(log, "alpha", "a2")
    await _put(log, "beta", "b1")

    from reyn.core.events.snapshot_generations import checkout as append_reset_record
    await append_reset_record(
        log, target_seq=1, scope=GLOBAL_SCOPE, supersedes=log.current_seq,
    )  # GLOBAL_SCOPE writes no scope field -- byte-identical to legacy

    result = await reg.recover_rewind_if_needed()

    assert result is not None
    assert result["scope"] is None
    alpha_snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(alpha_snap) == ["a1"]
    # Global recovery DOES touch beta too (its own snapshot gets written,
    # self-contained, even though beta's own content is unaffected).
    assert _snap_path(tmp_path, "beta").exists()


@pytest.mark.asyncio
async def test_scoped_rewind_survives_wal_truncation_past_its_own_events(tmp_path):
    """Tier 2: CLAUDE.md hard rule -- recovery-feature PRs need a
    truncate-falsify test in the SAME PR. Set X (a scoped checkout) ->
    truncate the WAL past X's own supporting events (the raw messages AND
    the reset-record itself) -> reconstruct from the surviving
    self-contained snapshot alone -> X survives.

    Mirrors `test_registry_rewind_to.py`'s own
    `test_rewind_to_snapshot_self_contained_for_restore_all` (the global
    case), but ACTUALLY calls `truncate_below` -- the raw evidence for
    "a2 was abandoned" (the reset-record naming target_seq=1) is
    genuinely gone from the WAL by the time reconstruction runs; only the
    self-contained snapshot (`applied_seq` pinned to the reset-record's
    own seq) carries that fact forward.
    """
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")   # seq 1 (kept)
    await _put(log, "alpha", "a2")   # seq 2 (abandoned by the scoped checkout)

    result = await reg.checkout(1, scope=("alpha", "main"))
    reset_seq = result["reset_seq"]

    # Post-rewind work, on the new active branch.
    await _put(log, "alpha", "a3")   # seq reset_seq + 1

    # Truncate the WAL PAST the scoped rewind's own supporting events --
    # a1, a2, AND the reset-record itself (seq <= reset_seq) are all
    # dropped. Only content strictly after reset_seq remains in the WAL.
    await log.truncate_below(reset_seq + 1)
    await log.flush()

    # Reconstruction now has NO access to a2, nor to the reset-record that
    # named it abandoned -- only the durable, self-contained snapshot
    # (applied_seq = reset_seq) and whatever WAL entries survived.
    saved = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert saved.applied_seq == reset_seq
    saved.apply_events(list(log.iter_from(saved.applied_seq + 1)))

    assert _inbox_ids(saved) == ["a1", "a3"]  # a2 stays gone -- the scope survived truncation


@pytest.mark.asyncio
async def test_module_level_checkout_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- a call site that forgets scope fails at the
    call, matching every other #5769/#5781 (agent, sid)-carrying seam's
    own required-kwarg contract. Pins OUR signature decision (a forgotten
    scope here WRITES a real, effectful global rewind -- the dangerous
    direction -- rather than silently reading the wrong branch), not a
    language behaviour."""
    from reyn.core.events.snapshot_generations import checkout

    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    with pytest.raises(TypeError):
        await checkout(log, target_seq=0)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_registry_checkout_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- same shape as
    `test_module_level_checkout_requires_scope_kwarg`, one layer OUT, on
    `AgentRegistry.checkout` itself (the function ADR-0047 decision 3
    names as the session-scoped-rewind boundary)."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    log = reg.state_log
    await _put(log, "alpha", "a1")

    with pytest.raises(TypeError):
        await reg.checkout(1)  # type: ignore[call-arg]
