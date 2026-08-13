"""Tier 2 invariant tests for RouterHostAdapter (wave 3 PR3).

Verifies structural properties of RouterHostAdapter without using mocks of
collaborators. Real services (MemoryService, EventLog) and closure-based
callbacks are used throughout. No private state assertions — public surface only.
"""
from __future__ import annotations

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.router_loop import RouterLoopHost
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
)

# ---------------------------------------------------------------------------
# Minimal stubs and helpers
# ---------------------------------------------------------------------------
# #3607/#3482: the adapter takes the session's op-context SUPPLIER plus the
# mcp-gateway bundle. These module-level constants are the inert instances the
# tests below reuse — the real classes, kept default-free, with the defaulting
# in caller code (see McpGatewayInputs / RouterOpContextSource docstrings).
from tests._support.router_host_adapter import make_op_context_source  # noqa: E402

_EMPTY_OP_CTX = make_op_context_source()
_EMPTY_MCP_GATEWAY = McpGatewayInputs(
    mcp_connection_service=None, mcp_agent_id=None, ephemeral_fn=None,
)

class _FakeEventStore:
    """Minimal event store that discards events."""

    def emit(self, type: str, **data) -> None:
        pass


class _FakePermResolver:
    """Stub PermissionResolver with no configured permissions."""
    _config: dict = {}


def _null_async(*args, **kwargs):
    async def _inner(*a, **kw):
        return {}
    return _inner()


# The make_adapter builder and its inert null_* action ports now live in
# tests/_support (stable, location-independent import path). Aliased back to the
# original module-local names so the tests below are unchanged.
from tests._support.router_host_adapter import (  # noqa: E402
    make_adapter as _make_adapter,
)
from tests._support.router_host_adapter import (
    null_append_history as _null_append_history,
)
from tests._support.router_host_adapter import (
    null_file_delete as _null_file_delete,
)
from tests._support.router_host_adapter import (
    null_file_read as _null_file_read,
)
from tests._support.router_host_adapter import (
    null_file_regen as _null_file_regen,
)
from tests._support.router_host_adapter import (
    null_file_write as _null_file_write,
)
from tests._support.router_host_adapter import (
    null_mcp_call_tool as _null_mcp_call_tool,
)
from tests._support.router_host_adapter import (
    null_put_outbox as _null_put_outbox,
)

# ---------------------------------------------------------------------------
# Test 1: Protocol conformance (runtime_checkable isinstance)
# ---------------------------------------------------------------------------

def test_adapter_protocol_conformance(tmp_path):
    """Tier 2: RouterHostAdapter is structurally conformant with RouterLoopHost.

    Uses @runtime_checkable isinstance check — catches missing methods at
    refactor time without requiring a real LLM or full session.
    """
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test-agent",
        universal_wrappers_enabled=False,  # #4159: not exercised by this test
    )
    assert isinstance(adapter, RouterLoopHost), (
        "RouterHostAdapter must satisfy RouterLoopHost protocol structurally"
    )


# ---------------------------------------------------------------------------
# Test 2: the memory capability is handed over whole
# ---------------------------------------------------------------------------

def test_adapter_exposes_the_memory_capability_itself(tmp_path):
    """Tier 2: adapter.memory IS the injected MemoryService, and the adapter
    exposes no file primitive in its place.

    #3607: the adapter used to expose ``memory_path`` / ``memory_dir`` plus
    four file-op methods, out of which the router loop assembled the memory
    operations. What the router needs is the operations — so the capability
    is handed over whole, and the primitives it was assembled from are not
    on the host surface at all. The second assertion is the load-bearing
    one: re-adding a ``file_write`` delegate here re-opens the layering hole
    even if ``memory`` is also present.
    """
    events = EventLog(subscribers=[])
    workspace = tmp_path / "agents" / "test-agent"
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = _make_adapter(
        agent_workspace_dir=workspace,
        events=events,
        memory=memory,
        universal_wrappers_enabled=False,  # #4159: not exercised by this test
    )

    assert adapter.memory is memory
    assert adapter.memory.memory_path("shared", "s") == memory.memory_path("shared", "s")
    for primitive in (
        "file_read", "file_write", "file_delete", "file_regenerate_index",
        "memory_path", "memory_dir", "scan_for_block",
    ):
        assert not hasattr(adapter, primitive), (
            f"the router host must not expose {primitive!r}: memory operations "
            f"belong to the memory capability, not to the router's host surface"
        )


# ---------------------------------------------------------------------------
# Test 3: events identity
# ---------------------------------------------------------------------------

def test_events_identity(tmp_path):
    """Tier 2: adapter.events is the same EventLog object as the session's _audit_events.

    No duplicate event log surface — ensures there is a single append-only
    event log for the session (P6 compliance).
    """
    events = EventLog(subscribers=[])
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "test-agent",
        events=events,
        universal_wrappers_enabled=False,  # #4159: not exercised by this test
    )
    assert adapter.events is events, (
        "adapter.events must be the same EventLog instance as injected"
    )


# ---------------------------------------------------------------------------
# (Formerly) Test 4: delegation tracker via callback — deleted in #4150.
# RouterHostAdapter.send_to_agent() (and the RouterLoopHost protocol member
# it implemented) had zero callers after P6 (#3978) removed the sole
# producer of the closure that used to reach it (router_loop.py's
# _send_to_agent_bound, removed in #4144) — this test drove the now-deleted
# method directly, so its subject is gone. The live peer-dispatch transport,
# InterAgentMessaging.send_to_agent (reached via Session._send_to_agent),
# never went through this adapter method and is untouched.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# #53 regression — permission_resolver property + intervention_bus wiring
# ---------------------------------------------------------------------------

def test_adapter_exposes_permission_resolver_property(tmp_path):
    """Tier 2: adapter.permission_resolver returns the resolver from __init__.

    Regression for #53. RouterLoop builds the ToolContext via
    ``getattr(self.host, "permission_resolver", None)``. Before the fix
    this returned None (the adapter stored the resolver as ``_perm`` only),
    so every router-invoked tool's permission_resolver was silently None
    and Tier-1 config-deny checks (web.fetch, mcp, …) were bypassed.

    The property must mirror what was passed to ``permission_resolver=``
    at construction time so the getattr lookup wires the right object.
    """
    sentinel = object()  # any non-None value — we only assert identity
    adapter = _make_adapter(
        agent_workspace_dir=tmp_path / "agents" / "alpha",
        universal_wrappers_enabled=False,  # #4159: not exercised by this test
    )
    # Re-build with the resolver argument set — _make_adapter doesn't take it.
    from reyn.core.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.services import MemoryService
    from reyn.runtime.services.router_host_adapter import RouterHostAdapter
    workspace = tmp_path / "agents" / "alpha2"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="alpha2",
        agent_role="role",
        output_language=None,
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=sentinel,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )

    assert adapter.permission_resolver is sentinel, (
        "adapter.permission_resolver must mirror the __init__ argument so "
        "RouterLoop's ToolContext.permission_resolver getattr lookup wires "
        "the session's resolver into router-invoked tool dispatch (#53)."
    )


def test_make_router_op_context_wires_intervention_bus(tmp_path):
    """Tier 2: make_router_op_context populates ``ctx.intervention_bus``
    via the ``intervention_bus_factory`` callable when provided.

    Regression for #53. web_fetch / mcp install / mcp drop handlers all
    guard ``if ctx.intervention_bus is None`` and raise RuntimeError
    when missing. Without this wiring, even a properly-resolved
    permission_resolver crashes the router path before it can deny.
    """
    from reyn.core.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.services import MemoryService
    from reyn.runtime.services.router_host_adapter import RouterHostAdapter
    workspace = tmp_path / "agents" / "bus-test"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    sentinel_bus = object()  # we only assert identity, not protocol
    adapter = RouterHostAdapter(
        agent_name="bus-test",
        agent_role="role",
        output_language=None,
        op_context_source=make_op_context_source(
            intervention_bus_factory=lambda: sentinel_bus,
        ),
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        intervention_bus_factory=lambda: sentinel_bus,
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )

    op_ctx = adapter.make_router_op_context()
    assert op_ctx.intervention_bus is sentinel_bus, (
        "make_router_op_context must call intervention_bus_factory() and "
        "wire the result into ctx.intervention_bus (#53)."
    )


def test_make_router_op_context_no_factory_leaves_bus_none(tmp_path):
    """Tier 2: factory-not-provided path keeps intervention_bus=None.

    Backward-compat sibling to the wiring test — narrow test sites that
    don't pass intervention_bus_factory must get the old behaviour
    (None bus), so the config-deny path still works without forcing
    every adapter caller to wire a bus.
    """
    from reyn.core.events.events import EventLog
    from reyn.llm.model_resolver import ModelResolver
    from reyn.runtime.services import MemoryService
    from reyn.runtime.services.router_host_adapter import RouterHostAdapter
    workspace = tmp_path / "agents" / "nobus-test"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="nobus-test",
        agent_role="role",
        output_language=None,
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=None,
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )

    op_ctx = adapter.make_router_op_context()
    assert op_ctx.intervention_bus is None


# ---------------------------------------------------------------------------
# Test: get_inbox_depth() tolerates a partial registry (#4127 CI falsify)
# ---------------------------------------------------------------------------

class _PartialAgentRegistry:
    """A registry double implementing only PART of AgentRegistry — no
    ``get_session``. This is a legitimate, deliberately-minimal stub (the
    same shape as ``tests/llm/test_router_loop_chatsession.py``'s
    ``_StubAgentRegistry``, which has ``get_or_load``/``exists``/etc. but no
    ``get_session``), not a bug in the stub itself.

    #4127 CI regression: ``RouterHostAdapter.get_inbox_depth()`` originally
    called ``self._registry.get_session(...)`` directly, so a registry
    double shaped exactly like this one raised ``AttributeError`` mid-turn
    when ``build_resource_caller_state`` called it unconditionally — and
    that propagated far enough to derail FOUR unrelated tests'
    turn-dispatch flow in ``tests/llm/test_router_loop_chatsession.py`` and
    ``tests/runtime/test_session_invariants.py``. This test falsifies that
    regression directly against the adapter."""

    def get_or_load(self, agent_name: str) -> object:
        raise AssertionError("get_inbox_depth must not reach get_or_load")

    def exists(self, agent_name: str) -> bool:
        return True


def test_get_inbox_depth_tolerates_a_registry_without_get_session(tmp_path):
    """Tier 2: get_inbox_depth() degrades to None, not AttributeError, when
    the wired registry lacks get_session — #4127 CI falsify regression."""
    workspace = tmp_path / "agents" / "partial-registry-test"
    events = EventLog(subscribers=[])
    memory = MemoryService(
        agent_workspace_dir=workspace,
        events=events,
        file_write=_null_file_write,
        file_read=_null_file_read,
        file_delete=_null_file_delete,
        file_regenerate_index=_null_file_regen,
    )
    adapter = RouterHostAdapter(
        agent_name="partial-registry-test",
        agent_role="role",
        output_language=None,
        op_context_source=_EMPTY_OP_CTX,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=ModelResolver({}),
        memory=memory,
        journal=None,
        agent_registry=_PartialAgentRegistry(),
        agent_workspace_dir=workspace,
        mcp_call_tool=_null_mcp_call_tool,
        mcp_gateway_inputs=_EMPTY_MCP_GATEWAY,
        put_outbox_inputs=PutOutboxInputs(
            put_outbox=_null_put_outbox, agent_replies_tracker=lambda: None,
        ),
        append_history=_null_append_history,
        live_session_id_inputs=LiveSessionIdInputs(
            session_id=None, live_session_id_fn=None,
        ),
        universal_wrappers_enabled=False,  # #4159: preserves prior implicit default
    )

    assert adapter.get_inbox_depth() is None
