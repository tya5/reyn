"""Tier 2: OS invariant -- ADR-0047 (#5769) acceptance item 10, the last
item blocking PROPOSED -> ACCEPTED (lead-coder-30/architect: 11/12
acceptance boxes checkable after stage 3 + #5778/#5781/#5784/#5786
landed the writer and the reconstruct scope-fix; this is the 12th and
final one).

Decision 6 ("A session-scoped rewind leaves every agent's existence and
`.archived` state untouched") was already correctly IMPLEMENTED
(`AgentRegistry._materialize_rewind`'s scoped branch returns before ever
reaching `_reconcile_archived_as_of_cut`, which is `GLOBAL_SCOPE`-gated
by design -- "archival is agent-wide, not owned by any one session," per
that method's own comment) -- it had no witness anywhere driving it end
to end.

**Why the `.archived` marker's own presence/absence is NOT, by itself,
a falsifiable discriminator here** (measured, not assumed):
`_reconcile_archived_as_of_cut` derives its answer from
`build_active_predicate(state_log, scope=GLOBAL_SCOPE)` -- a pure
function of the CURRENT global WAL state, independent of which call
site triggers it. Once "a" is genuinely archived (a real WAL record makes
it so), invoking that reconciliation AGAIN from anywhere -- including a
scoped branch that should never reach it -- recomputes the SAME correct
answer and writes the SAME marker content. A test that only re-asserts
"the marker is still present" after a scoped checkout would pass whether
or not the scoped branch correctly skips agent-lifecycle reconciliation
entirely; it cannot tell "correctly never touched" from "touched, but
happened to compute the same true answer anyway."

The real, discriminating witness is therefore FILE-TOUCH, mirroring
acceptance item 3's own already-proven pattern in
`test_5786_reconstruct_scope_aware.py`: a scoped checkout of "b" must
never even WRITE "a"'s own snapshot file at all -- if the scoped branch
fell through to `_materialize_rewind`'s GLOBAL per-session loop (the
actual shape a regression would take), "a"'s `snapshot.json` (main
session) would be rewritten as a side effect of that loop, even though
its CONTENT would still look correct. "a"'s snapshot file not existing
at all, both BEFORE and AFTER the scoped checkout, is the actual,
correct "untouched" claim. The `.archived` marker check is kept too
(it is literally what decision 6 names), but as a secondary, corroborating
assertion, not the load-bearing one.

Built as a direct structural contrast against the GLOBAL sibling test
`test_agent_lifecycle_rewind_2103_s2.py::
test_archived_state_reconciled_as_of_cut_after_archive`: the identical
WAL shape (an agent created then archived) is reconciled once GLOBALLY
first (establishing the marker as a real, driven fact -- proving the
reconciliation machinery is alive, not merely never exercised), then a
SECOND, real checkout scoped to a completely different agent's session
is driven. The population witness for the scoped checkout itself (per
tonight's own standing lesson: a test asserting something stayed put
must also show its own driving call did real work, not a no-op) is
`_snap_path(tmp_path, "b")` existing afterward with the real message in
it -- proof `reg.checkout(b_seq, scope=("b", "main"))` genuinely
materialised B's own session, not merely returned early doing nothing
at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE
from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import ARCHIVED_MARKER, AgentRegistry


def _no_factory(_profile):
    raise AssertionError("session factory must not be called in these tests")


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    return AgentRegistry(
        project_root=tmp_path, session_factory=_no_factory, state_log=state_log,
    )


def _seed_agent(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _agent_dir(tmp_path: Path, name: str) -> Path:
    return tmp_path / ".reyn" / "agents" / name


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
async def test_scoped_rewind_leaves_another_agents_archived_state_untouched(tmp_path):
    """Tier 2: ADR-0047 acceptance 10 -- decision 6's own defining witness,
    driven end to end. See module docstring for the full contrast."""
    reg = _make_registry(tmp_path)
    _seed_agent(tmp_path, "a")
    _seed_agent(tmp_path, "b")
    log = reg.state_log

    await log.append(
        "agent_created", entity_kind="agent", name="a", sid="",
        profile={"name": "a", "role": ""},
    )  # seq 1
    n_seq = await log.append("agent_archived", entity_kind="agent", name="a")  # seq 2 = N

    # Establish the archived marker via a real GLOBAL checkout first --
    # mirrors the sibling GLOBAL test's own "rewind-after-archive" shape
    # (target N = the archive event's own seq, so the abandoned interval
    # (N, R) is empty and the marker is WRITTEN, preserving the archive).
    # This is also the population witness that the reconciliation
    # machinery genuinely fires for this WAL shape -- not that nothing
    # ever exercises it.
    await reg.checkout(n_seq, scope=GLOBAL_SCOPE)
    assert (_agent_dir(tmp_path, "a") / ARCHIVED_MARKER).is_file()  # premise

    # The GLOBAL checkout above also materialised "a"'s own snapshot (its
    # per-session loop touches every present agent) -- population witness
    # that the reconciliation machinery genuinely wrote it, not that
    # nothing ever exercises this path.
    a_snap_path = _snap_path(tmp_path, "a")
    assert a_snap_path.exists()
    # Delete it now: merely re-asserting the ARCHIVED_MARKER's presence
    # afterward would NOT discriminate a real regression (see module
    # docstring) -- `_reconcile_archived_as_of_cut` is a pure function of
    # the CURRENT global WAL state, so calling it again from anywhere
    # (including a scoped branch that should never reach it) recomputes
    # the SAME correct marker content, indistinguishable from "never
    # touched." Deleting "a"'s snapshot file here and confirming it stays
    # absent is what actually discriminates "correctly never reached
    # agent-lifecycle reconciliation" from "reached it, but it happened to
    # recompute the same true answer" -- a regression that fell through to
    # the GLOBAL per-session loop would unconditionally recreate it.
    a_snap_path.unlink()

    # B's own activity, then a checkout SCOPED to B alone -- a completely
    # unrelated session.
    b_seq = await _put(log, "b", "b1")
    await reg.checkout(b_seq, scope=("b", "main"))

    # Population witness for the scoped checkout itself: it genuinely
    # materialised B's own session (not a silent no-op that would trivially
    # leave "a" untouched for the wrong reason).
    assert _snap_path(tmp_path, "b").exists()

    # Decision 6, the load-bearing assertion: a checkout scoped to a
    # DIFFERENT session never recreates/rewrites "a"'s own snapshot at all.
    assert not a_snap_path.exists()
    b_snap = AgentSnapshot.load("b", _snap_path(tmp_path, "b"))
    assert _inbox_ids(b_snap) == ["b1"]

    # Decision 6: agent "a"'s existence and .archived state are untouched by
    # a rewind scoped to a different session.
    assert (_agent_dir(tmp_path, "a") / ARCHIVED_MARKER).is_file()
    assert _agent_dir(tmp_path, "a").exists()
