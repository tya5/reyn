"""Tier 2c: pipeline-R5 — run_agent_step, the agent-step run+collect primitive.

``run_agent_step`` (``runtime/session_api.py``) composes
``spawn_ephemeral_session`` + ``MessageBus.request`` + ``core.pipeline.schema.
validate`` — real ``AgentRegistry`` / ``Session`` / ``RouterLoop`` /
``MessageBus`` throughout. The ONLY faked collaborator is the LLM completion
call itself, injected via ``RouterLoop``'s designed ``_llm_caller`` Tier-2
test seam (``router_loop.py``), reached through ``RouterLoopDriver``'s
``_loop_observer`` seam (``router_loop_driver.py``) on the session's
ALREADY-CONSTRUCTED ``_loop_driver`` — a post-construction observer, not a
factory-seam bypass (every other Session collaborator: router host, history
buffer, budget advisor, dispatch, permission gate, is the real production
object; see ``test_execution_driver_seam.py`` for the same seam's own
invariant tests).

Per ``docs/deep-dives/contributing/testing.md``, this is Tier 2c ("Multi-
component integration ... LLM is faked via a stub real callable NOT via
LLMReplay — that path is Tier 3"), not Tier 3: Tier 3 today is scoped to
Tier 3a (single LLM call replay); a full router turn through a real Session
(system prompt + tool catalog + history) is Tier 3b, explicitly deferred
pending the CLI/ChatSession-driver redesign. The LLM's actual content here is
incidental to what's under test (the run+collect composition), which is
exactly Tier 2c's carve-out — a real recorded ``LLMReplay`` fixture would
also embed the live host clock (``get_environment_info()``'s ``date``) into
its cache key, breaking replay across days.

``_ScriptedAgentReply`` is a concrete class with a typed ``__call__`` (NOT
``unittest.mock.MagicMock``/``AsyncMock``/``patch``) — a signature drift in
the ``call_llm_tools`` contract it stands in for raises ``TypeError`` here
exactly as it would in production.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.errors import AgentStepError
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_api import (
    _build_agent_step_narrowing,
    run_agent_step,
    spawn_ephemeral_session,
)

# ── real-callable LLM stub (Tier 2c: LLM is incidental) ─────────────────────


class _ScriptedAgentReply:
    """Always answers with one fixed plain-text/JSON turn (no tool_calls) —
    the RouterLoop's enumerate-all scheme classifies empty ``tool_calls`` as
    ``PlainText`` (``schemes/enumerate_all.py``), so the OS emits ``content``
    to the outbox and stops; no LLM signature is bypassed."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def __call__(self, **kwargs: Any) -> LLMToolCallResult:
        self.calls += 1
        return LLMToolCallResult(
            content=self.content, tool_calls=[], finish_reason="stop",
            usage=TokenUsage(),
        )


def _registry(tmp_path: Path, scripted: "_ScriptedAgentReply | None") -> AgentRegistry:
    """Real AgentRegistry + real Session factory (mirrors
    ``test_pipeline_a2_spawn_ephemeral_session.py`` / ``test_2103_A_...``'s
    ``holder`` deferred-registry-ref trick so the factory can pass
    ``registry=`` for ephemeral auto-vanish). When ``scripted`` is given, every
    constructed session's real ``RouterLoopDriver`` gets the scripted LLM
    wired in via ``_loop_observer`` before the driver's first turn."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        s = Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_consumer=presentation_consumer,
            intervention_bridge=intervention_bridge,
        )
        if scripted is not None:
            s._loop_driver._loop_observer = (
                lambda loop: setattr(loop, "_llm_caller", scripted)
            )
        return s

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


# ── run+collect happy path ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_step_returns_reply_text(tmp_path: Path) -> None:
    """Tier 2c: run_agent_step spawns an ephemeral session, runs one turn,
    and returns the plain-text reply verbatim (no schema declared)."""
    scripted = _ScriptedAgentReply("hello from the leaf worker")
    reg = _registry(tmp_path, scripted)

    result = await run_agent_step(reg, identity="worker", prompt="say hi")

    assert result == "hello from the leaf worker"
    assert scripted.calls == 1


# ── structured output: success ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_step_schema_success_returns_parsed_value(tmp_path: Path) -> None:
    """Tier 2c: with schema= set, JSON text conforming to the registered
    schema is parsed + validated, and the PARSED value (not raw text) is
    returned."""
    scripted = _ScriptedAgentReply('{"verdict": "approve", "confidence": 0.9}')
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", {
        "fields": {
            "verdict": {"type": "enum", "values": ["approve", "reject"], "required": True},
            "confidence": {"type": "number", "required": True},
        },
    })

    result = await run_agent_step(
        reg, identity="worker", prompt="review this",
        schema="review", schema_registry=schema_registry,
    )

    assert result == {"verdict": "approve", "confidence": 0.9}


# ── structured output: malformed JSON ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_step_schema_malformed_json_raises(tmp_path: Path) -> None:
    """Tier 2c: schema declared but the agent's reply text is not JSON at
    all → AgentStepError (a normal step failure), not a bare JSONDecodeError
    or a silent pass-through of the raw text."""
    scripted = _ScriptedAgentReply("Sure — looks good to me!")
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", {
        "fields": {"verdict": {"type": "string", "required": True}},
    })

    with pytest.raises(AgentStepError):
        await run_agent_step(
            reg, identity="worker", prompt="review this",
            schema="review", schema_registry=schema_registry,
        )


# ── structured output: schema non-conformance ────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_step_schema_nonconforming_raises(tmp_path: Path) -> None:
    """Tier 2c: valid JSON that does NOT conform to the declared schema
    (missing the required field) → AgentStepError, not a silently-accepted
    partial value."""
    scripted = _ScriptedAgentReply('{"unexpected": 123}')
    reg = _registry(tmp_path, scripted)
    schema_registry = SchemaRegistry()
    schema_registry.register("review", {
        "fields": {"verdict": {"type": "string", "required": True}},
    })

    with pytest.raises(AgentStepError):
        await run_agent_step(
            reg, identity="worker", prompt="review this",
            schema="review", schema_registry=schema_registry,
        )


# ── delegation forbidden (structural — no LLM turn needed) ──────────────────


@pytest.mark.asyncio
async def test_agent_step_narrowing_denies_delegation_when_spawned(tmp_path: Path) -> None:
    """Tier 2: OS invariant — the SAME narrowing run_agent_step builds
    (``_build_agent_step_narrowing``), spawned via ``spawn_ephemeral_session``,
    is LIVE-enforced (``registry.resolved_profile_for``) to deny
    ``delegate_to_agent`` (+ its qualified ``multi_agent__delegate`` alias)
    AND ``run_pipeline`` (+ its qualified ``pipeline__run`` alias, IS-1 R6 S3:
    an agent step is a spawn-tree LEAF — no launching a nested pipeline)
    even when the caller's own ``capabilities`` list explicitly names them —
    ``capability_profile`` resolution is deny-always-wins
    (``profile_permits``), so an ``agent`` step cannot re-open either via its
    ``capabilities`` argument. Purely structural: no LLM turn / actual
    delegation or pipeline-launch attempt needed to prove the gate."""
    reg = _registry(tmp_path, scripted=None)
    narrowing = _build_agent_step_narrowing(
        ["delegate_to_agent", "run_pipeline", "file__read"]
    )

    sid = await spawn_ephemeral_session(reg, identity="worker", narrowing=narrowing, presentation_consumer=None, intervention_bridge=None)
    contextual, _excluded = reg.resolved_profile_for("worker", sid=sid)

    assert contextual is not None
    assert {"delegate_to_agent", "multi_agent__delegate"} <= contextual.tool_deny
    assert {"run_pipeline", "pipeline__run"} <= contextual.tool_deny
    # IS-2: the async launch is the same S3 escape hatch — denied alongside.
    assert {"run_pipeline_async", "pipeline__run_async"} <= contextual.tool_deny


def test_build_agent_step_narrowing_no_capabilities_is_restrict_only(tmp_path: Path) -> None:
    """Tier 2: OS invariant — omitting ``capabilities`` (None) leaves
    ``tool_allow`` unset (no re-grant beyond the agent's normal envelope);
    only the structural leaf-worker deny (delegation + the nested pipeline
    launches — registered AND inline, sync AND async — R6 S3) is imposed.
    Pure function, no session needed."""
    narrowing = _build_agent_step_narrowing(None)
    assert "tool_allow" not in narrowing
    assert set(narrowing["tool_deny"]) == {
        "delegate_to_agent", "run_pipeline", "run_pipeline_async",
        "run_pipeline_inline", "run_pipeline_inline_async",
    }


# ── ephemeral cleanup ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_agent_step_ephemeral_session_vanishes(tmp_path: Path) -> None:
    """Tier 2c: after run_agent_step completes, the ephemeral session it
    spawned is torn down (public ``registry.session_ids`` surface) — R5's
    "close" step (self-vanish via ``_maybe_schedule_ephemeral_vanish``, fired
    automatically at the end of the turn's ``run_one_iteration``) needs no
    explicit call from ``run_agent_step`` itself.

    Mirrors ``test_2103_A_ephemeral_auto_vanish_1953.py``'s precedent of
    awaiting the scheduled (but not yet run) detached teardown task before
    asserting — without this, a synchronous check right after
    ``run_agent_step`` returns could race the vanish and see the session
    still present, hiding a regression either way."""
    scripted = _ScriptedAgentReply("done")
    reg = _registry(tmp_path, scripted)

    result = await run_agent_step(reg, identity="worker", prompt="say bye")
    assert result == "done"

    # Tuple-unpack (not a size check): raises ValueError itself if
    # run_agent_step spawned zero or more than one non-main session —
    # behavioral, not a format/size pin.
    (spawned_sid,) = [sid for sid in reg.session_ids("worker") if sid != "main"]
    spawned = reg._peek_session("worker", spawned_sid)  # noqa: SLF001 — test-bridge, precedent above
    if spawned is not None:
        vanish_task = spawned._vanish_task  # noqa: SLF001 — test-bridge, precedent above
        if vanish_task is not None:
            await vanish_task

    assert spawned_sid not in reg.session_ids("worker")
