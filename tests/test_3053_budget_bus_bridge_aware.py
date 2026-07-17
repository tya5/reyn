"""#3053 — the `safety.limit` cost-gate bus (`_ChatBudgetBus`) is bridge-aware.

Sibling gap to #3049/#3052: the per-LLM-call budget-exceed gate
(`_budget_exceed_allows_continue` -> `handle_limit_exceeded`) dispatches its
`safety.limit.*` "keep going?" prompt via `Session._ChatBudgetBus` (a bus
published at `Session.__init__` through `set_llm_call_limit_context`, read
back by `_budget_exceed_allows_continue`'s ambient `LLMCallLimitContext`). Prior
to this fix that bus was built by CAPTURING `self._dispatch_intervention`
directly — a bus wrapping the local session's OWN dispatcher, bypassing
`Session._intervention_bridge` entirely. On an ATTACHED spawned/driver session
(a pipeline driver, a delegated sub-agent) that means a budget-exceed prompt
dispatched on the driver's own listener-less `InterventionRegistry`
(`enforce_listener_presence=True`, no listener registered for a driver) and
the `enforce_listener_presence` short-circuit resolved it to an EMPTY
`InterventionAnswer` immediately — auto-refusing without ever reaching the
pipeline ORIGINATOR's live operator. Different symptom from #3049 (auto-refuse,
not an orphaned hang — `handle_limit_exceeded` turns a `choice_id`-less answer
into `allow_continue=False`), same delivery-rule violation
(`docs/concepts/runtime/intervention-delivery.md`): "an intervention... is
answered by the originator the run is ultimately attached to."

Fix: `_ChatBudgetBus.request` now resolves its bus fresh on each call via
`Session._make_router_intervention_bus` — the SAME bridge-aware seam #3052
gave the 5 MCP router-op methods — instead of freezing a self-bound
`_dispatch_intervention` reference at `__init__` time.

Drive-measured live (see PR description) with real `AgentRegistry` / `Session`
/ `BridgeToParent` objects before writing this fix: pre-fix, the prompt in the
attached case resolved to `LimitDecision(reason="user_refused")` in the same
event-loop tick, WITHOUT the originator's active queue ever seeing it.
Post-fix it lands on the originator's queue and only resolves once answered.

Real `AgentRegistry` / `Session` / `BridgeToParent` / `BudgetCheck` /
`handle_limit_exceeded` — no collaborator mocks. Mirrors
`test_3049_driver_router_op_intervention_reaches_originator.py`'s
cross-session-witness + fail-close + structural-guard pattern.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.intervention_choices import YES
from reyn.llm.llm import _budget_exceed_allows_continue
from reyn.runtime.budget.budget import BudgetCheck
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.runtime.session_api import _build_agent_step_narrowing, spawn_ephemeral_session
from reyn.runtime.spawn_routing import AuditOnlyNoSurface, BridgeToParent


def _registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + a Session factory that forwards BOTH spawn overrides — the
    driver spawn threads a parent-bound ``intervention_bridge`` exactly as production does.
    ``non_interactive=False`` (unlike the #3049 harness) because ``handle_limit_exceeded``'s
    ``interactive`` mode only reaches the bus at all when the caller CAN ask (a non-interactive
    caller takes the bounded-auto-extend branch before ever touching the bus — #1649)."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log, registry=holder.get("reg"),
            non_interactive=False, presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


async def _spawn_driver(reg: "AgentRegistry", routing) -> Session:
    """A pipeline driver session spawned with ``routing``'s intervention bridge — the SAME
    spawn shape ``_spawn_pipeline_driver_session`` uses (BridgeToParent attached / AuditOnly
    detached). Constructing the driver publishes its OWN ``_ChatBudgetBus`` as the ambient
    ``LLMCallLimitContext`` (``Session.__init__`` -> ``set_llm_call_limit_context``) — the
    same contextvar ``_budget_exceed_allows_continue`` reads, so calling it from this same
    async context after spawning the driver drives the EXACT bus a budget-exceed check on the
    driver would use."""
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


# ── Attached: a driver-session budget-exceed prompt reaches the originator operator ────────


@pytest.mark.asyncio
async def test_attached_driver_budget_exceed_prompt_reaches_originator_operator(
    tmp_path: Path,
) -> None:
    """Tier 2: an ATTACHED pipeline driver's budget-exceed gate — driven for real through
    ``_budget_exceed_allows_continue`` (the exact function ``call_llm_tools`` invokes when
    ``check_pre_llm`` refuses) — delivers its ``safety.limit.*`` prompt to the ORIGINATOR
    operator's live listener, and the operator's approval flows back as ``allow_continue=True``.
    RED before the fix: ``_ChatBudgetBus`` captured ``self._dispatch_intervention`` bound to the
    DRIVER itself, so the prompt resolved to an empty answer (``allow_continue=False``,
    ``reason="user_refused"``) in the same tick, WITHOUT ever reaching the originator's queue."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    originator = reg.get_or_load("worker")
    originator.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    await _spawn_driver(reg, BridgeToParent(originator))

    # Real BudgetCheck as call_llm_tools' check_pre_llm would return on a hard-cap refusal.
    check = BudgetCheck(allowed=False, hard_dimension="test_budget")
    gate = asyncio.ensure_future(_budget_exceed_allows_continue(check, "worker"))

    loop = asyncio.get_event_loop()
    deadline = loop.time() + 5.0
    while loop.time() < deadline and not originator.interventions.list_active():
        await asyncio.sleep(0.02)
    assert originator.interventions.list_active(), (
        "the driver's budget-exceed prompt never reached the ORIGINATOR operator's active "
        "queue — it auto-refused on the driver's own listener-less registry (the #3053 gap)."
    )
    assert originator.list_stalled_interventions() == [], (
        "the bridged budget-exceed prompt was parked in the originator's stalled queue "
        "instead of its live listener."
    )

    consumed = await originator.answer_oldest_intervention_choice(YES)
    assert consumed is True
    allow_continue = await asyncio.wait_for(gate, timeout=5.0)
    assert allow_continue is True, (
        "the operator's YES approval on the originator surface did not flow back as "
        "allow_continue=True."
    )

    _cancel_tasks(reg)


# ── Detached: a headless driver's budget-exceed gate fails closed (never reaches, never hangs) ─


@pytest.mark.asyncio
async def test_detached_driver_budget_exceed_fail_closed_refuse(tmp_path: Path) -> None:
    """Tier 2: (fail-close half of the rule "no attached originator -> close and answer") a
    DETACHED driver (spawned ``AuditOnlyNoSurface``, as ``start_pipeline_run`` does) resolves
    its budget-exceed gate to a deliberate refusal (``allow_continue=False``) without ever
    reaching (or hanging on) a would-be operator's live listener. The fix preserves the
    detached fail-close by construction (the bridge IS ``AuditOnlyInterventionBridge``), not
    by a per-gate special case."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _registry(tmp_path, state_log)
    # A would-be operator exists but this driver is NOT attached to it — it must never be consulted.
    originator = reg.get_or_load("worker")
    originator.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)

    await _spawn_driver(reg, AuditOnlyNoSurface())

    check = BudgetCheck(allowed=False, hard_dimension="test_budget")
    allow_continue = await asyncio.wait_for(
        _budget_exceed_allows_continue(check, "worker"), timeout=3.0
    )
    assert allow_continue is False, (
        "a detached driver's budget-exceed gate must fail-close (deny), never silently allow."
    )
    assert originator.interventions.list_active() == [], (
        "a detached driver's budget-exceed prompt reached a non-attached operator — detached "
        "must fail-close locally, not bridge to an unrelated surface."
    )

    _cancel_tasks(reg)


# ── Structural: the budget bus resolves through the single bridge-aware seam, not a frozen ──
# ── self-bound `_dispatch_intervention` capture ──────────────────────────────────────────────


def _frozen_dispatch_capture_bus_classes() -> "list[str]":
    """Enumerate nested classes defined inside a ``Session`` method that invoke a LOCAL name
    assigned directly from ``self._dispatch_intervention`` (e.g. ``_x = self._dispatch_intervention``
    then ``_x(iv)`` inside the nested class) — the #3053 anti-pattern: a bespoke intervention-bus
    class built by freezing a self-bound dispatcher at construction time, bypassing
    ``_make_router_intervention_bus`` (bridge-aware). Derived from the live class source (never
    hand-listed), so a FUTURE limit/budget-style bus reintroducing the same self-bound freeze is
    caught automatically, independent of variable naming."""
    src = textwrap.dedent(inspect.getsource(Session))
    tree = ast.parse(src)
    offenders: list[str] = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        frozen_names: set[str] = set()
        for stmt in ast.walk(func):
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and isinstance(stmt.value, ast.Attribute)
                and stmt.value.attr == "_dispatch_intervention"
                and isinstance(stmt.value.value, ast.Name)
                and stmt.value.value.id == "self"
            ):
                frozen_names.add(stmt.targets[0].id)
        if not frozen_names:
            continue
        for node in ast.walk(func):
            if not isinstance(node, ast.ClassDef):
                continue
            for call in ast.walk(node):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in frozen_names
                ):
                    offenders.append(f"{func.name}.{node.name}")
                    break
    return offenders


def test_budget_bus_has_no_frozen_self_bound_dispatch_capture() -> None:
    """Tier 2: #3053's fix-class guard — no nested bus class inside a ``Session`` method may
    invoke a name frozen directly from ``self._dispatch_intervention`` at construction time.
    That was the exact ``_ChatBudgetBus`` anti-pattern: it bypassed ``_intervention_bridge``
    entirely, so an attached driver session's budget-exceed prompt auto-refused on the driver's
    OWN listener-less registry instead of reaching the pipeline originator. RED before the fix:
    ``__init__`` captured ``_session_dispatch = self._dispatch_intervention`` and
    ``_ChatBudgetBus.request`` called it directly."""
    offenders = _frozen_dispatch_capture_bus_classes()
    assert offenders == [], (
        "these Session-nested bus classes invoke a frozen self-bound `_dispatch_intervention` "
        "capture directly, bypassing the bridge-aware `_make_router_intervention_bus` seam — "
        f"an attached driver session's intervention raised there would auto-refuse or orphan "
        f"instead of reaching the pipeline originator (#3053): {sorted(set(offenders))}"
    )

    # Vacuity guard: `_ChatBudgetBus` must still resolve through `_make_router_intervention_bus`
    # on each call (not a frozen bus reference), else the scan above is vacuously green because
    # there is nothing left to freeze-and-call in the first place (guard inert).
    init_src = inspect.getsource(Session.__init__)
    assert "_ChatBudgetBus" in init_src and "_make_router_intervention_bus" in init_src, (
        "Session.__init__ no longer wires `_ChatBudgetBus` through `_make_router_intervention_bus` "
        "— the frozen-capture structural scan would be vacuously green (guard inert)."
    )
