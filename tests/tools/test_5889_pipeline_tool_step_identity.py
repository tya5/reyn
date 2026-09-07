"""Tier 2: OS invariant — #5889 (#5883 co-vet follow-up). The pipeline
driver's tool-step dispatch carries three identity facts on the SAME seam
(``ToolContext`` / ``DispatchContext``, ``pipeline_verbs._make_tool_dispatch``)
that were stale or empty:

1. ``ToolContext.caller_kind`` was hardcoded ``"router"`` at both
   constructors that feed a pipeline ``tool:`` step
   (``PipelineExecutorDriver._make_dispatch`` and the ``reyn pipe`` CLI's
   ``_build_run_tool_context``), while the SAME call's ``DispatchContext``
   (built one frame down, in ``_make_tool_dispatch``) already said
   ``"pipeline"`` (#5865) — one call, two answers. A tool handler reading
   ``ctx.caller_kind`` directly saw the wrong one.
2. ``DispatchContext.chain_id`` was hardcoded ``None`` even though the
   driver's own ``run_turn(user_text, chain_id)`` receives a real chain_id
   per nudge — so a pipeline step's ``tool_called``/``tool_returned``
   events could never join their own run's chain.
3. ``caller_id`` fell back to an empty string when no ``agent_name`` was
   available (the session-less ``reyn pipe run`` CLI path) — an empty
   string carries "this caller has no identity" as if it WERE one.

This file drives the REAL driver (mirrors
``test_pipeline_driver_resource_caller_state_2567.py``'s harness) and the
REAL CLI context builder (mirrors ``tests/mcp/test_cli_pipe.py``'s
``_install_echo_tool`` idiom), never a hand-rolled stand-in for either.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import reyn.tools as tools_pkg
from reyn.core.events.state_log import StateLog
from reyn.core.pipeline.work_order import PipelineWorkOrder
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.services.pipeline_executor_driver import PipelineExecutorDriver
from reyn.runtime.session import Session
from reyn.runtime.session_params import PresentationWiring
from reyn.tools.types import ToolDefinition, ToolGates
from tests._support.agent_session import make_session

_TOOL_NAME = "p5889_identity_probe"


def _install_probe_tool(monkeypatch, seen: "list[dict]") -> None:
    """A REAL ``ToolDefinition`` whose handler records the ``ctx`` it was
    invoked with — the direct witness that a handler reading
    ``ctx.caller_kind``/``ctx.chain_id`` sees the same values the call's
    own audit trail does (same registration idiom as
    ``test_5865_pipeline_tool_step_dispatch_tool.py``'s ``_install_tool``)."""

    async def _handler(args, ctx):
        seen.append({"caller_kind": ctx.caller_kind, "chain_id": ctx.chain_id})
        return {"ok": True}

    tool = ToolDefinition(
        name=_TOOL_NAME,
        description="#5889 test tool.",
        parameters={"type": "object", "properties": {}},
        gates=ToolGates(router="allow"),
        handler=_handler,
        category="io",
        purity="pure",
    )
    base = tools_pkg.get_default_registry

    def _with_tool():
        registry = base()
        registry.register(tool)
        return registry

    monkeypatch.setattr(tools_pkg, "get_default_registry", _with_tool)


def _events_by_type(sink: "list") -> "dict[str, list[dict]]":
    grouped: "dict[str, list[dict]]" = {}
    for event in sink:
        grouped.setdefault(event.type, []).append(event.data)
    return grouped


def _worker_registry(tmp_path: Path, state_log: "StateLog") -> AgentRegistry:
    """Real AgentRegistry + real Session factory — mirrors
    ``test_pipeline_driver_resource_caller_state_2567.py``'s own
    ``_worker_registry`` helper."""
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer, intervention_bridge=intervention_bridge,
            ),
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("worker")
    return reg


# ---------------------------------------------------------------------------
# 1 & 2. Driver: caller_kind consistency + chain_id propagation.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_driver_tool_context_caller_kind_matches_the_audit_trail(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: the handler's OWN ``ctx.caller_kind`` (what
    ``PipelineExecutorDriver._make_dispatch`` set on the ``ToolContext``)
    equals the ``tool_called`` audit event's ``caller_kind`` (what
    ``_make_tool_dispatch`` set on the ``DispatchContext`` for the SAME
    call) — the "1 call → 2 answers" defect closed.

    Strip (verified): reverting ``_make_dispatch``'s ``caller_kind="pipeline"``
    to ``"router"`` turns this red (the handler-observed value diverges
    from the audit trail's, which stays "pipeline" — ``_make_tool_dispatch``
    hardcodes that side unconditionally)."""
    _install_probe_tool(monkeypatch, seen := [])
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _worker_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    caller_host = caller._router_host  # noqa: SIM118 — seam-test assignment

    work_order = PipelineWorkOrder(
        run_id="run-5889a", pipeline_name="p", pipeline={"steps": [], "description": ""},
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv-5889a",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log)
    driver.bind_session(caller, caller_host)

    sink: "list" = []
    caller_host.events.add_subscriber(sink.append)
    dispatch = await driver._make_dispatch("chain-5889")
    await dispatch(_TOOL_NAME, {})
    await caller_host.events.drain()

    assert seen, "setup: the probe tool's handler never ran"
    (called,) = _events_by_type(sink)["tool_called"]
    assert seen[0]["caller_kind"] == called["caller_kind"] == "pipeline", (
        f"handler saw caller_kind={seen[0]['caller_kind']!r}, audit trail said "
        f"{called['caller_kind']!r} — same call, different answers"
    )


@pytest.mark.asyncio
async def test_driver_tool_context_chain_id_matches_the_run_turns_own_chain(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a pipeline tool-step's ``tool_called``/``tool_returned``
    events carry the SAME ``chain_id`` the driver's own ``run_turn`` nudge
    received — never a hardcoded ``None`` that could never join the run's
    other events.

    Strip (verified): reverting ``_make_dispatch`` to build ``ToolContext``
    with no ``chain_id`` (defaults ``None``) and ``_make_tool_dispatch`` to
    hardcode ``DispatchContext(chain_id=None, ...)`` turns this red — the
    handler-observed and audit-trail chain_id both go back to ``None``."""
    _install_probe_tool(monkeypatch, seen := [])
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _worker_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    caller_host = caller._router_host  # noqa: SIM118 — seam-test assignment

    work_order = PipelineWorkOrder(
        run_id="run-5889b", pipeline_name="p", pipeline={"steps": [], "description": ""},
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv-5889b",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log)
    driver.bind_session(caller, caller_host)

    sink: "list" = []
    caller_host.events.add_subscriber(sink.append)
    dispatch = await driver._make_dispatch("chain-abc-123")
    await dispatch(_TOOL_NAME, {})
    await caller_host.events.drain()

    assert seen, "setup: the probe tool's handler never ran"
    (called,) = _events_by_type(sink)["tool_called"]
    assert seen[0]["chain_id"] == called["chain_id"] == "chain-abc-123", (
        f"handler saw chain_id={seen[0]['chain_id']!r}, audit trail said "
        f"{called['chain_id']!r} — expected both to be the run_turn nudge's "
        f"own chain_id"
    )


@pytest.mark.asyncio
async def test_a_second_nudge_with_a_different_chain_id_carries_its_own(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: non-vacuity for the chain_id witness above — TWO dispatches
    built from the SAME driver but different nudges carry DIFFERENT
    chain_ids, proving the value threaded through is the caller's
    per-call ``chain_id`` argument, not some fixed value baked in once at
    driver construction."""
    _install_probe_tool(monkeypatch, seen := [])
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    reg = _worker_registry(tmp_path, state_log)
    caller = reg.get_or_load("worker")
    caller_host = caller._router_host  # noqa: SIM118 — seam-test assignment

    work_order = PipelineWorkOrder(
        run_id="run-5889c", pipeline_name="p", pipeline={"steps": [], "description": ""},
        input=None, reply_to_agent="worker", reply_to_sid="main",
        driver_agent="worker", driver_sid="drv-5889c",
    )
    driver = PipelineExecutorDriver(work_order, registry=reg, state_log=state_log)
    driver.bind_session(caller, caller_host)

    dispatch_1 = await driver._make_dispatch("chain-first")
    await dispatch_1(_TOOL_NAME, {})
    dispatch_2 = await driver._make_dispatch("chain-second")
    await dispatch_2(_TOOL_NAME, {})

    assert [s["chain_id"] for s in seen] == ["chain-first", "chain-second"]


# ---------------------------------------------------------------------------
# 3. reyn pipe CLI: caller_id must never be empty.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reyn_pipe_cli_path_never_emits_an_empty_caller_id(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the ``reyn pipe run`` path's own ``ToolContext``
    (``pipe._build_run_tool_context`` — no live session, no ``agent_name``)
    must still produce a non-empty ``caller_id`` on the ``tool_called``
    audit event: the fixed per-CALLER-ROLE literal ``"pipeline_run_cli"``
    (decided on the issue before implementation, #5889), never ``""``.

    Real ``_build_run_tool_context`` (the exact function ``reyn pipe run``
    calls), real ``_make_tool_dispatch``, real dispatch.

    Strip (verified): reverting ``_make_tool_dispatch``'s fallback to
    ``ctx.agent_name or ""`` turns this red — ``caller_id`` comes back
    ``""``."""
    from reyn.interfaces.cli.commands.pipe import _build_run_tool_context
    from reyn.tools.pipeline_verbs import _make_tool_dispatch

    _install_probe_tool(monkeypatch, seen := [])
    monkeypatch.chdir(tmp_path)

    ctx = _build_run_tool_context(tmp_path, None)
    assert ctx.agent_name is None, (
        "setup: the CLI path genuinely has no per-agent identity to give — "
        "if this ever changes, this test's own precondition is stale"
    )
    assert ctx.caller_kind == "pipeline", (
        "setup/regression: the CLI's ToolContext must also carry the fixed "
        "caller_kind (#5889 item 1) — was 'router'"
    )

    sink: "list" = []
    ctx.events.add_subscriber(sink.append)
    dispatch = _make_tool_dispatch(ctx)
    await dispatch(_TOOL_NAME, {})
    await ctx.events.drain()

    (called,) = _events_by_type(sink)["tool_called"]
    assert called["caller_id"] == "pipeline_run_cli", (
        f"expected the fixed per-CALLER-ROLE literal, got {called['caller_id']!r} "
        "(never an empty string — see #5889's issue-recorded decision)"
    )
    assert called["caller_id"] != "", "caller_id must never be empty"


# ---------------------------------------------------------------------------
# 4 (architect addition). canonical.py's error-message extractor must not
# repr a nested {"kind", "message"} dict.
# ---------------------------------------------------------------------------


def test_a_nested_error_dict_extracts_its_message_not_a_repr() -> None:
    """Tier 1: ``run_pipeline``'s own error envelope shape
    (``{"status": "error", "error": {"kind", "message"}}`` — the SAME shape
    ``dispatch_tool``'s own error result produces) must render its
    ``message``, never a Python dict repr.

    Strip (verified): reverting ``_extract_error_message`` to the pre-#5889
    unconditional ``str(value)`` on the ``error``/``error_message`` field
    turns this red — ``text`` becomes the repr string instead of the
    message."""
    from reyn.core.offload.canonical import error_to_canonical

    result = {
        "status": "error",
        "error": {"kind": "boom_kind", "message": "boom message"},
    }
    canonical = error_to_canonical(result)
    assert canonical["text"] == "boom message"
    assert "{" not in canonical["text"], f"a dict repr leaked into text: {canonical['text']!r}"
    # Losslessness (error_to_canonical's own documented guarantee) is
    # unaffected — the whole dict still survives in the attachment.
    assert canonical["attachments"] == [{"kind": "structured", "data": result}]


def test_a_nested_error_dict_with_no_message_falls_back_to_its_own_kind() -> None:
    """Tier 1: architect's exact witness — a nested dict with ``kind`` but
    no ``message`` extracts the bare ``kind`` string (``"nope"``), never a
    repr and never the outer ``f"error: {kind}"`` wrapping (that wrapping
    is reserved for the TOP-LEVEL ``error_kind`` field, a different,
    pre-existing branch this fix does not touch)."""
    from reyn.core.offload.canonical import _extract_error_message

    result = {"status": "error", "error": {"kind": "nope"}}
    assert _extract_error_message(result) == "nope"
    assert "{" not in _extract_error_message(result)


def test_a_nested_error_dict_with_neither_key_falls_through_not_repr(
) -> None:
    """Tier 1: non-vacuity / discriminating control — a dict that has
    NEITHER ``message`` nor ``kind`` is not a message either; the
    extractor must fall through to the next candidate field (here, the
    top-level ``error_kind``) rather than repr the unrecognized dict."""
    from reyn.core.offload.canonical import _extract_error_message

    result = {"status": "error", "error": {"unrelated": "shape"}, "error_kind": "fallback"}
    assert _extract_error_message(result) == "error: fallback"


def test_a_plain_string_error_field_is_unaffected_by_the_dict_branch() -> None:
    """Tier 1: positive control — the pre-existing, non-dict ``error``
    string path is untouched by the new ``isinstance(value, dict)`` branch."""
    from reyn.core.offload.canonical import _extract_error_message

    assert _extract_error_message({"error": "plain string error"}) == "plain string error"
