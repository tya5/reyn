"""Tier 2: multi-agent P7 invariant + parallel delegation tests.

Coverage audit item: Agent F flagged the multi-agent dispatch path
(topology dispatch / AgentRegistry / ChainManager) as missing Tier 2
coverage for the P7 invariant — that OS code never embeds
skill/phase/artifact-specific string literals.

Four tests are added:

1. test_two_simultaneous_delegations_both_resolve
   Two concurrent chains A→B + A→C both resolve when both peers reply.
   Pure chain-mechanics; no LLM required.

2. test_os_topology_dispatch_has_no_skill_specific_strings
   Static grep regression net: topology.py, registry.py, and
   chain_manager.py must not contain known skill names or phase-name
   patterns. A new skill introduced in OS code trips this test
   immediately (P7 detection rule).

3. test_chain_resolve_does_not_inspect_skill_name
   ChainManager.register / .resolve roundtrip with an arbitrary
   chain_id that looks like a skill name: the manager resolves it
   opaquely — no branching on the value, no rejection.

4. test_topology_permit_is_skill_agnostic
   Topology.can_send returns the same result regardless of what the
   agent names look like (names that match skill names vs. generic
   names) — topology dispatch does not inspect or filter by name
   semantics.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.chain_manager import ChainManager
from reyn.runtime.session import Session
from reyn.runtime.topology import Topology

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _make_registry_with_agents(
    tmp_path: Path,
    names: list[str],
) -> tuple[AgentRegistry, dict[str, Session], StateLog]:
    """Build a registry that holds real Sessions for each name.

    No background tasks are started — test logic drives inboxes
    synchronously.  Returns (registry, {name: session}, state_log).
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    sessions: dict[str, Session] = {}
    for name in names:
        agent_dir = tmp_path / ".reyn" / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        AgentProfile.new(name, role="").save(agent_dir)
        sess = registry.get_or_load(name)
        sess._registry = registry
        sessions[name] = sess

    return registry, sessions, state_log


# Minimal _JournalLike fake: no WAL I/O needed for ChainManager unit tests
class _NullJournal:
    """Fake journal that satisfies _JournalLike without doing I/O."""

    class _Snapshot:
        pending_chains: dict = {}

    snapshot = _Snapshot()

    async def record_chain_register(self, *, chain_id: str, fields: dict) -> None:  # noqa: D401
        pass

    async def record_chain_update(self, *, chain_id: str, fields: dict) -> None:
        pass

    async def record_chain_resolve(self, *, chain_id: str) -> None:
        pass

    async def record_chain_timeout_fired(self, *, chain_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Test 1: two simultaneous delegations both resolve
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_simultaneous_delegations_both_resolve(tmp_path: Path):
    """Tier 2: A→B and A→C delegations both resolve when each peer replies.

    Invariant (P5 + multi-agent chain mechanics): two concurrent
    delegations on the same originating agent each hold an independent
    chain in A's ChainManager. When B replies its chain resolves; when C
    replies its chain resolves independently. Neither reply leaks into the
    wrong chain, and both pending_chains entries are cleared from A's
    snapshot after both replies arrive.

    No LLM required — this exercises pure chain register/resolve mechanics
    at the Session public surface.
    """
    registry, sessions, state_log = _make_registry_with_agents(
        tmp_path, ["A", "B", "C"]
    )
    sess_a = sessions["A"]
    sess_b = sessions["B"]
    sess_c = sessions["C"]

    chain_ab = "chain-ab-001"
    chain_ac = "chain-ac-001"

    # Directly register two chains on A, simulating that A delegated to B
    # and C simultaneously (the router would do this; we bypass the LLM to
    # test pure chain mechanics).
    await sess_a.chains.register(
        chain_id=chain_ab,
        from_user=True,
        depth=1,
        original_text="task for B",
        sender=None,
        waiting_on={"B"},
        origin_agent="",
        origin_depth=0,
    )
    await sess_a.chains.register(
        chain_id=chain_ac,
        from_user=True,
        depth=1,
        original_text="task for C",
        sender=None,
        waiting_on={"C"},
        origin_agent="",
        origin_depth=0,
    )

    # Both chains must be pending now.
    assert sess_a.chains.has(chain_ab), "chain_ab must be pending after register"
    assert sess_a.chains.has(chain_ac), "chain_ac must be pending after register"

    # Resolve chain_ab (B replied).
    resolved_ab = await sess_a.chains.resolve(chain_ab)
    assert resolved_ab is not None, "resolve must return the chain for chain_ab"
    assert resolved_ab.chain_id == chain_ab

    # chain_ac must still be pending — resolve is independent.
    assert sess_a.chains.has(chain_ac), (
        "chain_ac must still be pending after chain_ab resolves"
    )
    assert not sess_a.chains.has(chain_ab), (
        "chain_ab must no longer be pending after resolve"
    )

    # Resolve chain_ac (C replied).
    resolved_ac = await sess_a.chains.resolve(chain_ac)
    assert resolved_ac is not None, "resolve must return the chain for chain_ac"
    assert resolved_ac.chain_id == chain_ac

    # Both chains are gone.
    assert not sess_a.chains.has(chain_ab)
    assert not sess_a.chains.has(chain_ac)

    # WAL must have two chain_register and two chain_resolve events.
    wal_entries = list(state_log.iter_from(0))
    register_events = [e for e in wal_entries if e.get("kind") == "chain_register"]
    resolve_events = [e for e in wal_entries if e.get("kind") == "chain_resolve"]

    register_ids = {e.get("chain_id") for e in register_events}
    resolve_ids = {e.get("chain_id") for e in resolve_events}

    assert chain_ab in register_ids and chain_ac in register_ids, (
        f"Both chains must be registered in WAL; got {register_ids}"
    )
    assert chain_ab in resolve_ids and chain_ac in resolve_ids, (
        f"Both chains must be resolved in WAL; got {resolve_ids}"
    )


# ---------------------------------------------------------------------------
# Test 2: OS topology dispatch code has no skill-specific strings (P7 grep)
# ---------------------------------------------------------------------------

# Skill names that must never appear as string literals in OS-layer modules.
# This list is the canonical set of known stdlib + internal skill names.
# Add entries here when new skills are created — a new skill appearing in
# OS code immediately triggers this regression net.
_KNOWN_SKILL_NAMES: frozenset[str] = frozenset({
    "skill_router",
    "skill_improver",
    # FP-0011: skill_narrator removed; router LLM narrates inline.
    "skill_builder",
    "skill_importer",
    "chat_compactor",
    "eval",
    "eval_builder",
    "judge_phase",
    "mcp_search",
    "skill_search",
    "read_local_files",
    "recall_docs",
    "direct_llm",
    "word_stats_demo",
    "article_generator",
    "digest_pipeline",
    "writing_review_app",
    "translate_doc",
})

# Pattern that matches phase-name literals: "<word>_phase" as a string.
# Legitimate OS strings like "next_phase" (a field name) are allowed;
# we specifically look for values like "analyze_phase", "write_phase".
_PHASE_NAME_LITERAL_RE = re.compile(r'"[a-z][a-z_]*_phase"')

# OS modules subject to P7 audit.  These are the multi-agent dispatch
# components the coverage audit identified: topology dispatch, agent
# registry, and chain manager.
_P7_AUDIT_MODULES: tuple[str, ...] = (
    "src/reyn/runtime/topology.py",
    "src/reyn/runtime/registry.py",
    "src/reyn/runtime/services/chain_manager.py",
)


def _read_source(repo_root: Path, rel_path: str) -> str:
    full = repo_root / rel_path
    return full.read_text(encoding="utf-8")


def _repo_root() -> Path:
    """Locate the repo root from the test file's location."""
    # tests/ sits one level below the repo root.
    return Path(__file__).parent.parent


def test_os_topology_dispatch_has_no_skill_specific_strings():
    """Tier 2: topology.py / registry.py / chain_manager.py contain no skill literals (P7).

    P7 detection rule: if a literal naming a specific skill appears in OS
    code it is a violation.  This test is a static grep regression net.

    Scope (three files):
      - src/reyn/runtime/topology.py  — topology dispatch (Topology.can_send)
      - src/reyn/runtime/registry.py  — AgentRegistry (topology permit, notify_chain_discarded)
      - src/reyn/runtime/services/chain_manager.py — chain lifecycle

    Two checks are performed per file:
      1. No known skill name appears as a string literal.
      2. No phase-name pattern (e.g. "analyze_phase") appears as a literal.

    If this test fails, the failing file + literal are reported so the
    developer can either (a) remove the skill-specific string from OS code
    or (b) update _KNOWN_SKILL_NAMES if a skill was renamed.
    """
    repo_root = _repo_root()
    violations: list[str] = []

    for rel_path in _P7_AUDIT_MODULES:
        try:
            source = _read_source(repo_root, rel_path)
        except FileNotFoundError:
            violations.append(f"MISSING: {rel_path}")
            continue

        # Check 1: known skill name literals
        for skill_name in _KNOWN_SKILL_NAMES:
            # Match as a quoted string literal: "skill_name" or 'skill_name'
            if f'"{skill_name}"' in source or f"'{skill_name}'" in source:
                violations.append(
                    f"P7 violation: {rel_path} contains skill name literal "
                    f"{skill_name!r}"
                )

        # Check 2: phase-name pattern literals (e.g. "analyze_phase")
        for match in _PHASE_NAME_LITERAL_RE.finditer(source):
            # Exclude false positives that are legitimate field names used
            # as dictionary keys in OS protocols: "next_phase", "entry_phase"
            literal = match.group(0)
            if literal not in ('"next_phase"', '"entry_phase"', '"input_phase"'):
                violations.append(
                    f"P7 violation: {rel_path} contains phase-name literal "
                    f"{literal}"
                )

    assert not violations, (
        "P7 violations detected in OS topology/dispatch modules:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Test 3: chain_resolve does not inspect the chain_id value
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_resolve_does_not_inspect_skill_name():
    """Tier 2: ChainManager.resolve is opaque — it does not branch on chain_id content.

    P7 invariant: the OS (ChainManager) must handle every chain_id
    uniformly, regardless of whether the value resembles a skill name,
    phase name, or any other domain concept.

    This test registers chains whose IDs look like known skill names and
    verifies that resolve() returns the correct chain in each case without
    error, filtering, or special handling.
    """
    journal = _NullJournal()
    mgr = ChainManager(
        journal=journal,
        events=None,
        chain_timeout_seconds=0,   # timeouts disabled
        max_hop_depth=5,
    )

    # Use chain_ids that deliberately look like skill names to expose any
    # domain-specific branching (P7 violation) in the manager.
    skill_like_ids = [
        "skill_router-chain-001",
        "skill_improver-abc",
        "eval-chain-xyz",
        "judge_phase-001",
        "generic-chain-999",   # control: non-skill-looking ID
    ]

    for cid in skill_like_ids:
        await mgr.register(
            chain_id=cid,
            from_user=False,
            depth=1,
            original_text="request",
            sender="upstream",
            waiting_on={"downstream"},
            origin_agent="upstream",
            origin_depth=1,
        )

    # All chains must be tracked.
    for cid in skill_like_ids:
        assert mgr.has(cid), (
            f"ChainManager.has({cid!r}) returned False; "
            "OS must track all chain_ids opaquely"
        )

    # Resolve each one — must return the correct chain, no exceptions.
    for cid in skill_like_ids:
        resolved = await mgr.resolve(cid)
        assert resolved is not None, (
            f"ChainManager.resolve({cid!r}) returned None; expected the chain"
        )
        assert resolved.chain_id == cid, (
            f"Resolved chain_id {resolved.chain_id!r} != expected {cid!r}"
        )
        assert not mgr.has(cid), (
            f"ChainManager.has({cid!r}) returned True after resolve; should be gone"
        )

    # All chains resolved — manager must be empty.
    assert mgr.all_chain_ids() == [], (
        f"Expected empty ChainManager after all resolves; "
        f"remaining: {mgr.all_chain_ids()}"
    )


# ---------------------------------------------------------------------------
# Test 4: topology permit is skill-agnostic (agent name content irrelevant)
# ---------------------------------------------------------------------------


def test_topology_permit_is_skill_agnostic():
    """Tier 2: Topology.can_send result is independent of what agent names contain.

    P7 invariant: topology dispatch (Topology.can_send) must be skill-
    and domain-agnostic. An agent named "skill_router" or "eval_phase"
    must receive the same routing decision as one named "alice" — the OS
    must not inspect or filter based on name content.

    This test creates network and team topologies with agent names that
    deliberately resemble skill names and verifies that can_send returns
    the expected topology-kind result, identical to the result for
    generic names.
    """
    # Network topology: all pairs can communicate.
    network = Topology(
        name="test-net",
        kind="network",
        members=("skill_router", "skill_improver", "eval"),
    )
    # All directed pairs in a network topology should be permitted.
    skill_pairs = [
        ("skill_router", "skill_improver"),
        ("skill_improver", "eval"),
        ("eval", "skill_router"),
    ]
    for from_a, to_b in skill_pairs:
        result = network.can_send(from_a, to_b)
        assert result is True, (
            f"Topology.can_send({from_a!r}, {to_b!r}) returned {result}; "
            "expected True for network topology regardless of agent name content"
        )

    # Verify same result for generic-named agents on the same topology kind.
    generic_net = Topology(
        name="generic-net",
        kind="network",
        members=("alice", "bob", "carol"),
    )
    for from_a, to_b in [("alice", "bob"), ("bob", "carol"), ("carol", "alice")]:
        assert generic_net.can_send(from_a, to_b) is True

    # Team topology: only leader ↔ member edges are allowed.
    # Test with skill-like leader name.
    team = Topology(
        name="test-team",
        kind="team",
        members=("skill_router", "skill_improver", "eval"),
        leader="skill_router",
    )
    # Leader can send to any member.
    assert team.can_send("skill_router", "skill_improver") is True
    assert team.can_send("skill_router", "eval") is True
    # Members cannot send to each other (not via leader).
    assert team.can_send("skill_improver", "eval") is False
    # Members can send to the leader.
    assert team.can_send("skill_improver", "skill_router") is True

    # Identical topology-kind results must hold for generic names.
    generic_team = Topology(
        name="generic-team",
        kind="team",
        members=("alice", "bob", "carol"),
        leader="alice",
    )
    assert generic_team.can_send("alice", "bob") is True
    assert generic_team.can_send("bob", "carol") is False
    assert generic_team.can_send("carol", "alice") is True

    # Confirm the two topologies produce identical can_send results for
    # structurally equivalent pairs (leader→member, member→member,
    # member→leader) — this pins the topology-kind rule, not agent names.
    assert (
        team.can_send("skill_router", "skill_improver")
        == generic_team.can_send("alice", "bob")
    ), "topology outcome must be name-content-independent"
    assert (
        team.can_send("skill_improver", "eval")
        == generic_team.can_send("bob", "carol")
    ), "topology outcome must be name-content-independent"
