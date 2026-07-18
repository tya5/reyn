"""#3070 — a probe/pre-flight `tool` step that FAILS on the tool's own error
(not a transport fault) must surface the real cause, never collapse into a
static "unreachable"/schema-mismatch string.

Root-cause chain measured on this issue (dogfood-coder's e2e report +
direct reproduction here): `rag_ingest.yaml`'s X1 pre-flight probes each MCP
server with a real tool call, schema-gated to `status == "ok"`
(`PreflightCheck`). All three servers' MCP HANDSHAKE succeeds
(`mcp_initialized`), but the chunker/vector-store PROBE TOOL CALL itself can
fail (observed: the `builtin-rag` extra -- apsw/sqlite-vec/chonkie -- not
installed in the probing environment raises `ModuleNotFoundError` INSIDE the
tool handler, which FastMCP reports as `isError: true` with the exception
text as content). Three swallow points hid that real exception end to end:

  1. `op_runtime/mcp.py`'s `mcp_completed` audit event recorded only the
     `is_error` boolean, never the tool's own error text -- undiagnosable
     from the P6 audit log alone.
  2. `executor.py`'s tool-step `verify: schema` failure raised
     `PipelineExecutionError` naming only the schema-validation error (e.g.
     "status: 'error' not in ['ok']"), discarding the tool's own result
     entirely.
  3. `executor.py`'s `parallel` step (`on_error: continue`) DROPS a failed
     branch from `collect`'s named `pipe` map by design (`_parallel_results`)
     -- so even with (2) fixed, the real cause never reached a `collect` step
     built from the R1 expression language, which could only ever observe
     ABSENCE, never WHY.

Each test below pins one closed swallow point. Real `EventLog` / `MCPClient`
/ `PipelineExecutor` / `SchemaRegistry` throughout (only the deepest fastmcp
transport is faked, mirroring `test_mcp_progress_and_timeout.py`'s own
precedent for this exact seam) -- no mocks of reyn's own collaborators.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from reyn.core.events.events import EventLog
from reyn.core.pipeline.executor import (
    ParallelStep,
    Pipeline,
    PipelineExecutionError,
    PipelineExecutor,
    ToolStep,
    TransformStep,
)
from reyn.core.pipeline.parser import PipelineParseError, parse_pipeline_dsl
from reyn.core.pipeline.schema import SchemaRegistry
from reyn.mcp.client import MCPClient
from reyn.schemas.models import MCPIROp

_PREFLIGHT_SCHEMA = {"fields": {"status": {"type": "enum", "values": ["ok"]}}}


# ── 1. the mcp op handler's own audit event carries the real text ───────────


class _StubPool:
    """Test double for MCPClientPool — get() returns a pre-set client (a359 P2),
    the same Real Fake `test_mcp_progress_and_timeout.py` already uses for this
    exact seam."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def __aenter__(self) -> "_StubPool":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    @property
    def owner_task(self) -> None:
        return None

    async def get(self, server: str, config: dict, *, agent_id: str | None = None) -> MCPClient:
        return self._client


class _FakeServerCapabilities:
    tools: Any = object()
    resources: Any = None
    prompts: Any = None
    logging: Any = None
    completions: Any = None


def _bypass_initialize(client: MCPClient, fake_fastmcp_client: Any) -> None:
    client._initialized = True
    client._client = fake_fastmcp_client
    client._negotiated_version = "2025-11-25"
    client._server_capabilities = _FakeServerCapabilities()


class _ErroringFastMCPClient:
    """Stands in for the deepest `fastmcp.Client` transport (see
    `test_mcp_progress_and_timeout.py`'s own precedent for faking this ONE
    seam): the tool call itself REPORTS an error (as a real FastMCP-wrapped
    ImportError would from a missing `builtin-rag` extra), it does not raise."""

    def __init__(self, error_text: str) -> None:
        self._error_text = error_text

    async def call_tool_mcp(
        self, name: str, arguments: "dict | None" = None, **kwargs: Any,
    ) -> Any:
        # `_result_to_dict` (reyn.mcp.client) reads `.content`/`.isError` as
        # plain ATTRIBUTES (mirrors ``mcp.types.CallToolResult``), and a
        # content item is either a pydantic model (``model_dump()``) or a
        # plain dict passed through unchanged -- the latter is simplest here.
        class _Result:
            content = [{"type": "text", "text": self._error_text}]
            isError = True
            structuredContent = None

        return _Result()


def test_mcp_completed_event_carries_the_real_error_text_on_isError() -> None:
    """Tier 2: `mcp_completed` records the tool's own error text (not just the
    `is_error` boolean) when the tool call reports `isError: true` -- the
    enabler this whole issue turns on: without it, an errored probe is
    undiagnosable from the P6 audit log alone."""
    from reyn.core.op_runtime import mcp as mcp_op_handler
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    real_text = "Error calling tool 'list_metadata': No module named 'sqlite_vec'"
    events = EventLog()
    ctx = OpContext(
        workspace=None,  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        mcp_servers={"reyn_vector_store": {"type": "stdio", "command": "/bin/true"}},
    )
    client = MCPClient({"type": "stdio", "command": "/bin/true"})
    _bypass_initialize(client, _ErroringFastMCPClient(real_text))
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="reyn_vector_store", tool="list_metadata", args={})
    asyncio.run(mcp_op_handler._execute(op, ctx))

    completed = [e for e in events.all() if e.type == "mcp_completed"]
    # exactly one `mcp_completed` event: this single-unpack IS the assertion
    # (raises ValueError if zero or more than one landed), not a length pin.
    (only,) = completed
    assert only.data["is_error"] is True
    assert only.data["error"] == real_text


def test_mcp_completed_event_carries_no_error_text_on_success() -> None:
    """Tier 2: the new `error` field stays None on a clean result -- the fix
    adds diagnosability to the failure path, it does not invent noise on the
    success path."""
    from reyn.core.op_runtime import mcp as mcp_op_handler
    from reyn.core.op_runtime.context import OpContext
    from reyn.security.permissions.permissions import PermissionDecl

    events = EventLog()
    ctx = OpContext(
        workspace=None,  # type: ignore[arg-type]
        events=events,
        permission_decl=PermissionDecl(),
        permission_resolver=None,
        mcp_servers={"reyn_chunker": {"type": "stdio", "command": "/bin/true"}},
    )
    client = MCPClient({"type": "stdio", "command": "/bin/true"})

    class _OkFastMCPClient:
        async def call_tool_mcp(self, name: str, arguments=None, **kwargs: Any) -> Any:
            class _Result:
                content = [{"type": "text", "text": "ok"}]
                isError = False
                structuredContent = None
            return _Result()

    _bypass_initialize(client, _OkFastMCPClient())
    ctx.mcp_pool = _StubPool(client)

    op = MCPIROp(kind="mcp", server="reyn_chunker", tool="chunk", args={})
    asyncio.run(mcp_op_handler._execute(op, ctx))

    completed = [e for e in events.all() if e.type == "mcp_completed"]
    (only,) = completed
    assert only.data["is_error"] is False
    assert only.data["error"] is None


# ── 2. the executor's schema-validation failure keeps the tool's own text ───


def test_schema_validation_failure_includes_the_tools_own_error_detail() -> None:
    """Tier 1: a `verify: schema`-gated tool step whose result fails
    validation because the TOOL ITSELF reported an error (a dict carrying
    `error`/`content`) surfaces that text in the raised
    `PipelineExecutionError` -- not just which schema field mismatched.
    STRIP-FALSIFY: reverting the `detail` block in `_run_tool_step` makes
    this RED (the message reverts to naming only `validation.errors`)."""
    registry = SchemaRegistry()
    registry.register("preflight", _PREFLIGHT_SCHEMA)

    def _erroring_dispatch(_name: str, _args: dict) -> dict:
        return {
            "status": "error",
            "content": "Error calling tool 'list_metadata': No module named 'sqlite_vec'",
        }

    pipeline = Pipeline(
        steps=[ToolStep(name="probe", args={}, output="p", schema="preflight")]
    )
    with pytest.raises(PipelineExecutionError) as excinfo:
        asyncio.run(
            PipelineExecutor().run(
                pipeline, None,
                tool_dispatch=_erroring_dispatch, state_log=None,
                run_id="run-3070-schema-detail", schema_registry=registry,
            )
        )
    assert "No module named 'sqlite_vec'" in str(excinfo.value)


def test_schema_validation_failure_with_no_tool_message_stays_unchanged() -> None:
    """Tier 1: when the failing result carries neither `error` nor `content`,
    no bogus detail is invented -- the message is exactly the pre-existing
    schema-only wording."""
    registry = SchemaRegistry()
    registry.register("preflight", _PREFLIGHT_SCHEMA)

    def _dispatch(_name: str, _args: dict) -> dict:
        return {"status": "error"}

    pipeline = Pipeline(
        steps=[ToolStep(name="probe", args={}, output="p", schema="preflight")]
    )
    with pytest.raises(PipelineExecutionError) as excinfo:
        asyncio.run(
            PipelineExecutor().run(
                pipeline, None,
                tool_dispatch=_dispatch, state_log=None,
                run_id="run-3070-schema-no-detail", schema_registry=registry,
            )
        )
    assert "the tool's own result" not in str(excinfo.value)


# ── 3. the dropped branch's real cause reaches collect's `pipe` ─────────────


def test_parallel_continue_surfaces_the_dropped_branchs_real_error_to_collect() -> None:
    """Tier 2: `on_error: continue` still DROPS the failed branch from the
    named-map `pipe` (unchanged -- `_parallel_results`'s existing contract),
    but `collect` can additionally read the branch's real failure text via
    the reserved `pipe.__branch_errors__.<name>` entry (#3070) -- the piece
    that lets a `collect` step (X1's `reachable` builder) report WHY a probe
    failed, not just THAT it did. STRIP-FALSIFY: reverting the
    `_parallel_branch_errors`/`PARALLEL_BRANCH_ERRORS_KEY` wiring in
    `_run_parallel_step` makes this RED (`__branch_errors__` never appears)."""
    registry = SchemaRegistry()
    registry.register("preflight", _PREFLIGHT_SCHEMA)

    def _dispatch(name: str, args: dict) -> dict:
        if args["v"] == "bad":
            return {
                "status": "error",
                "content": "Error calling tool 'chunk': No module named 'chonkie'",
            }
        return {"status": "ok"}

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="continue",
            branches={
                "good": ToolStep(name="probe", args={"v": "ok"}, schema="preflight"),
                "bad": ToolStep(name="probe", args={"v": "bad"}, schema="preflight"),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = asyncio.run(
        PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None,
            run_id="run-3070-parallel-cause", schema_registry=registry,
        )
    )
    # the surviving-map contract is UNCHANGED: "bad" is absent, not None.
    assert "bad" not in result.pipe_data
    assert "good" in result.pipe_data
    # ...and the real cause is reachable under the reserved key.
    errors = result.pipe_data["__branch_errors__"]
    assert "No module named 'chonkie'" in errors["bad"]
    assert "good" not in errors


def test_parallel_no_dropped_branch_never_adds_the_errors_key() -> None:
    """Tier 2: when no branch drops, `__branch_errors__` never appears at all
    -- the fix adds a key only on an actual drop, so a `collect` step that
    never fails a branch sees NO shape change."""
    registry = SchemaRegistry()
    registry.register("preflight", _PREFLIGHT_SCHEMA)

    def _dispatch(_name: str, _args: dict) -> dict:
        return {"status": "ok"}

    pipeline = Pipeline(steps=[
        ParallelStep(
            on_error="continue",
            branches={
                "a": ToolStep(name="probe", args={}, schema="preflight"),
                "b": ToolStep(name="probe", args={}, schema="preflight"),
            },
            collect=TransformStep(value="pipe"),
        ),
    ])
    result = asyncio.run(
        PipelineExecutor().run(
            pipeline, None,
            tool_dispatch=_dispatch, state_log=None,
            run_id="run-3070-parallel-no-drop", schema_registry=registry,
        )
    )
    assert "__branch_errors__" not in result.pipe_data


def test_parser_rejects_a_branch_named_the_reserved_errors_key() -> None:
    """Tier 1: a branch literally named `__branch_errors__` would collide
    with the executor's own reserved key (#3070) -- the parser fails it
    loud at parse time rather than letting it silently clobber the real
    dropped-branch-error map at run time."""
    dsl = """
pipeline: bad-branch-name
description: a branch shadows the reserved parallel-branch-errors key
steps:
  - parallel:
      on_error: continue
      branches:
        __branch_errors__: {transform: {value: "1"}}
      collect: {transform: {value: "pipe"}}
"""
    with pytest.raises(PipelineParseError, match="reserved"):
        parse_pipeline_dsl(dsl, SchemaRegistry())
