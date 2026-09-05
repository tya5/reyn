"""Tier 2: OS invariant -- #5786, a real, reachable correctness bug found
while building ADR-0047 (#5769) acceptance item 3's own witness (not a
missing-witness gap -- the bug pre-dates this PR, on `origin/main` since
#5778 introduced the scoped-checkout writer).

`snapshot_generations.reconstruct()` used to derive `is_active` from
scope-BLIND `_abandoned_intervals(_rewind_records(state_log))` --
stripping every reset-record's own `scope` field and treating it as
GLOBAL regardless. Both real callers (`AgentRegistry._materialize_
rewind`'s scoped branch AND its GLOBAL per-session loop) already build a
correctly-scoped `build_active_predicate(state_log, scope=...)` for
their own spawn/vanish gate checks one line above their call into
`reconstruct` -- but the computed predicate was discarded, and
`reconstruct` recomputed (and used) the wrong, unscoped one instead.

Effect: a session-scoped checkout's reset-record was silently treated as
a GLOBAL abandonment when a DIFFERENT, unrelated agent/session was later
reconstructed, hiding that session's own real WAL entries -- exactly the
cross-session corruption ADR-0047 decision 5 exists to rule out.

`reconstruct` now takes `scope` as a required keyword-only argument (no
default -- matching every other #5769 seam's own contract): `GLOBAL_
SCOPE` for the GLOBAL per-session loop's own per-`(name, sid)` call, a
real `(agent, sid)` for the scoped branch.

`test_scoped_rewind_of_a_leaves_bs_own_received_message_untouched` is
simultaneously the bug's own regression test AND ADR-0047 acceptance
item 3's witness ("An A->B message is B's WAL entry (routed by target).
Rewinding A alone leaves B holding a message A no longer remembers
sending.") -- driven end to end via `AgentRegistry.checkout`
(`event_route_key` routes an event by `target` first, falling back to
`agent`; a real `chain_register` event, routed via `agent` per
`SnapshotJournal`'s own production append, is A's own "I registered
this delegation" record, distinct from B's own `inbox_put`, routed by
`target=B`, for the delivered task). Confirmed genuinely RED before the
fix (strip-falsified: temporarily reverted `reconstruct`'s own
`is_active` line back to the scope-blind form via in-file `Edit` --
B's reconstructed inbox came back empty instead of holding its own
message -- restored, green again).

Acceptance item 10 (decision 6, agent existence/`.archived` untouched)
is a SEPARATE, independent witness -- already correctly implemented,
unaffected by this bug, and deliberately left out of this PR's scope
(lead-coder-30's explicit call: keep this PR to the one fix + its one
directly-dependent witness) -- tracked as a quick follow-up.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import reconstruct
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


def _snap_path(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name / "state" / "snapshot.json"


def _inbox_ids(snap: AgentSnapshot) -> "list[str]":
    return [m["id"] for m in snap.inbox]


async def _put(log: StateLog, agent: str, text: str) -> int:
    return await log.append(
        "inbox_put", target=agent, msg_id=text, msg_kind="user",
        payload={"text": text},
    )


@pytest.mark.asyncio
async def test_reconstruct_requires_scope_kwarg(tmp_path):
    """Tier 2: no default -- same shape as every other #5769 seam's own
    required-kwarg contract. Pins OUR signature decision (a forgotten
    scope here silently treats a scoped reset-record as global,
    corrupting an unrelated session's reconstruction -- the #5786 bug),
    not a language behaviour."""
    from reyn.core.events.snapshot_generations import SnapshotGenerationStore

    log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    store = SnapshotGenerationStore("alpha", tmp_path / "generations")

    with pytest.raises(TypeError):
        reconstruct("alpha", store, log, 0)  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_scoped_rewind_of_a_leaves_bs_own_received_message_untouched(tmp_path):
    """Tier 2: #5786's own regression test AND ADR-0047 acceptance item 3's
    witness, driven end to end. See module docstring."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log

    # A's own memory BEFORE the delegation -- kept (the rewind target below
    # is exactly this seq).
    kept_seq = await _put(log, "alpha", "a-earlier")  # seq 1

    # A registers a pending chain waiting on B -- A's own "I sent this"
    # record (chain_register routes via `agent`, matching
    # SnapshotJournal's real production append: `agent=self._agent_name`).
    await log.append(
        "chain_register", agent="alpha", session_id="main",
        chain_id="c1", origin_depth=0, original_request="do X",
        waiting_on=["beta-task-1"], task_kind="prompt",
    )

    # B receives the delegated task -- B's OWN WAL entry (routed by target).
    await _put(log, "beta", "beta-task-1")

    # Scoped checkout: rewind ALPHA alone back to BEFORE the chain_register.
    await reg.checkout(kept_seq, scope=("alpha", "main"))

    alpha_snap = AgentSnapshot.load("alpha", _snap_path(tmp_path, "alpha"))
    assert _inbox_ids(alpha_snap) == ["a-earlier"]
    assert alpha_snap.pending_chains == {}  # A forgot registering the delegation

    # B, reconstructed under ITS OWN real scope -- untouched by A's scoped
    # rewind (the reset-record above carries scope=["alpha", "main"], which
    # `build_active_predicate`'s own contract makes invisible to a (beta,
    # main) query): still holds the delegated task.
    head = log.current_seq
    await reg.checkout(head, scope=("beta", "main"))
    beta_snap = AgentSnapshot.load("beta", _snap_path(tmp_path, "beta"))
    assert _inbox_ids(beta_snap) == ["beta-task-1"]


@pytest.mark.asyncio
async def test_bs_message_survives_wal_truncation_past_the_scoped_rewind(tmp_path):
    """Tier 2: MANDATORY CLAUDE.md recovery gate -- a recovery-feature PR
    needs a truncate-falsify test: set X, truncate the WAL past X's
    events, reconstruct, assert X survives. X here is the #5786 fix's own
    property: B's message stays correctly visible after A's scoped
    rewind, EVEN ONCE the WAL entries that made the bug possible (A's own
    chain_register and its scoped reset-record) are truncated away --
    proving the fix's correctness rides the self-contained snapshot +
    surviving records, not a happenstance of an untruncated WAL."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "alpha")
    _seed_agent(tmp_path, "beta")
    log = reg.state_log

    kept_seq = await _put(log, "alpha", "a-earlier")  # seq 1
    await log.append(
        "chain_register", agent="alpha", session_id="main",
        chain_id="c1", origin_depth=0, original_request="do X",
        waiting_on=["beta-task-1"], task_kind="prompt",
    )  # seq 2
    await _put(log, "beta", "beta-task-1")  # seq 3

    await reg.checkout(kept_seq, scope=("alpha", "main"))  # seq 4 (alpha's reset-record)

    beta_head = log.current_seq
    beta_result = await reg.checkout(beta_head, scope=("beta", "main"))
    beta_reset_seq = beta_result["reset_seq"]

    # More activity on the active branch, then truncate the WAL below beta's
    # OWN reset-record's seq -- dropping seq 1-4 entirely (alpha's earlier
    # message, its chain_register, its scoped reset-record, AND beta's own
    # original inbox_put) from the raw WAL. Only beta's self-contained
    # snapshot (pinned at beta_reset_seq) and whatever survives above it
    # remain.
    await log.truncate_below(beta_reset_seq)
    await log.flush()
    surviving = {e["seq"] for e in log.iter_from(0)}
    assert all(s >= beta_reset_seq for s in surviving), (
        "the WAL entries this fix depends on must be genuinely truncated away"
    )

    beta_snap = AgentSnapshot.load("beta", _snap_path(tmp_path, "beta"))
    assert beta_snap.applied_seq == beta_reset_seq
    beta_snap.apply_events(list(log.iter_from(beta_snap.applied_seq + 1)))
    assert _inbox_ids(beta_snap) == ["beta-task-1"]  # survives truncation
