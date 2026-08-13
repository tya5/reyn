"""Tier 2: #4556 — spawn_session's optional ``agent`` / ``session`` parameters.

``RouterHostAdapter.spawn_session`` always spawned a session under the
CALLER's own agent, with an auto-generated sid. #4556 adds two optional
arguments: ``agent`` (target a specific agent — restrict-only, must be
the caller itself or a transitive spawn-descendant of it, the SAME
``is_spawn_descendant`` predicate ``create_topology`` already uses for its
own subtree forge-guard) and ``session`` (a caller-chosen session id
instead of an auto-generated one).

Real ``AgentRegistry``/``Session``/``RouterHostAdapter`` construction
throughout (mirrors ``test_4200_2_spawn_time_base_dir_write.py``'s own
pattern) — no mocks. The accept-path witness for a cross-agent spawn is
``run_prompt(collect="async")`` (``RouterHostAdapter.run_prompt_async``)
actually reaching the spawned (agent, session) pair — NOT merely that
``spawn_session`` itself returned ``status: "spawned"``. A spawned session
starts its own ``session.run()`` background task immediately
(``ensure_session_running``), which makes it "self-running" — the
``run_prompt(collect="attached")`` variant refuses ANY self-running target
by design (the double-pump guard, #3978), so it cannot serve as a witness
here; ``run_prompt(collect="async")`` is the variant built to dispatch INTO
an already-running peer and is what a real LLM caller would use to reach a
sub-agent's freshly spawned session — the same production surface that
returned "not found" for #4364 C-1's un-reachable case.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML


async def _registry_with_live_coordinator(
    tmp_path: Path, *, child_of_coord: "str | None" = None,
) -> "tuple[AgentRegistry, object]":
    """A real ``AgentRegistry`` with a LIVE "coord" spawner session (spawned
    through the production ``spawn_session_recorded`` seam — mirrors
    ``test_4200_2``'s own ``_registry_with_live_parent`` helper), plus
    (optionally) a agent-profile-only descendant agent ``child_of_coord``
    already wired into "coord"'s spawn lineage via ``create_agent`` — the
    SAME seam ``create_topology``'s own subtree tests use."""
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            workspace_state_dir=tmp_path / ".reyn",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("coord")
    if child_of_coord is not None:
        await reg.create_agent(child_of_coord, parent="coord")
    sid = await reg.spawn_session_recorded(
        "coord", mode="persistent",
        presentation_consumer=None, intervention_bridge=None,
    )
    coord = reg.get_session("coord", sid)
    assert coord is not None
    return reg, coord


async def _spawn(coord: object, **kwargs) -> dict:
    """Drive the REAL, production ``RouterHostAdapter.spawn_session`` for
    *coord* — the exact site #4556's guard + wiring live at."""
    return await coord._router_host.spawn_session(
        request="p4556-child", mode="persistent", narrowing=None,
        chain_id="p4556-chain", **kwargs,
    )


# ── agent omitted — regression: still spawns under the caller, unchanged ────


@pytest.mark.asyncio
async def test_agent_omitted_still_spawns_under_the_caller(tmp_path: Path) -> None:
    """Tier 2: regression guard — omitting ``agent`` (the pre-#4556 call
    shape) spawns under the CALLER's own agent, exactly as before; the new
    ``agent`` field on the response names the caller itself."""
    reg, coord = await _registry_with_live_coordinator(tmp_path)

    result = await _spawn(coord)

    assert result["status"] == "spawned", result
    assert result["agent"] == "coord"
    assert reg.get_session("coord", result["sid"]) is not None


# ── agent = a subtree descendant — accepted, and genuinely reachable ────────


@pytest.mark.asyncio
async def test_agent_targeting_a_spawned_child_creates_a_session_reachable_via_run_prompt(
    tmp_path: Path,
) -> None:
    """Tier 2: the accept-path witness is NOT "spawn returned spawned" — it is
    a SEPARATE production call, ``run_prompt(collect="async")``, actually
    reaching the (agent, session) pair the LLM asked to target. A bug that
    silently spawned under the wrong agent, or under a ``sid`` other than the
    one requested, would surface here as ``target_session_not_found`` even
    though ``spawn_session`` itself reported success — the #4364 C-1 lesson
    (mechanism existing != reachable) applied directly."""
    reg, coord = await _registry_with_live_coordinator(tmp_path, child_of_coord="worker")

    result = await _spawn(coord, agent="worker", session="w1")

    assert result["status"] == "spawned", result
    assert result["agent"] == "worker"
    assert result["sid"] == "w1"

    witness = await coord._router_host.run_prompt_async(
        agent="worker", session="w1", prompt="hello worker",
    )
    assert witness["status"] == "started", witness
    assert witness["data"]["task_id"]


# ── agent absent (unknown name) — typed error, never a raised exception ─────


@pytest.mark.asyncio
async def test_agent_naming_a_nonexistent_agent_is_a_typed_error(tmp_path: Path) -> None:
    """Tier 2: an ``agent`` naming no agent at all is a clean, typed refusal
    — never a raised exception reaching the LLM caller."""
    reg, coord = await _registry_with_live_coordinator(tmp_path)

    result = await _spawn(coord, agent="ghost")

    assert result["status"] == "error"
    assert result["kind"] == "agent_not_found"
    assert "ghost" in result["error"]


# ── session naming a duplicate — typed error, never overwritten ─────────────


@pytest.mark.asyncio
async def test_duplicate_session_id_is_a_typed_error_not_a_raw_exception(
    tmp_path: Path,
) -> None:
    """Tier 2: the registry's own duplicate-``(name, sid)`` ``ValueError``
    (``AgentRegistry.spawn_session``'s existing guard) must be reshaped into
    the SAME typed-error-response convention as every other guard in this
    method — never let it reach the LLM as a raw, unhandled exception."""
    reg, coord = await _registry_with_live_coordinator(tmp_path)

    first = await _spawn(coord, session="dup")
    assert first["status"] == "spawned", first

    second = await _spawn(coord, session="dup")

    assert second["status"] == "error"
    assert second["kind"] == "session_already_exists"


# ── agent outside the caller's own subtree — rejected, WITH falsification ───


@pytest.mark.asyncio
async def test_agent_outside_the_callers_subtree_is_rejected(tmp_path: Path) -> None:
    """Tier 2: the C1-mirrored forge-guard — an agent NOT in the caller's own
    spawn subtree (here, an unrelated operator-top agent with no lineage
    edge to "coord" at all) is rejected, never wired into."""
    reg, coord = await _registry_with_live_coordinator(tmp_path)
    reg.create("stranger")  # operator-top, no lineage edge to "coord"

    result = await _spawn(coord, agent="stranger")

    assert result["status"] == "error"
    assert result["kind"] == "agent_outside_subtree"
    # #4575 review (lead-coder): a bare ``get_session("stranger", "main")``
    # check is vacuous — spawn_session always mints a fresh uuid4 sid unless
    # ``session`` is given (which this call doesn't), so "main" was never
    # going to match regardless of whether the guard fired. ``session_ids``
    # over the whole agent is the check that actually witnesses "nothing was
    # spawned into 'stranger' at all".
    assert reg.session_ids("stranger") == [], (
        "a rejected out-of-subtree target must not have been spawned into "
        "regardless of the error"
    )
