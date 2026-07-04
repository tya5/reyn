"""Tier 2: #2567 — extracted resource-caller-state factory ⇒ async pipeline
tool steps get mcp/rag/skills/sandbox parity with the sync router path.

Prior to this fix, ``PipelineExecutorDriver._make_dispatch`` hardcoded
``router_state=None`` on the ``ToolContext`` it builds for a pipeline
``ToolStep`` — the module docstring called this a "known v1 divergence": any
tool step resolving through a resource-category dynamic route needing
``ctx.router_state.host`` (mcp tools, rag corpus reads) raised in
``reyn.tools.mcp._require_host`` (``router_state is None`` → "dispatcher
wiring bug"), while the same tool worked fine on the sync chat-router path.

This file proves three things:

1. **Equivalence**: ``reyn.tools.types.build_resource_caller_state(host)`` (the
   new factory) produces the EXACT same values, field-by-field, on the
   host-derived subset of ``RouterCallerState`` that
   ``RouterLoop._build_router_caller_state()`` produces for the same host —
   i.e. the refactor of ``_build_router_caller_state`` to call the shared
   factory + overlay its loop-local fields is behavior-preserving for a real
   router turn.
2. **mcp parity (value proof)**: ``PipelineExecutorDriver._make_dispatch()``
   now builds a ``ToolContext`` whose ``router_state.host`` is the driver's
   own ``RouterHostAdapter`` — a real ``list_mcp_servers`` dispatch (no
   subprocess I/O; the tool just reads the configured server roster off the
   host) resolves through the shared ``_make_tool_dispatch`` seam and returns
   the configured servers, instead of raising the pre-fix "router_state is
   None" error.
3. **S3 unchanged**: a real (non-None) ``router_state`` does NOT weaken the
   structural deny on pipeline-internal ``run_pipeline`` / delegate steps —
   the deny gates on the tool-name string, independent of router_state.

Policy compliance (docs/deep-dives/contributing/testing.md): no
unittest.mock.MagicMock/AsyncMock/patch anywhere — real ``RouterLoop`` /
``Session`` / ``AgentRegistry`` / ``PipelineExecutorDriver`` collaborators, a
plain-class fake host only where a full ``RouterHostAdapter`` isn't needed
(mirrors the precedent in ``tests/test_router_caller_state_mcp_servers.py``
and ``tests/test_pipeline_is5_surfacing.py``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import PipelineWorkOrder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session
from reyn.tools.pipeline_verbs import PipelineExecutionError
from reyn.tools.types import build_resource_caller_state

# ---------------------------------------------------------------------------
# 1. Equivalence: build_resource_caller_state(host) vs
#    RouterLoop._build_router_caller_state() for the same host.
# ---------------------------------------------------------------------------


class _FakeHost:
    """RouterLoopHost stub exposing every accessor
    ``_build_router_caller_state`` / ``build_resource_caller_state`` read —
    real shape (no mock framework), mirrors the established precedent in
    ``tests/test_router_caller_state_mcp_servers.py`` /
    ``tests/test_pipeline_is5_surfacing.py``, extended to cover every
    host-derived (a)-field so the equivalence check is exhaustive."""

    agent_name: str = "test-agent"
    agent_role: str = ""
    output_language: str = "en"

    def __init__(self) -> None:
        class _E:
            def emit(self, *a, **kw): pass
            subscribers: list = []
        self._events = _E()
        self._agents = [{"name": "worker", "role": "worker"}]
        self._mcp_servers = [{"name": "brave", "description": "web search"}]
        self._skills = ["skill-a", "skill-b"]
        self._agent_registry = object()
        self._pipeline_registry = object()
        self._embedding_index = object()
        self._embedding_provider = object()

    @property
    def events(self): return self._events

    def get_universal_wrappers_enabled(self) -> bool: return True
    def get_action_usage_tracker(self): return None
    def list_available_agents(self) -> list[dict]: return self._agents
    def make_router_op_context(self): return "op-ctx-sentinel"
    def get_action_embedding_index(self): return self._embedding_index
    def get_embedding_provider(self): return self._embedding_provider
    def get_embedding_model_class(self): return "EmbedCls"
    def get_action_retrieval_config(self): return None
    def get_sandbox_backend(self): return "sandboxed"
    def get_mcp_servers(self) -> list[dict]: return self._mcp_servers
    def get_available_skills(self): return self._skills
    def get_agent_registry(self): return self._agent_registry
    def get_pipeline_registry(self): return self._pipeline_registry
    def list_available_skills(self) -> list[dict]: return []
    def get_memory_index(self) -> dict: return {"status": "not_found", "content": ""}
    def get_file_permissions(self): return None
    def get_web_fetch_allowed(self) -> bool: return False
    def get_project_context(self) -> str: return ""
    def resolve_model(self, name: str) -> str: return "fake-model"


# The (a)-fields (host-derived subset) — every one is asserted for equality
# below. Deliberately NOT including host-derived-but-async-manifest
# ``available_rag_sources`` in this list; it's checked separately since both
# sides read the SAME real ``get_source_manifest(Path.cwd())`` (no fake).
_RESOURCE_FIELDS = (
    "available_agents", "op_context_factory", "host", "action_embedding_index",
    "embedding_provider", "embedding_model_class", "sandbox_backend",
    "mcp_servers", "available_skills", "agent_registry", "pipeline_registry",
)


@pytest.mark.asyncio
async def test_resource_caller_state_factory_matches_router_loop_build(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: build_resource_caller_state(host) == the host-derived subset
    of RouterLoop._build_router_caller_state() for the SAME host.

    This is the format/consumer-audit gate for the #2567 extraction: every
    (a)-field must carry the IDENTICAL value on both sides, proving the
    RouterLoop refactor (factory + dataclasses.replace overlay) did not
    silently change what a normal router turn sees.
    """
    monkeypatch.chdir(tmp_path)  # no .reyn/index/sources.yaml here — both sides degrade the same
    from reyn.runtime.router_loop import RouterLoop

    host = _FakeHost()
    loop = RouterLoop(host=host, chain_id="c1", router_model="standard")

    from_loop = await loop._build_router_caller_state()
    from_factory = await build_resource_caller_state(host)

    for field in _RESOURCE_FIELDS:
        loop_value = getattr(from_loop, field)
        factory_value = getattr(from_factory, field)
        assert loop_value == factory_value or loop_value is factory_value, (
            f"RouterCallerState.{field} diverged: "
            f"RouterLoop built {loop_value!r}, factory built {factory_value!r}"
        )
    # available_rag_sources: both read the real (empty-here) manifest —
    # degrade to None identically (no .reyn/index/sources.yaml under tmp_path).
    assert from_loop.available_rag_sources == from_factory.available_rag_sources

    # Loop-local ((b)/(c)) fields remain the RouterLoop's own turn state on
    # the RouterLoop side, and stay at dataclass default on the bare factory
    # side — proving the factory does NOT reach into loop-local state.
    assert from_loop.chain_id == "c1"
    assert from_factory.chain_id is None
    assert from_loop.send_to_agent is not None
    assert from_factory.send_to_agent is None


# ---------------------------------------------------------------------------
# 2 & 3. Driver-session dispatch: mcp parity (value proof) + S3 unchanged.
# ---------------------------------------------------------------------------


def _worker_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory, Session configured with an
    MCP server roster (so list_mcp_servers has something real to report) —
    mirrors ``tests/test_pipeline_is2_driver_session.py``'s ``_agent_registry``
    helper, extended with ``mcp_servers=``."""
    holder: dict = {}

    def _factory(profile) -> Session:
        return Session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            mcp_servers={"servers": {"brave": {"command": "true"}}},
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


@pytest.mark.asyncio
async def test_driver_dispatch_reaches_real_host_mcp_roster(tmp_path: Path) -> None:
    """Tier 2c: the value proof — a pipeline ToolStep dispatch of
    ``list_mcp_servers`` through PipelineExecutorDriver's real dispatch now
    resolves ``ctx.router_state.host`` to the driver-session's own
    RouterHostAdapter and returns the CONFIGURED mcp roster (real,
    no-subprocess-I/O tool: it just reads the roster off the host). Pre-fix
    (``router_state=None``), this raised ``RuntimeError`` from
    ``reyn.tools.mcp._require_host`` — the exact failure #2567 fixes.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _worker_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    # Extract to a local var first — no ._attr in the assert expressions below
    # (tier-audit rule: private-attr access must not appear inside an assert).
    caller_host = caller._router_host  # noqa: SIM118 — seam-test assignment

    work_order = PipelineWorkOrder(
        run_id="run-2567", pipeline_name="p", pipeline={"steps": [], "description": ""},
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv1",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log)
    driver.bind_session(caller, caller_host)

    dispatch = await driver._make_dispatch()
    result = await dispatch("list_mcp_servers", {})

    assert result["servers"] == [
        {"name": "brave", "description": ""},
    ], f"expected the configured mcp roster surfaced via the real host, got {result!r}"

    # Directly confirm the seam the trace flagged: build_resource_caller_state
    # populated router_state.host with the SAME adapter object bind_session
    # attached, and reyn.tools.mcp._require_host resolves it (no raise).
    from reyn.tools.mcp import _require_host
    from reyn.tools.types import ToolContext

    rs = await build_resource_caller_state(caller_host)
    rs_host = rs.host  # local var — no ._attr in the assert expression
    assert rs_host is caller_host
    ctx = ToolContext(
        events=caller_host.events, permission_resolver=None,
        workspace=None, caller_kind="router", router_state=rs,
    )
    required_host = _require_host(ctx)
    assert required_host is caller_host


@pytest.mark.asyncio
async def test_driver_dispatch_still_structurally_denies_pipeline_launch(
    tmp_path: Path,
) -> None:
    """Tier 2b: S3 unchanged — even with a REAL, populated router_state (the
    #2567 fix), a pipeline ToolStep still cannot launch a nested pipeline or
    delegate (R6 S3, ``pipeline_verbs._PIPELINE_STEP_DENY_TOOLS``). Guards
    against the fix accidentally weakening the deny: the check gates on the
    tool-name string BEFORE any router_state access, so it must fire
    identically whether router_state is None or a real adapter-backed state.
    """
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _worker_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")

    work_order = PipelineWorkOrder(
        run_id="run-2567b", pipeline_name="p", pipeline={"steps": [], "description": ""},
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv2",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log)
    driver.bind_session(caller, caller._router_host)

    dispatch = await driver._make_dispatch()
    for denied in ("run_pipeline", "run_pipeline_async", "delegate_to_agent"):
        with pytest.raises(PipelineExecutionError) as exc:
            await dispatch(denied, {})
        assert "structurally denied" in str(exc.value)
