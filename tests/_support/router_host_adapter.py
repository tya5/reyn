"""Shared RouterHostAdapter test builder with real collaborators (no mocks).

Real services (MemoryService, EventLog) and closure-based callbacks are used
throughout. The ``null_*`` callables are inert async stubs (real callables, not
mocks) used as the adapter's action ports when a test only needs construction.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from reyn.core.events.events import EventLog
from reyn.llm.model_resolver import ModelResolver
from reyn.runtime.router_op_context import RouterOpContextSource
from reyn.runtime.services import (
    LiveSessionIdInputs,
    McpGatewayInputs,
    MemoryService,
    PutOutboxInputs,
    RouterHostAdapter,
)

# Every RouterOpContextSource field, all inert. Deliberately spelled out
# rather than defaulted on the class: the class stays default-free (a silent
# omission there would absorb a wiring change unnoticed), so adding a field to
# it makes make_op_context_source raise TypeError here — one loud edit instead
# of a quiet None in every test.
_INERT_OP_CONTEXT_SOURCE_FIELDS: dict = {
    "events": None,
    "permission_resolver": None,
    "file_permissions_fn": None,
    "mcp_servers_fn": None,
    "mcp_servers_flat_fn": None,
    "allowed_mcp_fn": None,
    "workspace_base_dir_fn": None,
    "workspace_state_dir": None,
    "environment_backend": None,
    "sandbox_backend": None,
    "sandbox_policy_fn": None,
    "agent_id": None,
    "agent_name": None,  # #4574
    "intervention_bus_factory": None,
    "presentation_renderer_factory": None,
    "presentation_registry_fn": None,
    "multimodal_config": None,
    "web_fetch_config": None,  # #4274
    "media_store_fn": None,
    "compact_now": None,
    "threat_scan": None,
    "contextual_permission_fn": None,
    "session_id_fn": None,
    "child_temp_dir": "",
    "hook_dispatcher": None,
    "hook_bus": None,
    "turn_origin_fn": None,
    "hot_reloader": None,
    "render_template_bounds": None,
    "budget_gateway": None,
    "available_skills_fn": None,
    "ephemeral_fn": None,  # #3903 a-2 ③
    "attended_fn": None,  # #4193 ①
}


def make_op_context_source(**overrides: object) -> RouterOpContextSource:
    """A REAL RouterOpContextSource with everything inert but *overrides*.

    The real class, not a stand-in: it is a plain object with no I/O, so a test
    that needs one builds one. The test harness owns its temporary directory.
    """
    unknown = set(overrides) - set(_INERT_OP_CONTEXT_SOURCE_FIELDS)
    assert not unknown, f"not RouterOpContextSource fields: {sorted(unknown)}"
    scratch = tempfile.TemporaryDirectory()
    fields = {**_INERT_OP_CONTEXT_SOURCE_FIELDS, "child_temp_dir": scratch.name, **overrides}
    source = RouterOpContextSource(**fields)
    source._test_temp_dir = scratch
    return source


async def null_file_read(path: str) -> dict:
    return {"content": ""}


async def null_file_write(path: str, content: str) -> dict:
    return {"path": path, "written": True}


async def null_file_delete(path: str) -> dict:
    return {"path": path, "deleted": True}


async def null_file_regen(*, path, output_path, entry_template, header) -> dict:
    return {"path": path, "output_path": output_path, "entries": 0}


async def null_mcp_call_tool(server: str, tool: str, args: dict) -> dict:
    return {}


async def null_put_outbox(msg) -> None:
    pass


def null_append_history(msg) -> None:
    pass


def make_adapter(
    *,
    agent_name: str = "test-agent",
    agent_workspace_dir: Path | None = None,
    events: EventLog | None = None,
    memory: MemoryService | None = None,
    agent_replies_list: "list[str] | None" = None,
    resolver: ModelResolver | None = None,
    turn_budget_engine: object = None,
    environment_backend: object = None,  # #1477: optional for sandbox-cwd tests
    session_id: "str | None" = None,
    turn_origin_fn: "object | None" = None,  # proposal 0060 Phase 1 (A7): per-turn provenance source
    workspace_base_dir: "object | None" = None,  # router-op-ctx Workspace root (else cwd)
    agent_registry: object = None,  # #2103: real AgentRegistry for spawn/topology seams
    pipeline_registry: object = None,  # IS-5: real PipelineRegistry for run_pipeline lookup
    on_limit: object = None,  # #2175: OnLimitConfig for the spawn-limit checkpoint (None → no checkpoint = unattended reject)
    safety_extensions: "dict | None" = None,  # #2175: shared per-run extension dict
    intervention_answer: "str | None" = None,  # #2175: interactive-mode bus answer (choice_id, e.g. "yes")
    peek_mid_turn_injection: "object | None" = None,  # #3792
    commit_mid_turn_injection: "object | None" = None,  # #3792
    # #4159 (remainder recorded on the issue, closed out here): this test
    # builder used to keep its OWN False default one layer above
    # RouterHostAdapter's own now-required kwarg — the exact same
    # implicit/config mismatch shape one level removed, and the exact
    # "vacuous pass" risk a wrapper-visibility assertion could silently
    # exercise the wrong path under. No default now; every caller states
    # what it means. All params are keyword-only (the bare ``*`` above) so
    # this doesn't need to move in the parameter list.
    universal_wrappers_enabled: bool,
) -> RouterHostAdapter:
    """Construct a minimal RouterHostAdapter with real collaborators."""
    if events is None:
        events = EventLog(subscribers=[])
    # #2175: a REAL on_limit checkpoint (no mock) wrapping handle_limit_exceeded with a
    # real OnLimitConfig + a real approving/declining bus, so spawn-limit tests exercise
    # the actual framework. None on_limit + None answer → no checkpoint wired → the host
    # adapter degrades to unattended (reject) — the C3 hard-deny posture.
    _ext: dict = safety_extensions if safety_extensions is not None else {}
    _checkpoint = None
    if on_limit is not None:
        from reyn.runtime.limits.limit_handler import handle_limit_exceeded

        class _FixedAnswerBus:  # real callable bus (not a mock)
            async def request(self, iv):  # noqa: ANN001
                from reyn.user_intervention import InterventionAnswer
                return InterventionAnswer(choice_id=intervention_answer)

        _bus = _FixedAnswerBus() if intervention_answer is not None else None

        async def _checkpoint_fn(*, kind, prompt, detail, extension_amount, run_id=None):
            decision = await handle_limit_exceeded(
                bus=_bus, on_limit=on_limit, kind=kind, run_id=run_id or "test",
                prompt=prompt, detail=detail, extension_amount=extension_amount,
            )
            if decision.allow_continue:
                _ext[kind] = _ext.get(kind, 0.0) + decision.extension
            return decision

        _checkpoint = _checkpoint_fn
    workspace = agent_workspace_dir or Path(".reyn") / "agents" / agent_name
    if memory is None:
        memory = MemoryService(
            agent_workspace_dir=workspace,
            events=events,
            file_write=null_file_write,
            file_read=null_file_read,
            file_delete=null_file_delete,
            file_regenerate_index=null_file_regen,
        )
    if resolver is None:
        resolver = ModelResolver({})

    _replies = agent_replies_list

    op_context_source = make_op_context_source(
        events=events,
        environment_backend=environment_backend,
        turn_origin_fn=turn_origin_fn,
        # #4200: RouterOpContextSource now reads this LIVE (workspace_base_dir_fn),
        # same reason session_id_fn below is a callable — see that class's own
        # docstring. This test builder's own param stays a plain value (test
        # ergonomics); wrap it once here at the one forwarding site.
        workspace_base_dir_fn=(
            (lambda: workspace_base_dir) if workspace_base_dir is not None else None
        ),
        session_id_fn=(lambda: session_id) if session_id is not None else None,
        agent_name=agent_name,  # #4574: mirrors the real Session's own wiring
    )
    mcp_gateway_inputs = McpGatewayInputs(
        mcp_connection_service=None,
        mcp_agent_id=None,
        ephemeral_fn=None,
    )
    put_outbox_inputs = PutOutboxInputs(
        put_outbox=null_put_outbox,
        agent_replies_tracker=lambda: _replies,
    )
    live_session_id_inputs = LiveSessionIdInputs(
        session_id=session_id,
        live_session_id_fn=None,
    )

    return RouterHostAdapter(
        agent_name=agent_name,
        agent_role="test role",
        output_language="en",
        op_context_source=op_context_source,
        permission_resolver=None,
        mcp_servers=None,
        project_context="",
        events=events,
        resolver=resolver,
        memory=memory,
        journal=None,
        agent_registry=agent_registry,
        pipeline_registry=pipeline_registry,  # IS-5
        handle_chat_limit_checkpoint=_checkpoint,  # #2175
        safety_extensions=_ext,  # #2175
        agent_workspace_dir=workspace,
        mcp_call_tool=null_mcp_call_tool,
        mcp_gateway_inputs=mcp_gateway_inputs,
        put_outbox_inputs=put_outbox_inputs,
        append_history=null_append_history,
        live_session_id_inputs=live_session_id_inputs,
        # #3671 follow-up: RouterHostAdapter takes a FACTORY now (computed at
        # most once, on first reference — see its own module for why). This
        # helper's own callers still pass an already-built engine value (or
        # None), so wrap it in a trivial factory here rather than pushing the
        # factory shape onto every call site.
        turn_budget_engine_factory=(
            (lambda: turn_budget_engine) if turn_budget_engine is not None else None
        ),
        environment_backend=environment_backend,
        peek_mid_turn_injection=peek_mid_turn_injection,  # #3792
        commit_mid_turn_injection=commit_mid_turn_injection,  # #3792
        universal_wrappers_enabled=universal_wrappers_enabled,  # #4159
    )
