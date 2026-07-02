"""Tier 2: #1726 FP-0043 Stage 3 — Registry holds N Sessions per Agent.

The structural multi-session enabler: identity (Agent, S2) is shared per name;
conversation Sessions are keyed by an opaque session-id (default "main" → N=1
byte-identical). spawn_session opens an additional Session under the SAME Agent
object. Inbound routing to non-default sessions is Stage 4 — S3 just makes the
structure hold N.

Real AgentRegistry + real Session (no mocks).
"""
from __future__ import annotations

import pytest

from reyn.runtime.budget.budget import BudgetTracker, CostConfig
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import _DEFAULT_SID, AgentRegistry
from reyn.runtime.session import Session


def _registry(tmp_path, tracker: "BudgetTracker | None" = None):
    # ONE shared BudgetTracker across all sessions — matches production (created + hydrated once at
    # the entry point, threaded to every session). agent_cost_usd reads this durable per-agent total.
    shared = tracker if tracker is not None else BudgetTracker(CostConfig())

    def factory(profile: AgentProfile):
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=shared,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=factory)
    return reg


def test_default_session_lookup_unchanged(tmp_path) -> None:
    """Tier 2: #1726 — get_or_load(name) yields the default "main" session, and
    get_session(name) / get_session(name, "main") return that SAME instance
    (the prior single-session lookup, unchanged at N=1)."""
    reg = _registry(tmp_path)
    s = reg.get_or_load("default")
    assert reg.get_session("default") is s
    assert reg.get_session("default", _DEFAULT_SID) is s
    assert reg.loaded_names() == ["default"]


def test_spawn_session_creates_distinct_session_sharing_agent(tmp_path) -> None:
    """Tier 2: #1726 — spawn_session opens an ADDITIONAL Session under the agent:
    a distinct CONVERSATION instance under the SAME identity. Observably: the
    spawned session is a different object with its own inbox, but reports the
    identical identity (agent_name/role), and the registry still lists ONE agent.
    (Impl shares the same Agent object via the S2 ``agent=`` seam — verified by
    construction in spawn_session; the frozen+private Agent isn't an observable
    surface, so the test pins the public identity-equivalence contract.)"""
    reg = _registry(tmp_path)
    main = reg.get_or_load("default")
    sid = reg.spawn_session("default")
    spawned = reg.get_session("default", sid)

    assert sid != _DEFAULT_SID
    assert spawned is not None and spawned is not main, "a distinct conversation Session"
    # Same identity (public surface), different conversation.
    assert spawned.agent_name == main.agent_name == "default"
    assert spawned.agent_role == main.agent_role, "same identity (role) as the agent"
    assert spawned.inbox is not main.inbox, "conversation (inbox) is per-session"
    # Still ONE agent in the registry (N sessions under one identity).
    assert reg.loaded_names() == ["default"]


def test_default_session_unaffected_by_spawn(tmp_path) -> None:
    """Tier 2: #1726 — spawning a second session does not disturb the default
    one (get_or_load still returns the original "main" instance)."""
    reg = _registry(tmp_path)
    main = reg.get_or_load("default")
    reg.spawn_session("default")
    assert reg.get_or_load("default") is main
    assert reg.get_session("default") is main


@pytest.mark.asyncio
async def test_attach_session_focuses_existing_and_rejects_unknown(tmp_path) -> None:
    """Tier 2: #1726 Stage 4a — attach_session focuses an EXISTING session of the
    agent (public attached_name/attached_sid/attached_session reflect it) and
    raises KeyError for an unknown sid (the graceful-error substrate the /session
    handler + forwarder rely on). No build — the session must already exist."""
    reg = _registry(tmp_path)
    reg.get_or_load("default")
    sid = reg.spawn_session("default")
    try:
        focused = await reg.attach_session("default", sid)
        assert reg.attached_name == "default"
        assert reg.attached_sid == sid
        assert reg.attached_session() is focused
        assert focused is reg.get_session("default", sid)

        with pytest.raises(KeyError):
            await reg.attach_session("default", "no-such-sid")
    finally:
        for task in reg.running_tasks():
            task.cancel()


def test_agent_cost_usd_reads_durable_per_agent_total(tmp_path):
    """Tier 2: agent_cost_usd() reads the DURABLE per-agent total (ledger-hydrated BudgetTracker) —
    ONE counter that already reflects cost across ALL sessions of the agent. So it (a) survives
    restart (the counter is ledger-derived, not live-gateway) and (b) does NOT N×-count multiple
    sessions (still one counter, not a sum over per-session gateways).

    Falsification: on the pre-fix impl (a SUM over per-session ``sess.total_cost_usd`` gateways),
    this ledger-only cost reports 0.0 (no live gateway accumulation) → the 0.15 assert fails; and a
    per-session-seed variant would report 0.30 across the two sessions (N×)."""
    import json
    from datetime import datetime, timezone

    # A ledger with two recorded LLM calls for agent "default" (all-time cumulative 0.10 + 0.05).
    ledger = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    ledger.write_text(
        json.dumps({"ts": ts, "agent": "default", "tokens": 100, "cost_usd": 0.10}) + "\n"
        + json.dumps({"ts": ts, "agent": "default", "tokens": 50, "cost_usd": 0.05}) + "\n",
        encoding="utf-8",
    )
    tracker = BudgetTracker(CostConfig())
    tracker.hydrate(ledger)  # = the restart restore path

    reg = _registry(tmp_path, tracker=tracker)
    reg.get_or_load("default")
    reg.spawn_session("default")  # a 2nd session — must NOT double the reported per-agent cost

    assert reg.agent_cost_usd("default") == pytest.approx(0.15), (
        "durable per-agent total (all sessions, one counter) — not reset to 0, not N×-summed"
    )
    assert reg.agent_tokens("default") == 150, "durable per-agent total tokens (companion accessor)"


def test_agent_cost_usd_survives_restart_and_byte_aligns_no_ntimes(tmp_path):
    """Tier 2: the fix's core (owner bug) — cost recorded pre-restart SURVIVES a restart (a fresh
    tracker hydrated from the persisted ledger = the real restore path) and byte-aligns with the
    tracker's own per-agent total (= what ``/cost`` reads), with NO N× across sessions.

    Falsification: the pre-fix summed live per-session gateways → 0.0 after restart (RED), and a
    per-session-seed variant → 3× across the three sessions (RED)."""
    import json
    from datetime import datetime, timezone

    ledger = tmp_path / ".reyn" / "state" / "budget_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    ledger.write_text(
        json.dumps({"ts": ts, "agent": "default", "tokens": 200, "cost_usd": 0.42}) + "\n",
        encoding="utf-8",
    )
    # "Restart": a brand-new tracker hydrated from the persisted ledger.
    restarted = BudgetTracker(CostConfig())
    restarted.hydrate(ledger)

    reg = _registry(tmp_path, tracker=restarted)
    reg.get_or_load("default")
    reg.spawn_session("default")
    reg.spawn_session("default")  # 3 sessions total — must not multiply the per-agent cost

    assert reg.agent_cost_usd("default") == pytest.approx(0.42), "survives restart (not 0), no N×"
    assert reg.agent_cost_usd("default") == restarted.agent_cost_usd("default"), (
        "status-bar per-agent cost byte-aligns with the /cost source (the durable tracker)"
    )
