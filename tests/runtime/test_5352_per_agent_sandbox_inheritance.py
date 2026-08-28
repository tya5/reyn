"""Tier 2: #5352 — per-agent sandbox-policy inheritance at spawn time.

The spawn-time priority table (architect + lead-coder, #5352 issue thread):

- **same-agent spawn** (``spawn_session``'s target agent == the spawner's own
  agent) -> the spawner's own currently-EFFECTIVE sandbox value (whatever it
  is — restrict-only, since a spawner cannot exceed its own envelope).
- **cross-agent spawn, target's own ``profile.yaml`` declares ``sandbox:``**
  -> the TARGET's declared value (same "each agent's own operator-authored
  baseline" precedent ``allowed_mcp`` already sets — not a restrict-only ∩
  against the spawner).
- **cross-agent spawn, target declares nothing** -> falls back to the
  spawner's own value (never unrestricted — an undeclared target must not
  silently escape whatever the spawner itself is under).

The mechanism mirrors #2126's own shape (``AgentRegistry.
resolved_sandbox_for`` + ``Session.apply_per_session_sandbox``, one axis over
``resolved_profile_for`` + ``apply_per_session_narrowing``): a value written to
the child's ``config.yaml`` is inert until re-resolved WITH the sid and
re-injected into the LIVE session, before the caller's first turn. The last
test in this module (``test_d_...``) is the one that would have caught the
#2126 failure mode reappearing on this axis: it reads the spawned session's
OWN ``_sandbox_config`` property directly, immediately after spawn — not just
the config.yaml on disk.

Real objects throughout — real ``AgentRegistry``/``Session``/
``RouterHostAdapter`` construction, real filesystem ``tmp_path`` roots, driven
through the production ``RouterHostAdapter.spawn_session`` entry point (the
``spawn_session`` LLM tool's dispatch target). No mocks.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


def _declare_sandbox(reg: AgentRegistry, name: str, sandbox: dict) -> None:
    """Write *sandbox* into ``name``'s own ``profile.yaml`` — the agent-layer
    declaration ``AgentProfile.sandbox`` reads (#5352)."""
    agent_dir = reg.agent_workspace_dir(name)
    prof = dataclasses.replace(AgentProfile.load(agent_dir), sandbox=dict(sandbox))
    prof.save(agent_dir)


async def _registry_with_live_spawner(
    tmp_path: Path, *, agent_names: "list[str]",
) -> "tuple[AgentRegistry, object]":
    """A real ``AgentRegistry`` with each of *agent_names* created, plus a LIVE
    spawner session under ``agent_names[0]`` (itself spawned through the
    production ``spawn_session_recorded`` seam — same style as #4200 2/2's own
    fixture: the registry's agent-level "main" session is a non-loading
    accessor, never auto-constructed by ``create()`` alone)."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            workspace_base_dir=tmp_path, workspace_state_dir=tmp_path / ".reyn",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    spawner_name = agent_names[0]
    reg.create(spawner_name)
    # #4556: any additional agent must be wired into the spawner's own spawn
    # lineage (``create_agent(..., parent=spawner_name)``) — the SAME
    # ``is_spawn_descendant`` subtree forge-guard ``create_topology`` uses —
    # or ``RouterHostAdapter.spawn_session``'s ``agent=`` argument refuses it
    # as ``agent_outside_subtree`` before the #5352 priority table is ever
    # reached (mirrors ``test_4556``'s own fixture).
    for name in agent_names[1:]:
        await reg.create_agent(name, parent=spawner_name)
    sid = await reg.spawn_session_recorded(
        spawner_name, mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    spawner = reg.get_session(spawner_name, sid)
    assert spawner is not None
    return reg, spawner


async def _spawn(spawner: object, *, agent: "str | None" = None) -> dict:
    """Drive the REAL, production ``RouterHostAdapter.spawn_session`` — the
    exact site the #5352 priority table's resolution lives at."""
    return await spawner._router_host.spawn_session(
        request="p5352-child", mode="persistent", narrowing=None,
        chain_id="p5352-chain", agent=agent,
    )


# ── Gate 1: same-agent spawn inherits the spawner's own effective value ────


@pytest.mark.asyncio
async def test_a_same_agent_spawn_inherits_the_spawners_effective_sandbox(
    tmp_path: Path,
) -> None:
    """Tier 2: the spawner's own agent (``worker``) declares a sandbox
    narrowing in its ``profile.yaml``; a same-agent child spawn (no ``agent=``
    argument — the reflexive default) is born under that SAME resolved value,
    persisted into the child's own config.yaml."""
    reg, spawner = await _registry_with_live_spawner(tmp_path, agent_names=["worker"])
    _declare_sandbox(reg, "worker", {"network": False})

    result = await _spawn(spawner, agent=None)

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    assert cfg.is_file()
    import yaml
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["sandbox"] == {"network": False}


# ── Gate 2: cross-agent spawn, target declares its own -> target's wins ────


@pytest.mark.asyncio
async def test_b_cross_agent_spawn_onto_a_declaring_target_uses_the_targets_own_value(
    tmp_path: Path,
) -> None:
    """Tier 2: a cross-agent spawn onto ``worker`` (whose OWN profile.yaml
    declares ``{"network": False}``) is born under THAT value, even though the
    spawner (``coordinator``) is itself under a DIFFERENT declared value
    (``{"subprocess": False}``) — the target's own baseline wins, it is not a
    restrict-only ∩ against the spawner."""
    reg, spawner = await _registry_with_live_spawner(
        tmp_path, agent_names=["coordinator", "worker"],
    )
    _declare_sandbox(reg, "coordinator", {"subprocess": False})
    _declare_sandbox(reg, "worker", {"network": False})

    result = await _spawn(spawner, agent="worker")

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    import yaml
    assert yaml.safe_load(cfg.read_text(encoding="utf-8"))["sandbox"] == {"network": False}


# ── Gate 3: cross-agent spawn, target declares nothing -> spawner's value ──


@pytest.mark.asyncio
async def test_c_cross_agent_spawn_onto_an_undeclaring_target_falls_back_to_the_spawner(
    tmp_path: Path,
) -> None:
    """Tier 2: a cross-agent spawn onto ``worker`` (which declares NO
    ``sandbox:`` at all) does NOT come up unrestricted — it falls back to the
    spawner's own currently-effective value, never to ``None``."""
    reg, spawner = await _registry_with_live_spawner(
        tmp_path, agent_names=["coordinator", "worker"],
    )
    _declare_sandbox(reg, "coordinator", {"subprocess": False})
    # "worker" declares nothing (its profile.yaml has no sandbox: key).

    result = await _spawn(spawner, agent="worker")

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    import yaml
    persisted = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert persisted.get("sandbox") == {"subprocess": False}, (
        f"an undeclared cross-agent target came up as {persisted.get('sandbox')!r} "
        "instead of inheriting the spawner's own value — this is the "
        "'falls back to unrestricted' regression the priority table forbids"
    )


# ── Gate 4 (load-bearing): already narrower on the FIRST turn, not just on disk ──


@pytest.mark.asyncio
async def test_d_the_spawned_session_is_already_narrower_on_its_first_turn(
    tmp_path: Path,
) -> None:
    """Tier 2: the #2126-shaped re-injection pitfall, for the sandbox axis.
    Writing the resolved value to config.yaml is not by itself enforcement —
    this reads the REAL production op-context the spawned session's OWN
    ``RouterOpContextSource`` builds (``session._router_op_context_source.
    build().sandbox_config`` — the SAME public-surface-adjacent read
    ``test_4200_2_spawn_time_base_dir_write.py``'s own last test uses for
    ``base_dir``, the production consumer's own view, not a raw yaml
    re-parse), with NO intervening reconstruction/reload, immediately after
    the spawn call returns. A regression that persists the file but forgets
    to call ``apply_per_session_sandbox`` would leave this GREEN-looking
    write inert (the live session would still report the agent's
    un-narrowed baseline, or the process-wide default) — exactly the
    failure mode #2126 named."""
    reg, spawner = await _registry_with_live_spawner(
        tmp_path, agent_names=["coordinator", "worker"],
    )
    _declare_sandbox(reg, "worker", {"network": False})

    result = await _spawn(spawner, agent="worker")
    assert result["status"] == "spawned", result

    child = reg.get_session("worker", result["sid"])
    assert child is not None
    live_sandbox_config = child._router_op_context_source.build().sandbox_config
    live_policy = live_sandbox_config.policy if live_sandbox_config is not None else None
    assert live_policy == {"network": False}, (
        f"the spawned session's LIVE op-context sandbox_config.policy is "
        f"{live_policy!r} — the resolved narrowing was persisted to "
        "config.yaml but never re-injected into the live session (the "
        "#2126 failure mode, on the sandbox axis)"
    )


# ── Regression guard: no sandbox anywhere -> nothing written, nothing changes ──


@pytest.mark.asyncio
async def test_e_no_declared_sandbox_anywhere_writes_nothing_and_changes_nothing(
    tmp_path: Path,
) -> None:
    """Tier 2: byte-identical-to-pre-#5352 default — an agent tree that
    declares no ``sandbox:`` anywhere spawns a child with no ``sandbox`` key in
    its config.yaml at all, and the child's live op-context ``sandbox_config``
    is whatever the process-wide default already was (``None`` here — no
    ``sandbox_config`` was ever wired for these test sessions)."""
    reg, spawner = await _registry_with_live_spawner(tmp_path, agent_names=["worker"])

    result = await _spawn(spawner, agent=None)

    assert result["status"] == "spawned", result
    cfg = reg._session_state_dir("worker", result["sid"]) / "config.yaml"
    if cfg.is_file():
        import yaml
        assert "sandbox" not in (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {})
    child = reg.get_session("worker", result["sid"])
    assert child is not None
    live_sandbox_config = child._router_op_context_source.build().sandbox_config
    assert live_sandbox_config is None
