"""Tier 2 / Tier 2c: IS-5 — run_pipeline surfacing + production registry wiring.

Prior to IS-5, ``PipelineRegistry`` (``core/pipeline/registry.py``) was never
wired into production: ``RouterCallerState.pipeline_registry`` (consumed by
the ``run_pipeline`` tool, ``tools/pipeline_verbs.py``) defaulted to ``None``
and nothing ever populated it on the live router path, so ``run_pipeline``
always errored "no PipelineRegistry" when an agent tried to call it. Likewise
``RouterCallerState.agent_registry`` was never populated by
``RouterLoop._build_router_caller_state`` (needed for a pipeline's
``AgentStep``). And the ``pipeline`` universal-catalog category had no
``_enumerate_category`` branch, so ``list_actions(category=["pipeline"])``
never surfaced a registered pipeline to the LLM.

This file covers:
  1. ``RouterCallerState.pipeline_registry`` / ``.agent_registry`` are
     populated from ``host.get_pipeline_registry()`` / ``get_agent_registry()``
     by ``RouterLoop._build_router_caller_state`` (mirrors the pre-existing
     ``test_router_caller_state_mcp_servers.py`` pattern for ``mcp_servers``).
  2. A real ``Session`` constructs + owns a real (non-None) ``PipelineRegistry``,
     threaded through ``RouterHostAdapter`` — the "landmine gone" invariant.
  3. ``list_actions(category=["pipeline"])`` surfaces a registered pipeline's
     name + description.
  4. The FULL live router loop: a scripted LLM emits
     ``invoke_action(action_name="pipeline__run", args={name, input})``;
     the OS drives it through the real Session → real PipelineRegistry →
     real PipelineExecutor, and the pipeline's actual output round-trips
     back into chat history.

Policy compliance (docs/deep-dives/contributing/testing.md): no
unittest.mock.MagicMock/AsyncMock/patch — the LLM is faked via a real async
callable stub (Tier 2c), monkeypatched onto ``reyn.runtime.router_loop.
call_llm_tools`` (the designed Tier-2 test seam), same precedent as
``tests/test_session_invariants.py``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.executor import Pipeline, TransformStep
from reyn.core.pipeline.registry import PipelineRegistry
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session


def _registry_backed_session(tmp_path: Path):
    """A production-shaped, registry-backed session for the full-loop tests.

    IS-6 reworked sync ``run_pipeline`` to spawn an ATTACHED driver-session, so
    it now needs the same substrate as the async verb: an ``AgentRegistry`` to
    spawn the driver-session under, and a ``.reyn``-anchored WAL for its
    work-order/recovery files. A live production router turn always has both
    (its session is registry-spawned); these tests wire them explicitly. Returns
    ``(registry, session)`` with the session pinned to the universal-category
    scheme (matching the scripted ``invoke_action`` tool-call shape)."""
    state_log = StateLog(tmp_path / ".reyn" / "state.wal")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None) -> Session:
        # #2708 P3.1: accept + forward the attached driver spawn's present-sink override.
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            chat_tool_use_scheme="universal-category",
            presentation_consumer=presentation_consumer,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("test_agent")
    session = reg.get_or_load("test_agent")
    session.is_attached = True
    return reg, session

# ---------------------------------------------------------------------------
# 1. RouterCallerState wiring (RouterLoop + minimal fake host — mirrors
#    tests/test_router_caller_state_mcp_servers.py's precedent).
# ---------------------------------------------------------------------------


class _FakeHost:
    """Minimal RouterLoopHost stub — real shape, no mock framework."""

    agent_name: str = "test-agent"
    agent_role: str = ""
    output_language: str = "en"

    def __init__(
        self,
        *,
        pipeline_registry: Any = None,
        agent_registry: Any = None,
        expose_pipeline_registry: bool = True,
        expose_agent_registry: bool = True,
    ) -> None:
        self._pipeline_registry = pipeline_registry
        self._agent_registry = agent_registry

        class _E:
            def emit(self, *a, **kw): pass
            subscribers: list = []
        self._events = _E()

        if expose_pipeline_registry:
            self.get_pipeline_registry = lambda: self._pipeline_registry  # type: ignore[method-assign]
        if expose_agent_registry:
            self.get_agent_registry = lambda: self._agent_registry  # type: ignore[method-assign]

    @property
    def events(self): return self._events

    def get_universal_wrappers_enabled(self) -> bool: return True
    def get_action_usage_tracker(self): return None
    def get_action_embedding_index(self): return None
    def get_embedding_provider(self): return None
    def get_embedding_model_class(self): return None
    def get_action_retrieval_config(self): return None
    def list_available_skills(self) -> list[dict]: return []
    def list_available_agents(self) -> list[dict]: return []
    def get_memory_index(self) -> dict: return {"status": "not_found", "content": ""}
    def get_file_permissions(self): return None
    def get_mcp_servers(self) -> list[dict]: return []
    def get_web_fetch_allowed(self) -> bool: return False
    def get_project_context(self) -> str: return ""
    def get_sandbox_backend(self): return None
    def resolve_model(self, name: str) -> str: return "fake-model"


def _build_router_loop(host: _FakeHost) -> Any:
    from reyn.runtime.router_loop import RouterLoop
    return RouterLoop(host=host, chain_id="c1", router_model="standard")


def test_pipeline_registry_threaded_into_router_caller_state() -> None:
    """Tier 2: host.get_pipeline_registry() lands on rs.pipeline_registry.

    The pre-fix landmine: RouterCallerState(...) construction never set
    ``pipeline_registry=``, so the dataclass default (None) always won and
    run_pipeline always errored "no PipelineRegistry available"."""
    registry = PipelineRegistry()
    loop = _build_router_loop(_FakeHost(pipeline_registry=registry))

    rs = asyncio.run(loop._build_router_caller_state())

    assert rs.pipeline_registry is registry


def test_agent_registry_threaded_into_router_caller_state() -> None:
    """Tier 2: host.get_agent_registry() lands on rs.agent_registry (needed
    for a pipeline's AgentStep — a pipeline with no agent step degrades fine
    with registry=None, but AgentStep pipelines need the real registry)."""
    sentinel_registry = object()
    loop = _build_router_loop(_FakeHost(agent_registry=sentinel_registry))

    rs = asyncio.run(loop._build_router_caller_state())

    assert rs.agent_registry is sentinel_registry


def test_pipeline_and_agent_registry_missing_method_falls_back_to_none() -> None:
    """Tier 2: a host without get_pipeline_registry()/get_agent_registry()
    (narrow test hosts / FakeRouterHost variants) degrades to None rather
    than raising AttributeError — the getattr-fallback posture every other
    RouterCallerState field uses (mirrors mcp_servers' precedent)."""
    loop = _build_router_loop(
        _FakeHost(expose_pipeline_registry=False, expose_agent_registry=False)
    )

    rs = asyncio.run(loop._build_router_caller_state())

    assert rs.pipeline_registry is None
    assert rs.agent_registry is None


# ---------------------------------------------------------------------------
# 2. list_actions surfaces a registered pipeline (name + description).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_actions_pipeline_category_surfaces_registered_pipeline() -> None:
    """Tier 2: list_actions(category=["pipeline"]) returns the registered
    pipeline's qualified name + its OWN description (not a generic
    run_pipeline blurb) — proves the enumerator + registry wiring end-to-end
    at the universal-catalog layer."""
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    registry = PipelineRegistry()
    registry.register(
        "digest_report",
        Pipeline(
            steps=[TransformStep(value="1 + 1", output="two")],
            description="Summarize the week's incoming reports.",
        ),
    )
    loop = _build_router_loop(_FakeHost(pipeline_registry=registry))
    rs = await loop._build_router_caller_state()
    ctx = ToolContext(
        events=loop.host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )

    result = await LIST_ACTIONS.handler({"category": ["pipeline"]}, ctx)

    items = {it["qualified_name"]: it for it in result["items"]}
    assert "pipeline__digest_report" in items
    assert items["pipeline__digest_report"]["short_description"] == (
        "Summarize the week's incoming reports."
    )
    # #2589: the static launch verbs (incl. the previously-unreachable
    # async/inline ones) are ALSO surfaced alongside the dynamic per-name
    # entry — the hybrid enumeration mirroring the ``mcp`` category.
    assert {
        "pipeline__run", "pipeline__run_async",
        "pipeline__run_inline", "pipeline__run_inline_async",
    } <= items.keys()


@pytest.mark.asyncio
async def test_list_actions_pipeline_category_empty_registry_returns_static_verbs_only() -> None:
    """Tier 2: no registered pipelines -> list_actions(category=["pipeline"])
    returns ONLY the static launch verbs (#2589 hybrid enumeration), not an
    empty list — the ``mcp`` category's hybrid pattern always surfaces its
    static verbs regardless of dynamic population; ``pipeline`` now matches."""
    from reyn.tools.types import ToolContext
    from reyn.tools.universal_catalog import LIST_ACTIONS

    loop = _build_router_loop(_FakeHost(pipeline_registry=PipelineRegistry()))
    rs = await loop._build_router_caller_state()
    ctx = ToolContext(
        events=loop.host.events,
        permission_resolver=None,
        workspace=None,
        caller_kind="router",
        router_state=rs,
    )

    result = await LIST_ACTIONS.handler({"category": ["pipeline"]}, ctx)

    names = {it["qualified_name"] for it in result["items"]}
    assert names == {
        "pipeline__run", "pipeline__run_async",
        "pipeline__run_inline", "pipeline__run_inline_async",
    }


# ---------------------------------------------------------------------------
# 3. Session owns a real, non-None PipelineRegistry, threaded to the adapter.
# ---------------------------------------------------------------------------


def test_session_pipeline_registry_is_real_instance_not_none(tmp_path: Path) -> None:
    """Tier 2: Session.pipeline_registry is a real PipelineRegistry (the
    "landmine gone" invariant) — threaded through RouterHostAdapter so
    ``adapter.get_pipeline_registry() is session.pipeline_registry``, the
    exact accessor RouterLoop._build_router_caller_state reads in
    production."""
    session = Session(agent_name="test_agent", state_log=StateLog(tmp_path / "wal.jsonl"))

    assert isinstance(session.pipeline_registry, PipelineRegistry)
    assert session.router_host.get_pipeline_registry() is session.pipeline_registry


def test_session_agent_registry_threaded_to_adapter_accessor(tmp_path: Path) -> None:
    """Tier 2: Session.agent_registry (possibly None outside a registry) is
    exposed on the adapter via the SAME public accessor pattern IS-5 adds for
    pipelines, so RouterLoop can read it without reaching into a private
    attribute."""
    session = Session(agent_name="test_agent", state_log=StateLog(tmp_path / "wal.jsonl"))

    assert session.router_host.get_agent_registry() is session.agent_registry


# ---------------------------------------------------------------------------
# 4. Full live router loop: LLM calls invoke_action("pipeline__run", ...).
# ---------------------------------------------------------------------------


_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _pipeline_invoke_result(name: str, seed_input: dict) -> LLMToolCallResult:
    """LLMToolCallResult that makes RouterLoop call run_pipeline via the
    invoke_action universal-catalog wrapper (the modern surfacing path)."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[
            {
                "id": "tc_pipeline_001",
                "type": "function",
                "function": {
                    "name": "invoke_action",
                    "arguments": json.dumps({
                        "action_name": "pipeline__run",
                        "args": {"name": name, "input": seed_input},
                    }),
                },
            }
        ],
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


def _text_result(text: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=text, tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE,
    )


def _make_llm_stub(results: list[LLMToolCallResult]):
    """Real async callable stub (Tier 2c) — NOT unittest.mock.AsyncMock — a
    signature drift in call_llm_tools would raise TypeError here exactly as
    it would in production."""
    call_count = [0]

    async def _stub(**kwargs) -> LLMToolCallResult:
        idx = call_count[0]
        call_count[0] += 1
        return results[idx] if idx < len(results) else results[-1]

    return _stub


def _drain_outbox(session: Session) -> list:
    msgs = []
    while not session.outbox.empty():
        msgs.append(session.outbox.get_nowait())
    return msgs


@pytest.mark.asyncio
async def test_run_pipeline_via_invoke_action_full_live_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2c: an agent actually launches a registered pipeline through the
    REAL router loop — the end-to-end proof IS-5 sets out to establish.

    Registers a real Pipeline into a real Session's production
    PipelineRegistry, scripts the LLM to emit
    ``invoke_action(action_name="pipeline__run", args={name, input})``, and
    drives one full user turn. Asserts the pipeline's REAL transform output
    (not a stub) round-trips into the tool-result chat history entry, and
    the router's second-round text reply reaches the outbox — proving the
    registry wiring + catalog surfacing + dispatch seam all connect, not
    just the handler in isolation (which test_run_pipeline_tool_is1.py
    already covers)."""
    monkeypatch.chdir(tmp_path)
    # #1657 precedent: pin the scheme (in the helper) to match the scripted
    # invoke_action tool-call shape. IS-6: registry-backed so the reworked sync
    # run_pipeline can spawn its attached driver-session.
    _reg, session = _registry_backed_session(tmp_path)

    # Register a real pipeline into the session's OWN production registry —
    # not a test-local instance — proving Session.pipeline_registry is what
    # the live loop actually consults.
    session.pipeline_registry.register(
        "greet",
        Pipeline(
            steps=[TransformStep(value="'hello ' + ctx.name", output="greeting")],
            description="Greet the named recipient.",
        ),
    )

    stub = _make_llm_stub([
        _pipeline_invoke_result("greet", {"name": "world"}),
        _text_result("the pipeline ran successfully"),
    ])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    await session._handle_user_message(
        "please run the greet pipeline", chain_id="chain-is5-001",
    )

    # The tool-result history entry carries the REAL pipeline output.
    tool_messages = [m for m in session.history if m.role == "tool"]
    assert tool_messages, "expected at least one tool-result history entry"
    # #2425 案B: the sync run_pipeline result renders as its str ``output`` (plain text — run_id /
    # named_stores are dropped from the LLM-visible side), whatever envelope nesting produced it.
    assert tool_messages[-1].content == "hello world"

    # The router's second round reached the outbox (the loop didn't hang or
    # error out after the tool call).
    msgs = _drain_outbox(session)
    agent_msgs = [m for m in msgs if m.kind == "agent"]
    assert agent_msgs, "expected an agent outbox message after the tool round"
    assert agent_msgs[-1].text == "the pipeline ran successfully"


# ---------------------------------------------------------------------------
# 5. The D19 pipeline__<name> resource-invoke path (the scope IS-5 adds
#    beyond IS-1's static pipeline__run verb).
# ---------------------------------------------------------------------------


def test_d19_pipeline_name_resolves_to_run_pipeline_with_curried_args() -> None:
    """Tier 2: OS invariant — the D19 resource-invoke rule routes
    ``pipeline__<name>`` to the ``run_pipeline`` target, currying the pipeline
    name from the qualified name and forwarding ``input`` unchanged. This is
    the whole point of the IS-5 D19 addition (the enumerate-all default scheme
    surfaces ``pipeline__<name>`` as a flat callable, so it MUST resolve) — and
    it is EQUIVALENT to invoking the pre-existing static ``pipeline__run`` verb
    with an explicit ``name``. Pure routing assertion (no handler invoked),
    same discipline as ``resolve_invoke_action``'s other resource-category
    tests."""
    from reyn.tools.universal_dispatch import resolve_invoke_action

    seed = {"name": "world"}

    # D19 per-name form: pipeline__greet, name curried from the qualified name.
    by_name = resolve_invoke_action("pipeline__greet", {"input": seed})
    # Static verb form: pipeline__run, name passed explicitly.
    by_verb = resolve_invoke_action("pipeline__run", {"name": "greet", "input": seed})

    assert by_name.target_tool_name == "run_pipeline"
    assert dict(by_name.target_args) == {"name": "greet", "input": seed}
    # The equivalence the _pipeline_run_args docstring claims: both spellings
    # reach run_pipeline with the SAME effective args.
    assert by_name.target_tool_name == by_verb.target_tool_name
    assert dict(by_name.target_args) == dict(by_verb.target_args)


def test_d19_pipeline_name_resolves_without_input_when_omitted() -> None:
    """Tier 2: OS invariant — ``pipeline__<name>`` with no ``input`` arg
    resolves to ``run_pipeline`` carrying only the curried ``name`` (a
    seed-less pipeline is a valid launch; the handler treats a missing
    ``input`` as no seed). Guards against the transformer injecting a
    spurious empty ``input`` key."""
    from reyn.tools.universal_dispatch import resolve_invoke_action

    resolved = resolve_invoke_action("pipeline__greet", {})

    assert resolved.target_tool_name == "run_pipeline"
    assert dict(resolved.target_args) == {"name": "greet"}


def _pipeline_by_name_invoke_result(name: str, seed_input: dict) -> LLMToolCallResult:
    """LLMToolCallResult that makes RouterLoop call the D19 per-name form
    ``invoke_action(action_name="pipeline__<name>", args={input})`` (the
    resource-invoke path, distinct from the static ``pipeline__run`` verb)."""
    return LLMToolCallResult(
        content=None,
        tool_calls=[
            {
                "id": "tc_pipeline_byname_001",
                "type": "function",
                "function": {
                    "name": "invoke_action",
                    "arguments": json.dumps({
                        "action_name": f"pipeline__{name}",
                        "args": {"input": seed_input},
                    }),
                },
            }
        ],
        finish_reason="tool_calls",
        usage=_EMPTY_USAGE,
    )


@pytest.mark.asyncio
async def test_run_pipeline_via_d19_pipeline_name_full_live_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2c: the D19 ``pipeline__<name>`` resource-invoke path end-to-end
    through the REAL router loop — proves the surfacing form the enumerator
    advertises (``pipeline__<name>``, not the generic ``pipeline__run`` verb)
    actually launches the registered pipeline and returns its real output.
    Same harness as the ``pipeline__run`` live-loop test above; only the
    LLM's chosen action name differs (the per-name D19 form), so this closes
    the coverage gap on exactly the path IS-5 adds."""
    monkeypatch.chdir(tmp_path)
    _reg, session = _registry_backed_session(tmp_path)

    session.pipeline_registry.register(
        "greet",
        Pipeline(
            steps=[TransformStep(value="'hello ' + ctx.name", output="greeting")],
            description="Greet the named recipient.",
        ),
    )

    stub = _make_llm_stub([
        _pipeline_by_name_invoke_result("greet", {"name": "world"}),
        _text_result("the pipeline ran successfully"),
    ])
    monkeypatch.setattr("reyn.runtime.router_loop.call_llm_tools", stub)

    await session._handle_user_message(
        "please run the greet pipeline", chain_id="chain-is5-002",
    )

    tool_messages = [m for m in session.history if m.role == "tool"]
    assert tool_messages, "expected at least one tool-result history entry"
    # #2425 案B: the sync run_pipeline result renders as its str ``output`` (plain text).
    assert tool_messages[-1].content == "hello world"
