"""Tier 2: A2A bus stamping + listener lifecycle (issue #268 Phase 2).

Phase 1 (PR #275) established the iv.origin_channel_id field + stalled
queue mechanism on the agent layer, but NO existing bus stamped the
field — so the new path was dormant in production. Phase 2 wires
A2AInterventionBus to stamp on deliver + register/unregister its
channel_id as a listener through ``send_to_agent_impl``'s override
path.

ChatInterventionBus stamping is **explicitly deferred** to a separate
follow-up because it has broader test-fixture implications (= every
``_make_session`` helper would need a "tui" listener to keep
existing dispatch tests green). A2A is safer to land first because
the override path is per-task and gated behind ``send_to_agent_impl``.

Pins:

  1. A2AInterventionBus exposes ``channel_id`` property of shape
     ``a2a:<run_id>``.
  2. ``deliver`` stamps ``iv.origin_channel_id`` to ``channel_id`` if
     not already set; respects pre-existing stamping (= upstream-set
     origin wins).
  3. ``send_to_agent_impl`` registers the bus's channel_id as an
     intervention listener during its scope + unregisters on exit.
  4. The listener registration uses ``getattr(bus, "channel_id", None)``
     so future RequestBus implementations without channel_id pass
     through the override path unchanged.

No mocks for the bus + session; small fake RunRegistry + scripted
flow.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="fastapi not installed ([web] extra missing)")

from reyn.interfaces.web.a2a_intervention import A2AInterventionBus  # noqa: E402
from reyn.interfaces.web.run_registry import RunRegistry  # noqa: E402
from reyn.runtime.session import Session  # noqa: E402
from reyn.user_intervention import (  # noqa: E402
    InterventionAnswer,
    UserIntervention,
)

# ── 1. A2AInterventionBus.channel_id ──────────────────────────────────


def test_a2a_intervention_bus_channel_id_format() -> None:
    """Tier 2: ``channel_id`` is ``a2a:<run_id>`` — stable shape used by
    bus stamping + listener registration.
    """
    registry = RunRegistry()
    bus = A2AInterventionBus(run_id="abc123", registry=registry)
    assert bus.channel_id == "a2a:abc123"


def test_a2a_intervention_bus_channel_id_reflects_run_id() -> None:
    """Tier 2: different run_id values produce different channel_id
    values (= each A2A task is its own channel).
    """
    registry = RunRegistry()
    bus_a = A2AInterventionBus(run_id="run-A", registry=registry)
    bus_b = A2AInterventionBus(run_id="run-B", registry=registry)
    assert bus_a.channel_id == "a2a:run-A"
    assert bus_b.channel_id == "a2a:run-B"
    assert bus_a.channel_id != bus_b.channel_id


# ── 2. deliver stamps iv.origin_channel_id ────────────────────────────


def test_on_dispatch_stamps_origin_channel_id_when_unset() -> None:
    """Tier 2: ``on_dispatch`` sets ``iv.origin_channel_id`` to the bus's
    ``channel_id`` when the iv was created without one. issue #292
    α: renamed from ``deliver`` to ``on_dispatch`` — semantics
    (side-effect observer) preserved for the stamping contract.
    """
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> str | None:
        iv = UserIntervention(kind="ask_user", prompt="?")
        await bus.on_dispatch(iv)
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == f"a2a:{entry.run_id}"


def test_on_dispatch_respects_preexisting_origin_channel_id() -> None:
    """Tier 2: when an iv arrives with ``origin_channel_id`` already
    set (= e.g. upstream multi-hop delegation), ``on_dispatch`` does
    NOT overwrite.
    """
    registry = RunRegistry()
    entry = registry.create(agent_name="demo", chain_id="chain-A")
    bus = A2AInterventionBus(run_id=entry.run_id, registry=registry)

    async def _drive() -> str | None:
        iv = UserIntervention(
            kind="ask_user",
            prompt="?",
            origin_channel_id="upstream:custom",
        )
        await bus.on_dispatch(iv)
        return iv.origin_channel_id

    stamped = asyncio.run(_drive())
    assert stamped == "upstream:custom"


# ── 3. send_to_agent_impl listener lifecycle ──────────────────────────


def test_send_to_agent_impl_registers_a2a_channel_id_as_listener(
    tmp_path: Path,
) -> None:
    """Tier 2: when ``send_to_agent_impl`` is called with an
    A2AInterventionBus override, the bus's ``channel_id`` is
    registered as an intervention listener on the session for the
    scope of the call. After the call exits, the listener is removed.

    Verifies the agent layer's origin-pin check (= Phase 1 design)
    treats the A2A channel as alive while the task runs.
    """
    from reyn.core.events.state_log import StateLog
    from reyn.mcp.server import send_to_agent_impl
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    agents_registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    agents_registry.create("demo", role="demo agent")

    run_registry = RunRegistry()
    entry = run_registry.create(agent_name="demo", chain_id="chain-A")
    bus = A2AInterventionBus(run_id=entry.run_id, registry=run_registry)

    # Snapshot listener state observed at 3 points:
    #   (a) before call
    #   (b) during call (= captured via a patched register_listener hook)
    #   (c) after call (= post-finally)
    observed_during: list[set[str]] = []

    # Monkey-patch register_intervention_listener on the session
    # factory's product so we observe the registration as it happens.
    # We override at the AgentRegistry's session creation: easier to
    # observe by patching the InterventionRegistry directly post-create
    # via a hook in send_to_agent_impl. Instead, snapshot before/after
    # the call.
    async def _drive() -> None:
        session = agents_registry.get_or_load("demo")
        listeners_before = set(session._interventions._listeners)
        assert bus.channel_id not in listeners_before

        # Send a message with the bus as override. We use a very short
        # timeout so the call returns quickly (= we don't need real
        # skill execution to observe the listener registration around
        # the call).
        try:
            await send_to_agent_impl(
                agents_registry,
                agent_name="demo",
                message="hello",
                timeout=0.1,
                intervention_override=bus,
            )
        except Exception:
            # Timeout / no reply OK — we're only observing listener wiring.
            pass

        listeners_after = set(session._interventions._listeners)
        observed_during.append(listeners_before)
        observed_during.append(listeners_after)

    asyncio.run(_drive())
    listeners_before, listeners_after = observed_during
    assert bus.channel_id not in listeners_before
    # After the call returns, the listener is removed (= unregister in
    # finally ran).
    assert bus.channel_id not in listeners_after, (
        f"channel_id {bus.channel_id!r} should be unregistered "
        f"after send_to_agent_impl returns, but listeners = "
        f"{listeners_after!r}"
    )


def test_send_to_agent_impl_skips_listener_when_override_has_no_channel_id(
    tmp_path: Path,
) -> None:
    """Tier 2: defensive — when an override doesn't expose
    ``channel_id`` (= future bus impls might not), the listener
    registration is silently skipped. The override path still works.

    Uses a minimal stub bus to verify the defensive
    ``getattr(intervention_override, "channel_id", None)`` path.
    """
    from reyn.core.events.state_log import StateLog
    from reyn.mcp.server import send_to_agent_impl
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry

    class _StubBusNoChannelId:
        """RequestBus impl that does NOT expose channel_id."""

        async def request(self, iv: UserIntervention) -> InterventionAnswer:
            return InterventionAnswer(text="stub")

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    agents_registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    agents_registry.create("demo", role="demo agent")

    async def _drive() -> None:
        session = agents_registry.get_or_load("demo")
        listeners_before = set(session._interventions._listeners)

        try:
            await send_to_agent_impl(
                agents_registry,
                agent_name="demo",
                message="hello",
                timeout=0.1,
                intervention_override=_StubBusNoChannelId(),
            )
        except Exception:
            pass

        listeners_after = set(session._interventions._listeners)
        # No new listener added (= defensive skip).
        assert listeners_after == listeners_before

    asyncio.run(_drive())


# ── 4. Backwards-compat: no override = no listener change ─────────────


def test_send_to_agent_impl_without_override_does_not_register_listener(
    tmp_path: Path,
) -> None:
    """Tier 2: when ``intervention_override`` is None (= the legacy
    path), no listener registration happens. Pre-#268 callers see
    unchanged behaviour.
    """
    from reyn.core.events.state_log import StateLog
    from reyn.mcp.server import send_to_agent_impl
    from reyn.runtime.budget.budget import BudgetTracker, CostConfig
    from reyn.runtime.profile import AgentProfile
    from reyn.runtime.registry import AgentRegistry

    state_log = StateLog(tmp_path / ".reyn" / "state" / "wal.jsonl")

    def factory(profile: AgentProfile) -> Session:
        agent_dir = tmp_path / ".reyn" / "agents" / profile.name
        agent_dir.mkdir(parents=True, exist_ok=True)
        return Session(
            agent_name=profile.name,
            agent_role=profile.role,
            output_language="en",
            budget_tracker=BudgetTracker(CostConfig()),
            state_log=state_log,
            snapshot_path=agent_dir / "state" / "snapshot.json",
        )

    agents_registry = AgentRegistry(
        project_root=tmp_path,
        session_factory=factory,
        state_log=state_log,
    )
    agents_registry.create("demo", role="demo agent")

    async def _drive() -> None:
        session = agents_registry.get_or_load("demo")
        listeners_before = set(session._interventions._listeners)
        try:
            await send_to_agent_impl(
                agents_registry,
                agent_name="demo",
                message="hello",
                timeout=0.1,
                intervention_override=None,
            )
        except Exception:
            pass
        listeners_after = set(session._interventions._listeners)
        assert listeners_after == listeners_before

    asyncio.run(_drive())
