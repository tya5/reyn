"""Tier 2: OS invariant — an ``agent`` step's leaf worker is born inside its
INVOKER's per-session capability envelope, not merely inside the step's own
``capabilities`` declaration.

#3553, the sibling of #3546 one level down. ``run_agent_step`` spawned the worker
with ``_build_agent_step_narrowing(capabilities)``, which composed only the
structural delegation deny plus the caller's ``capabilities`` list — the invoker's
own sid-keyed #2103-S1a narrowing was never a term. The worker gets a FRESH sid, so
that layer resolves to nothing on it: the same axis #3546 restored at the pipeline
driver spawn was dropped again at the driver's own children.

The composition is derived, not chosen (``capability_profile.
compose_narrowing_mappings``): **deny keys union, allow keys intersect, an ABSENT
allow key is ⊤**. The two acceptance legs below are the two rules, one each, and
they are separable on purpose — with only one of them measured you cannot tell
which rule did the work, and half a composition still widens the child:

  * ``test_worker_without_capabilities_inherits_invoker_allow_list`` — the invoker
    narrows with ``tool_allow`` ONLY and the step declares NO ``capabilities``.
    Before the fix the worker's narrowing had no ``tool_allow`` key at all, i.e. no
    allow restriction: the invoker's allow-list was lost WHOLE. This is the leg that
    fails if an absent allow key is read as anything other than "the other side
    verbatim".
  * ``test_worker_with_capabilities_inherits_invoker_deny_list`` — the invoker
    narrows with ``tool_deny`` ONLY and the step explicitly declares
    ``capabilities=["write_file"]``, i.e. asks for exactly what its invoker was
    refused. This is the leg that fails if the denies do not union.

Both witness a REAL side effect — the file ``write_file`` puts on disk — rather than
a status string: a denial that only changed a return value would not prove the
capability was prevented. ``write_file`` is the production tool, reached through a
real ``RouterLoop`` turn on the real worker session; the only faked collaborator is
the LLM completion itself, injected through ``RouterLoopDriver``'s designed
``_loop_observer`` seam. An earlier draft of these tests registered a bespoke
side-effecting tool instead and passed against the UNFIXED code, because a tool that
is not in the universal catalog is never advertised and so never dispatches — the
tests were measuring nothing. ``test_the_witness_tool_actually_runs_when_permitted``
is the instrument check that keeps that failure detectable.

The completeness half of the gate lives in
``tests/runtime/test_3546_pipeline_driver_narrowing_inheritance.py``, extended by #3553 to
record, per spawn site, WHICH parent layers it composes and which test measures
that. That half records INTENT: a site whose declaration and implementation disagree
stays green there. These two legs are what actually measures this site, and neither
half substitutes for the other.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.model_resolver import ModelResolver
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import _build_agent_step_narrowing, run_agent_step
from reyn.runtime.session_params import PresentationWiring
from reyn.runtime.spawn_routing import AuditOnlyNoSurface
from reyn.security.permissions.capability_profile import compose_narrowing_mappings
from tests._support.agent_session import make_session
from tests._support.permissions import make_resolver

#: The capability under test: a real, catalog-listed, side-effecting tool whose
#: effect is observable on disk without reading any return value.
_WITNESS_TOOL = "write_file"
#: A second real tool, used as the invoker's ``tool_allow`` content in leg 1 so the
#: allow-list is non-empty and demonstrably does NOT contain ``_WITNESS_TOOL``.
_OTHER_TOOL = "read_file"
_OUT_NAME = "p3553_out.txt"


class _WritesOnceLLM:
    """A real ``_llm_caller``-shaped callable (the RouterLoop Tier-2 test seam): the
    first turn asks for ``write_file``, every later turn answers in plain text so the
    loop terminates whether or not the call was allowed.

    Not a ``MagicMock``: a drift in the ``call_llm_tools`` keyword contract raises
    ``TypeError`` here exactly as it would in production. The LLM's content is
    incidental — what is under test is whether the OS lets the requested capability
    reach its handler.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        if self.calls == 1:
            return LLMToolCallResult(
                content=None,
                tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {
                        "name": _WITNESS_TOOL,
                        "arguments": '{"path": "%s", "content": "step-ran"}' % _OUT_NAME,
                    },
                }],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=1, completion_tokens=1),
            )
        return LLMToolCallResult(
            content="done", tool_calls=[], finish_reason="stop", usage=TokenUsage(),
        )


def _registry(tmp_path: Path, scripted: "_WritesOnceLLM") -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory + real ``RouterLoop`` +
    real ``PermissionResolver`` (with ``file.write`` approved, so a denial observed
    below is the capability narrowing and not the write gate)."""
    if not (tmp_path / "reyn.yaml").exists():
        (tmp_path / "reyn.yaml").write_text("llm:\n  model: standard\n", encoding="utf-8")
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}
    resolver = ModelResolver({"standard": "gemini/gemini-2.5-flash-lite"})
    perms = make_resolver(tmp_path, config={"file.write": "allow"})

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            permission_resolver=perms, workspace_base_dir=tmp_path,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
            resolver=resolver,
        )
        s._loop_driver._loop_observer = (
            lambda loop: setattr(loop, "_llm_caller", scripted)
        )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    if not reg.exists("worker"):
        reg.create("worker")
    return reg


async def _narrowed_invoker(reg: AgentRegistry, narrowing: "dict | None") -> Session:
    """Spawn a REAL session under ``narrowing`` through the production spawn seam
    (``spawn_session_recorded`` — the same call the ``session_spawn`` tool makes), so
    the invoker's sid-keyed ``config.yaml`` is written by production code."""
    routing = AuditOnlyNoSurface()
    sid = await reg.spawn_session_recorded(
        "worker", mode="persistent", narrowing=narrowing,
        presentation_consumer=routing.presentation_consumer,
        intervention_bridge=routing.intervention_bridge,
    )
    session = reg.get_session("worker", sid)
    assert session is not None
    return session


def _written(tmp_path: Path) -> "list[str]":
    """Every on-disk copy of the witness file, wherever the worker's workspace
    resolved it — the side effect itself, not a status string."""
    return sorted(str(p) for p in tmp_path.rglob(_OUT_NAME))


# ── instrument check ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_witness_tool_actually_runs_when_permitted(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: instrument check — with the invoker narrowed on an UNRELATED tool, the
    agent-step worker's ``write_file`` call really does reach its handler and put a
    file on disk.

    Without this, both acceptance legs below would pass for the wrong reason if the
    tool call stopped being dispatchable at all (which is exactly what happened to an
    earlier draft using a non-catalog tool). The legs assert an absence; this asserts
    the presence they are the absence of.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)

    invoker = await _narrowed_invoker(reg, {"tool_deny": ["some_unrelated_tool"]})
    await run_agent_step(
        reg, identity="worker", prompt="write the file", invoker_session=invoker,
    )

    assert _written(tmp_path), (
        "the agent-step worker's write_file never executed even with nothing "
        "narrowing it — the two acceptance legs below would be vacuous"
    )


# ── leg 1: the ⊤ rule (invoker allow-list, step declares no capabilities) ──────


@pytest.mark.asyncio
async def test_worker_without_capabilities_inherits_invoker_allow_list(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: an ``agent`` step that declares NO ``capabilities`` still cannot run a
    tool its invoker's ``tool_allow`` excludes.

    This is the widening the composition's ⊤ rule closes: the step imposes no allow
    restriction of its own, so the worker's allow-list must be the invoker's
    (``parent ∩ ⊤ = parent``) rather than absent. Before the fix it was absent, and
    the tool's real side effect happened.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)

    invoker = await _narrowed_invoker(reg, {"tool_allow": [_OTHER_TOOL]})
    result = await run_agent_step(
        reg, identity="worker", prompt="write the file", invoker_session=invoker,
    )

    assert not _written(tmp_path), (
        "a tool OUTSIDE the invoker's tool_allow executed its real side effect "
        f"inside an agent step that declared no capabilities (reply: {result!r})"
    )


# ── leg 2: the union rule (invoker deny-list, step asks for the denied tool) ───


@pytest.mark.asyncio
async def test_worker_with_capabilities_inherits_invoker_deny_list(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: an ``agent`` step cannot re-grant a tool its invoker denied by naming
    it in ``capabilities``.

    The step's ``capabilities`` is an allow-list, so on its own it would make the tool
    the worker's ONLY reachable capability. The invoker's ``tool_deny`` has to union
    in for the answer to stay a denial. Before the fix it did not, and the tool's real
    side effect happened.
    """
    monkeypatch.chdir(tmp_path)
    scripted = _WritesOnceLLM()
    reg = _registry(tmp_path, scripted)

    invoker = await _narrowed_invoker(reg, {"tool_deny": [_WITNESS_TOOL]})
    result = await run_agent_step(
        reg, identity="worker", prompt="write the file",
        capabilities=[_WITNESS_TOOL], invoker_session=invoker,
    )

    assert not _written(tmp_path), (
        "a tool the invoker's per-session narrowing DENIED executed its real side "
        f"effect inside an agent step that named it in capabilities (reply: {result!r})"
    )


# ── the composition rules, as a unit (the two legs' shared derivation) ────────


def test_compose_absent_allow_is_top_not_empty() -> None:
    """Tier 2: an ABSENT allow key means ⊤ (no restriction on that axis), so the other
    side's allow-list survives composition verbatim — in BOTH directions.

    Reading absence as the empty set instead would make a child that declares no
    capabilities unable to reach anything (parent-absent case), or silently drop the
    parent's allow-list (child-absent case — the #3553 defect itself).
    """
    child_only = compose_narrowing_mappings(
        {"tool_deny": ["d"]}, {"tool_allow": ["a"], "tool_deny": ["e"]},
    )
    assert set(child_only["tool_allow"]) == {"a"}

    parent_only = compose_narrowing_mappings(
        {"tool_allow": ["a", "b"]}, {"tool_deny": ["e"]},
    )
    assert set(parent_only["tool_allow"]) == {"a", "b"}


def test_compose_denies_union_and_allows_intersect() -> None:
    """Tier 2: the two restrict-only rules, on the axis where both sides constrain —
    ``*_deny`` unions (deny-always-wins) and ``*_allow`` intersects (a value stays
    reachable only if EVERY term allows it)."""
    composed = compose_narrowing_mappings(
        {"tool_allow": ["a", "b"], "tool_deny": ["x"]},
        {"tool_allow": ["b", "c"], "tool_deny": ["y"]},
    )
    assert set(composed["tool_allow"]) == {"b"}
    assert set(composed["tool_deny"]) == {"x", "y"}


def test_compose_carries_axes_the_child_does_not_constrain() -> None:
    """Tier 2: the MCP and category axes are composed by the same two rules, so a
    parent narrowing that uses them is not silently dropped just because an ``agent``
    step only ever speaks the TOOL axis."""
    composed = compose_narrowing_mappings(
        {"mcp_allow": ["github"], "mcp_deny": ["evil"], "categories": ["io"]},
        {"tool_deny": ["delegate_to_agent"]},
    )
    assert set(composed["mcp_allow"]) == {"github"}
    assert set(composed["mcp_deny"]) == {"evil"}
    assert set(composed["categories"]) == {"io"}
    assert set(composed["tool_deny"]) == {"delegate_to_agent"}


def test_compose_key_tables_cover_every_profile_axis() -> None:
    """Tier 2: the two composition tables cover every axis
    ``load_capability_profile`` actually reads.

    ``compose_narrowing_mappings`` may only claim that an unlisted key cannot change a
    session's envelope while that is true. A new axis added to ``CapabilityProfile``
    without being added to a table would fall through to the "inert" branch and start
    being silently dropped from inherited narrowings.
    """
    from dataclasses import fields

    from reyn.security.permissions.capability_profile import (
        _NARROWING_ALLOW_KEYS,
        _NARROWING_DENY_KEYS,
        CapabilityProfile,
    )

    axes = {f.name for f in fields(CapabilityProfile)} - {"name", "description"}
    tabled = set(_NARROWING_ALLOW_KEYS) | set(_NARROWING_DENY_KEYS)
    assert axes == tabled, (
        "a CapabilityProfile axis is not classified as allow-shaped or deny-shaped in "
        "capability_profile's composition tables, so an inherited narrowing would "
        f"drop it: {axes ^ tabled!r}"
    )


def test_agent_step_narrowing_without_an_invoker_is_unchanged() -> None:
    """Tier 2: with nothing to inherit (a headless ``reyn pipe`` run — no
    ``invoker_session``), the narrowing an ``agent`` step builds is exactly the
    structural delegation deny plus any declared ``capabilities``, so the inert path
    is byte-identical to pre-#3553."""
    assert _build_agent_step_narrowing(None, None) == _build_agent_step_narrowing(None)
    plain = _build_agent_step_narrowing([_OTHER_TOOL], None)
    assert plain is not None
    assert set(plain["tool_allow"]) == {_OTHER_TOOL}
    # delegate_to_agent (this deny-set's former other member) retired in
    # proposal 0067 P6 (#3978), with no replacement here.
    assert "run_pipeline" in plain["tool_deny"]
