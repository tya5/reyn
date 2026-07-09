"""#2692 — present + render_template are reachable from chat AND pipeline (part of the #2688 sweep).

The bug: the ``present`` / ``render_template`` op handlers existed and worked, but neither had a
``ToolDefinition``. So the default chat JSON tool catalog never offered them (0/69) AND
``registry.lookup("present")`` returned ``None`` → a pipeline ``tool: present`` step raised a
PipelineExecutionError ("not a registered tool"). The headline present-layer arc was reachable from
nowhere.

The fix registers one ``ToolDefinition`` per op in the single unified registry, which opens BOTH
surfaces (chat via build_tools + gates.router="allow"; pipeline via bare-name lookup). These are the
anti-regression proofs on both surfaces + the read-authority-preservation, tool→op bridge, tiered
schema, and FP-0056 canonical-declaration invariants. Real Workspace / PermissionResolver / EventLog /
PipelineExecutor — no collaborator mocks; assertions on the public op result and the built catalog,
never private state or exact rendered whitespace.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.core.events.events import EventLog
from reyn.core.offload.canonical import canonical_declaration, to_canonical
from reyn.core.op_runtime.context import OpContext
from reyn.core.pipeline.executor import Pipeline, PipelineExecutor, ToolStep
from reyn.data.workspace.workspace import Workspace
from reyn.runtime.router_tools import build_tools
from reyn.security.permissions.permissions import PermissionDecl, PermissionResolver
from reyn.tools import RouterCallerState, ToolContext, get_default_registry
from reyn.tools.pipeline_verbs import _make_tool_dispatch
from reyn.tools.present import _handle_present
from reyn.tools.render_template import _handle_render_template


def _run(coro):
    return asyncio.run(coro)


def _resolver(tmp_path: Path, config_permissions: dict | None = None) -> PermissionResolver:
    return PermissionResolver(
        config_permissions=config_permissions or {},
        project_root=tmp_path,
        interactive=False,
    )


def _tool_ctx(
    tmp_path: Path,
    resolver: PermissionResolver | None,
    *,
    with_factory: bool = False,
) -> ToolContext:
    """A real ToolContext. ``with_factory=False`` exercises the minimal-OpContext path
    (deny-by-default read-authority); ``with_factory=True`` threads a real OpContext via
    ``router_state.op_context_factory`` (the chat precedent)."""
    events = EventLog()
    ws = Workspace(events=events, permission_resolver=resolver)
    router_state = None
    if with_factory:
        def _factory() -> OpContext:
            return OpContext(
                workspace=ws,
                events=events,
                permission_decl=PermissionDecl(),
                permission_resolver=resolver,
                actor="t2692",
            )
        router_state = RouterCallerState(op_context_factory=_factory)
    return ToolContext(
        events=events,
        permission_resolver=resolver,
        workspace=ws,
        caller_kind="router",
        router_state=router_state,
    )


# ── registry wiring (the root cause) ─────────────────────────────────────────


def test_present_and_render_template_resolve_in_the_default_registry():
    """Tier 2: registry.lookup resolves both — the #2692 bug was exactly that these
    returned None (no ToolDefinition), so pipeline + chat could not reach the ops."""
    registry = get_default_registry()
    assert registry.lookup("present") is not None
    assert registry.lookup("render_template") is not None


# ── anti-regression: chat surface (reverses build_tools 0/69) ────────────────


def test_build_tools_contains_present_and_render_template():
    """Tier 2: the default chat catalog (build_tools output) now advertises present +
    render_template — directly reversing the observed 0/69 (unreachable from chat)."""
    tools = build_tools([{"name": "a1", "description": "d"}])
    names = {t["function"]["name"] for t in tools}
    assert "present" in names
    assert "render_template" in names


# ── anti-regression: pipeline surface (reverses PipelineExecutionError) ──────


@pytest.mark.asyncio
async def test_pipeline_present_step_reaches_the_op():
    """Tier 2: a real pipeline `tool: present` step resolves via registry.lookup and
    reaches execute_op — returning the present ack, NOT the prior 'not a registered
    tool' PipelineExecutionError. data_inline avoids the read-authority gate so this
    isolates the dispatch-resolution reversal."""
    ctx = _tool_ctx(Path("/tmp"), None, with_factory=True)
    pipeline = Pipeline(
        steps=[
            ToolStep(
                name="present",
                args={"data_inline": {"title": "Q3", "rows": [1, 2, 3]}},
                output="ack",
            ),
        ]
    )
    result = await PipelineExecutor().run(
        pipeline, {}, tool_dispatch=_make_tool_dispatch(ctx), state_log=None,
        run_id="run-2692-present",
    )
    # The step's output carries the present op ack — proof the op ran (not an error).
    store = result.named_stores["ack"]
    assert store is not None


@pytest.mark.asyncio
async def test_pipeline_render_template_step_reaches_the_op():
    """Tier 2: a real pipeline `tool: render_template` step resolves + reaches
    execute_op, returning the rendered string — reversing the prior PipelineExecutionError."""
    ctx = _tool_ctx(Path("/tmp"), None, with_factory=True)
    pipeline = Pipeline(
        steps=[
            ToolStep(
                name="render_template",
                args={"template": "hi {{ data.name }}", "data_inline": {"name": "reyn"}},
                output="rendered",
            ),
        ]
    )
    result = await PipelineExecutor().run(
        pipeline, {}, tool_dispatch=_make_tool_dispatch(ctx), state_log=None,
        run_id="run-2692-render",
    )
    # The rendered string reached the step store (canonical text of the producer).
    store = result.named_stores["rendered"]
    assert "hi reyn" in json.dumps(store)


# ── tool → op bridge builds the correct IR op ────────────────────────────────


def test_present_tool_builds_present_op_and_reaches_handler():
    """Tier 1: the present tool handler builds a real PresentIROp from its args and
    dispatches through execute_op — the minimal data_inline call routes to the stage-3
    default viewer (mode='default') and acks ok."""
    ctx = _tool_ctx(Path("/tmp"), None)
    result = _run(_handle_present({"data_inline": {"rows": [{"a": 1}]}}, ctx))
    assert result["kind"] == "present"
    assert result["status"] == "ok"
    assert result["mode"] == "default"


def test_render_template_tool_builds_op_and_renders():
    """Tier 1: the render_template tool handler builds a real RenderTemplateIROp and the
    op renders the inline template against the inline data (round-trips the args)."""
    ctx = _tool_ctx(Path("/tmp"), None)
    result = _run(_handle_render_template(
        {"template": "value={{ data.x }}", "data_inline": {"x": 42}}, ctx,
    ))
    assert result["kind"] == "render_template"
    assert result["status"] == "ok"
    assert "value=42" in result["rendered"]


# ── read-authority equivalence: present tool denial ⇔ file.read denial ───────


def test_present_tool_data_ref_denied_iff_file_read_denied(tmp_path, monkeypatch):
    """Tier 1: a present tool data_ref read goes through the SAME gate as file.read — a
    config file.read:deny denies BOTH. Proves the tool adds no read-authority bypass
    (real resolver, not None). Falsify (allow) below."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.json").write_text(json.dumps({"x": 1}))
    deny = _resolver(tmp_path, {"file": {"read": "deny"}})
    ctx = _tool_ctx(tmp_path, deny)
    result = _run(_handle_present({"data_ref": "data.json"}, ctx))
    assert result["status"] == "denied"
    # file.read denied on the same path (the equivalence, both directions).
    with pytest.raises(PermissionError):
        _run(deny.require_file_read(PermissionDecl(), str(tmp_path / "data.json"), "t"))


def test_present_tool_data_ref_allowed_when_file_read_allowed(tmp_path, monkeypatch):
    """Tier 1: falsify direction — with read allowed (default CWD zone) the same
    present(data_ref) presents ok, so the denial above is a real gate, not a blanket
    refusal. Also the 'minimal call = present(data_ref)' tiered-schema path."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data.json").write_text(json.dumps({"rows": [1, 2]}))
    allow = _resolver(tmp_path)
    ctx = _tool_ctx(tmp_path, allow)
    result = _run(_handle_present({"data_ref": "data.json"}, ctx))
    assert result["status"] == "ok"
    assert result["mode"] == "default"


# ── tiered schema: both view AND blueprint → reject ──────────────────────────


def test_present_tool_rejects_both_view_and_blueprint():
    """Tier 1: the IR op validator's 'at most one of view / blueprint' XOR is surfaced
    (not re-implemented) — passing both is a clean error, not a crash."""
    ctx = _tool_ctx(Path("/tmp"), None)
    result = _run(_handle_present(
        {"data_inline": {"x": 1}, "view": "some_view", "blueprint": {"kind": "table"}},
        ctx,
    ))
    assert result["status"] == "error"


def test_render_template_tool_rejects_both_template_and_template_ref():
    """Tier 1: render_template's 'exactly one of template / template_ref' XOR is surfaced
    from the IR op validator — passing both is a clean error."""
    ctx = _tool_ctx(Path("/tmp"), None)
    result = _run(_handle_render_template(
        {"template": "x", "template_ref": "t.j2", "data_inline": {"x": 1}}, ctx,
    ))
    assert result["status"] == "error"


# ── FP-0056: both new tools carry a canonical declaration ────────────────────


def test_present_and_render_template_have_canonical_declarations():
    """Tier 1: both tool names resolve to a real (callable) canonical mapper by invoked
    identity (tool name == op kind), keeping the FP-0056 coverage gate green — present
    ships a real text mapper (no longer CANONICAL_TODO)."""
    assert callable(canonical_declaration("present"))
    assert callable(canonical_declaration("render_template"))


def test_present_ack_canonicalizes_to_text_not_structured():
    """Tier 1: a present ack normalizes to a short canonical `text` (agent-facing signal),
    NOT a whole-dict `structured` attachment — guards against the FP-0056 whole-dict gap
    for this producer."""
    ack = {
        "kind": "present", "status": "ok", "ok": True, "mode": "default",
        "bindings_resolved": 2, "bindings_dropped": [], "rows": 3,
        "all_bindings_missed": False,
    }
    canonical = to_canonical(ack, source="present")
    assert canonical["text"]
    assert not canonical["attachments"]
