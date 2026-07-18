"""#3049 — a pipeline DRIVER session's router-op intervention reaches the pipeline ORIGINATOR.

The RAG-ingest hang: the ``rag_ingest`` X1 pre-flight fans out ``call_mcp_tool`` reachability
probes. Each probe's ``require_mcp`` permission gate raised a ``permission.generic`` intervention
on a bus the driver session HARDCODED to itself (``Session._mcp_call_tool`` and its 4 MCP-op
siblings built ``ChatInterventionBus(self, ...)`` directly, bypassing the session's
``_intervention_bridge``). So on an ATTACHED driver (spawned ``BridgeToParent`` — see
``_spawn_pipeline_driver_session``) the prompt landed on the driver's OWN listener-less
``InterventionRegistry`` instead of the operator's parent surface: dispatched, parked stalled,
and ``await``-ed forever (the confirmed indefinite hang, orphan-future id-matched 18/18 to the
stalled ``permission.generic`` interventions in the live-process measurement on #3049).

The fix routes every router-op intervention bus through ``Session._make_router_intervention_bus``
— bridge-aware (attached driver → parent's live listener; root/detached → self-bound /
fail-close), the SAME resolution the ``RouterHostAdapter`` intervention-bus factory already used.
One construction seam, uniform across every IV-raising router-op leaf — exactly as a ``tool:
ask_user`` step already reached the parent (it never overrode the bridge-aware bus, which is why
``ask_user`` worked while ``call_mcp_tool`` orphaned).

Real ``AgentRegistry`` / ``Session`` / ``PermissionResolver`` / bridge + intervention machinery —
no collaborator mocks. The permission gate (``require_mcp`` / ``require_tool``) is driven for real
and its ``permission.generic`` intervention is witnessed CROSS-SESSION on the originator's own
active queue (a single-session harness could never witness this — the bug is precisely that the
prompt lands on the WRONG session's registry; #3037-inverse). Mirrors the operator-reach pattern
of ``test_2708_p32a_spawn_bridge_intervention`` / ``test_2769_agent_step_intervention_reaches_invoker``.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.intervention_choices import YES, generic_yn_choices
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.runtime.session_api import _build_agent_step_narrowing, spawn_ephemeral_session
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface, BridgeToParent
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver

_SERVER = "reyn3049_probe_server"
_TOOL = "reyn3049_probe_tool"


def _registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + a Session factory that forwards BOTH spawn overrides, so the
    driver spawn threads a parent-bound ``intervention_bridge`` exactly as production does."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            non_interactive=True, presentation_wiring=PresentationWiring(presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


async def _spawn_driver(reg: "AgentRegistry", routing) -> Session:
    """A pipeline driver session spawned with ``routing``'s intervention bridge — the SAME
    spawn shape ``_spawn_pipeline_driver_session`` uses (BridgeToParent attached / AuditOnly
    detached)."""
    sid = await spawn_ephemeral_session(
        reg, identity="worker", narrowing=_build_agent_step_narrowing(None),
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    driver = reg.get_session("worker", sid)
    assert driver is not None
    return driver


def _cancel_tasks(reg: "AgentRegistry") -> None:
    for task in list(reg._tasks.values()):  # noqa: SLF001 — teardown precedent (sibling tests)
        if not task.done():
            task.cancel()


# ── Attached: a driver-session MCP permission prompt reaches the originator operator ──────


@pytest.mark.asyncio
async def test_attached_driver_mcp_permission_reaches_originator_operator(tmp_path: Path) -> None:
    """Tier 2: an ATTACHED pipeline driver's ``call_mcp_tool`` permission gate — driven for real
    through ``PermissionResolver.require_mcp`` on the bus the driver's MCP op builds
    (``_make_router_intervention_bus``) — delivers its ``permission.generic`` prompt to the
    ORIGINATOR operator's live listener, and the operator's grant flows back so the call proceeds.
    RED before the fix: the MCP op hardcoded ``ChatInterventionBus(self, ...)`` → the prompt
    orphaned on the driver's own listener-less registry (stalled + awaited forever = the #3049 hang),
    never reaching the parent."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    originator = reg.get_or_load("worker")
    originator.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    driver = await _spawn_driver(reg, BridgeToParent(originator))

    # The EXACT bus the driver's _mcp_call_tool builds for the permission gate (the production seam).
    bus = driver._make_router_intervention_bus()  # noqa: SLF001 — production op-ctx construction seam
    resolver = PermissionResolver({}, project_root=tmp_path, interactive=True)
    decl = PermissionDecl(mcp=[_SERVER])  # statically authorized, not pre-approved → prompts

    gate = asyncio.ensure_future(resolver.require_mcp(decl, _SERVER, bus))

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not originator.interventions.list_active():
        await asyncio.sleep(0.02)
    assert originator.interventions.list_active(), (
        "the driver's MCP permission prompt never reached the ORIGINATOR operator's active queue — "
        "it orphaned on the driver's own listener-less registry (the #3049 stall)."
    )
    # Delivered to the LIVE listener, not parked stalled on the parent (the bridge stamps the
    # channel the operator actually listens on).
    assert originator.list_stalled_interventions() == [], (
        "the bridged MCP prompt was parked in the originator's stalled queue instead of its live "
        "listener."
    )

    # The operator grants on the originator surface; require_mcp returns (allow), no hang.
    consumed = await originator.answer_oldest_intervention_choice(YES)
    assert consumed is True
    await asyncio.wait_for(gate, timeout=5.0)  # returns None on allow; MUST NOT raise/hang

    _cancel_tasks(reg)


@pytest.mark.asyncio
async def test_attached_driver_permission_kind_agnostic_same_bus(tmp_path: Path) -> None:
    """Tier 2: the fix is at the BUS-CONSTRUCTION seam, so it is IV-KIND-agnostic — the same
    driver bus that carries an MCP-server ``permission.generic`` also carries a tool-authority
    ``permission.generic`` (``require_tool``) to the originator. Guards that the delivery rides the
    surface, not a per-gate special-case (the whole point of the single ``_make_router_intervention_bus``
    seam: a future permission consumer inherits origin-delivery for free)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    originator = reg.get_or_load("worker")
    originator.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    driver = await _spawn_driver(reg, BridgeToParent(originator))
    bus = driver._make_router_intervention_bus()  # noqa: SLF001 — production seam
    resolver = PermissionResolver({}, project_root=tmp_path, interactive=True)
    decl = PermissionDecl(tool=[_TOOL])

    gate = asyncio.ensure_future(resolver.require_tool(decl, _TOOL, bus))
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not originator.interventions.list_active():
        await asyncio.sleep(0.02)
    assert originator.interventions.list_active(), (
        "a tool-authority permission prompt on the same driver bus did NOT reach the originator — "
        "delivery must ride the bus seam uniformly, not per permission-gate."
    )
    consumed = await originator.answer_oldest_intervention_choice(YES)
    assert consumed is True
    await asyncio.wait_for(gate, timeout=5.0)

    _cancel_tasks(reg)


# ── Detached: a headless driver's MCP permission fails closed (never reaches, never hangs) ─


@pytest.mark.asyncio
async def test_detached_driver_mcp_permission_fail_closed_deny(tmp_path: Path) -> None:
    """Tier 2: (fail-close half of the rule "no attached originator → close and answer") a DETACHED
    driver (spawned ``AuditOnlyNoSurface``, as ``start_pipeline_run`` does) builds its router-op bus
    the same way, and its MCP permission gate resolves to a typed refusal → ``require_mcp`` DENIES
    (``PermissionError``), never reaching (nor hanging on) any operator. The fix preserves the
    detached fail-close by construction (bridge is ``AuditOnlyInterventionBridge``), not by a
    per-op special case."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    # A would-be operator exists but this driver is NOT attached to it — it must never be consulted.
    originator = reg.get_or_load("worker")
    originator.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    driver = await _spawn_driver(reg, AuditOnlyNoSurface())
    bus = driver._make_router_intervention_bus()  # noqa: SLF001 — production seam
    resolver = PermissionResolver({}, project_root=tmp_path, interactive=True)
    decl = PermissionDecl(mcp=[_SERVER])

    with pytest.raises(PermissionError):
        # The AuditOnly refusal (choice_id=None) is consumed as DENY — require_mcp raises,
        # resolving immediately (no park). A refuse→allow hole would let this pass.
        await asyncio.wait_for(resolver.require_mcp(decl, _SERVER, bus), timeout=3.0)

    # The non-attached originator's live listener was never consulted.
    assert originator.interventions.list_active() == [], (
        "a detached driver's MCP prompt reached a non-attached operator — detached must fail-close "
        "locally, not bridge to an unrelated surface."
    )

    _cancel_tasks(reg)


# ── Structural: every router-op intervention bus is the single bridge-aware seam ──────────


def _self_bound_bus_construction_methods() -> "list[str]":
    """Enumerate ``Session`` methods whose body constructs a SELF-BOUND
    ``ChatInterventionBus(self, ...)`` directly (positional first arg ``self``) — derived from
    the live class source, never hand-listed, so a NEWLY-added router-op seam that hardcodes its
    own self-bound bus is caught automatically. ``_make_router_intervention_bus`` (the ONE
    sanctioned construction point, which chooses self-bound only when no bridge is present) is
    excluded — it is the seam every other method must route through."""
    src = textwrap.dedent(inspect.getsource(Session))
    tree = ast.parse(src)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "_make_router_intervention_bus":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "ChatInterventionBus"
                and call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "self"
            ):
                offenders.append(node.name)
                break
    return offenders


def test_router_op_intervention_bus_has_single_bridge_aware_seam() -> None:
    """Tier 2: #3049's fix-class guard — no ``Session`` method may hardcode a self-bound
    ``ChatInterventionBus(self, ...)`` outside ``_make_router_intervention_bus``. That helper is the
    ONE bridge-aware construction seam (attached driver → originator; root/detached → self-bound /
    fail-close), so EVERY IV-raising router-op leaf inherits origin-delivery by construction. A new
    MCP-style op method that builds its own self-bound bus — reintroducing the #3049 orphan for a
    driver session — makes this list non-empty and fails here. RED before the fix: the 5 MCP op
    methods (``_mcp_call_tool`` + resource/prompt siblings) each hardcoded the self-bound bus."""
    offenders = _self_bound_bus_construction_methods()
    assert offenders == [], (
        "these Session methods construct a self-bound ChatInterventionBus(self, ...) directly, "
        "bypassing the bridge-aware _make_router_intervention_bus seam — a driver session's "
        f"intervention raised there would orphan on its own registry (#3049): {sorted(set(offenders))}"
    )

    # Vacuity guard: the helper itself MUST contain the sanctioned self-bound construction, else
    # the scan matches nothing for a trivial reason (renamed class / moved seam) and the guard is
    # inert. Assert the one legitimate site exists so "offenders == []" means "swept", not "empty scan".
    helper_src = inspect.getsource(Session._make_router_intervention_bus)
    assert "ChatInterventionBus(" in helper_src and "_intervention_bridge" in helper_src, (
        "the bridge-aware seam _make_router_intervention_bus no longer contains the guarded "
        "construction — the structural scan would be vacuously green (guard inert)."
    )
