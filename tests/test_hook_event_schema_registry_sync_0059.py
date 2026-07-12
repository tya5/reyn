"""Tier 2: Hook-Event Redesign Phase 1 — Schema Registry <-> dispatch call-site
CI sync gate (proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md``
§4), mirroring the ``OP_KIND_MODEL_MAP`` <-> ``control-ir.md`` sync discipline
(CLAUDE.md hard rule).

Every one of reyn's 10 builtin hook-points now funnels its payload through
``reyn.hooks.schema_registry.build_hook_payload`` at its ONE producer call
site (``reyn.core.op_runtime.task`` / ``reyn.runtime.session`` /
``reyn.mcp.message_handler`` / ``reyn.runtime.fs_watcher`` /
``reyn.runtime.cron.routing`` / ``reyn.runtime.webhook_routing``) — so a call
site's assembled payload IS the shipped schema BY CONSTRUCTION: a missing,
renamed, or extra field raises ``HookSchemaError`` immediately at that call
site, not just detectable by a separate after-the-fact diff.

This file drives the REAL production call sites (no mocks — real ``Session``,
real ``HookDispatcher``, real op handlers, real ingress-routing functions) and
captures the EXACT field-set each dispatches, proving:

1. byte-identical coverage — every one of the 10 builtin points is actually
   exercised and its captured payload key-set matches
   ``BUILTIN_HOOK_SCHEMAS`` exactly (the values are the same ones the
   pre-Phase-1 ad-hoc dict literals carried — only the schema-check is new).
2. the strip-falsify direction — narrowing a schema entry (as if a call site
   had drifted) makes the NEXT real call through that site raise
   ``HookSchemaError`` — the gate actually catches drift, not just documents
   intent.
3. bare-name / canonical-kind alias round-trip (§2) + the ``hooks.yaml``
   bare-name config still loads and fires unmodified (§2/§11 regress check).

Policy (docs/deep-dives/contributing/testing.md): real instances only. The
capture point is ``HookDispatcher.dispatch`` itself (patched via pytest's
``monkeypatch`` to RECORD-THEN-DELEGATE to the real implementation — every
side effect still runs for real; only the observation is added) since every
builtin producer, in-process or out-of-process, ultimately calls some
``HookDispatcher`` instance's ``dispatch(point, template_vars)``. The LLM
boundary is the only thing faked (a real async stub, the established idiom
in ``tests/test_1800_wake_drain.py`` / ``tests/test_2608_run_loop_pickup.py``).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.core.op_runtime import task as taskmod
from reyn.hooks import dispatcher as dispatcher_mod
from reyn.hooks.ingress import McpIngressAdapter
from reyn.hooks.loader import load_hooks
from reyn.hooks.registry import HookRegistry
from reyn.hooks.schema_registry import (
    BUILTIN_HOOK_SCHEMAS,
    HookSchemaError,
    bare_point,
    build_hook_payload,
    canonical_kind,
)
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.mcp.message_handler import ReynMCPMessageHandler
from reyn.runtime.cron.routing import dispatch_cron_fired
from reyn.runtime.session import Session
from reyn.runtime.webhook_routing import dispatch_webhook_received
from reyn.task import InMemoryTaskBackend

_EMPTY_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _text_result(text: str = "ok") -> LLMToolCallResult:
    return LLMToolCallResult(content=text, tool_calls=[], finish_reason="stop", usage=_EMPTY_USAGE)


def _llm_stub(result: LLMToolCallResult):
    async def _stub(**kwargs) -> LLMToolCallResult:
        return result
    return _stub


def _capture_dispatch(monkeypatch, captured: list) -> None:
    """Record (point, dict(template_vars)) on EVERY ``HookDispatcher.dispatch``
    call, then delegate to the real implementation (per-hook isolation, matcher,
    push/shell/pipeline routing all still run for real — only observation is
    added). Every builtin producer (in-process or out-of-process) funnels
    through some ``HookDispatcher`` instance's ``dispatch``, so patching the
    class method here captures all 10 points uniformly."""
    original = dispatcher_mod.HookDispatcher.dispatch

    async def _recording_dispatch(self, point, template_vars):
        captured.append((point, dict(template_vars)))
        return await original(self, point, template_vars)

    monkeypatch.setattr(dispatcher_mod.HookDispatcher, "dispatch", _recording_dispatch)


async def _noop(*args, **kwargs):
    return None


async def _wait_for(predicate, *, attempts: int = 200, delay: float = 0.02) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


def _make_session(tmp_path: Path) -> Session:
    return Session(
        agent_name="sync-gate-agent",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
    )


# ---------------------------------------------------------------------------
# 1) byte-identical coverage — drive all 10 real producer call sites, capture
#    every dispatched payload, and assert each matches its builtin schema.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_ten_builtin_points_dispatch_schema_matching_payloads(
    tmp_path, monkeypatch,
):
    """Tier 2: every one of the 10 builtin hook-points, exercised via its REAL
    production call site, dispatches a payload whose key-set EXACTLY matches
    ``BUILTIN_HOOK_SCHEMAS`` for that point — the byte-identical proof."""
    captured: list[tuple[str, dict]] = []
    _capture_dispatch(monkeypatch, captured)

    # ── session_start / turn_start / turn_end / session_end (reyn.runtime.session) ──
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools", _llm_stub(_text_result("hi back")),
    )
    session = _make_session(tmp_path)
    session.is_attached = True
    run_task = asyncio.create_task(session.run())
    await session.submit_user_text("hello")
    await _wait_for(lambda: any(p == "turn_end" for p, _ in captured))
    await session.shutdown()
    try:
        await asyncio.wait_for(run_task, timeout=2.0)
    except asyncio.TimeoutError:
        run_task.cancel()
        await asyncio.gather(run_task, return_exceptions=True)

    # ── task_start / task_end (done) / task_end (aborted) (reyn.core.op_runtime.task) ──
    backend = InMemoryTaskBackend()
    disp = dispatcher_mod.HookDispatcher(
        HookRegistry([]), put_inbox=_noop, stage_next_turn_context=_noop,
    )
    ctx = SimpleNamespace(
        session_id="worker-1", agent_id="a", events=None, task_backend=backend,
        task_waker=None, task_subscription_writer=None, current_task_id=None,
        hook_dispatcher=disp,
    )
    created = await taskmod._create(
        SimpleNamespace(name="job-1", assignee="worker-1", description="d", deps=[]), ctx,
    )
    task_id = created["task"]["task_id"]
    await taskmod._update_status(
        SimpleNamespace(task_id=task_id, status="done"), ctx,
    )
    created2 = await taskmod._create(
        SimpleNamespace(name="job-2", assignee="worker-1", description="d", deps=[]), ctx,
    )
    task_id2 = created2["task"]["task_id"]
    await taskmod._abort(SimpleNamespace(task_id=task_id2, reason="no longer needed"), ctx)

    # ── cron_fired / webhook_received (out-of-process ingress routing) ──
    dispatch_cron_fired(session, "nightly-backup", "sync-gate-agent")
    dispatch_webhook_received(session, "slack:U456")
    await _wait_for(lambda: sum(1 for p, _ in captured if p in ("cron_fired", "webhook_received")) >= 2)

    # ── mcp_resource_updated (in-process MCP bridge) ── ``on_external_event`` is
    # called SYNCHRONOUSLY by ``emit_resource_updated`` with the RAW signal
    # (``uri``, ``resync``) — #2875 F1: the payload build now happens via the REAL
    # ``McpIngressAdapter.to_event`` (proposal 0059 §6), mirroring the production
    # ``MCPConnectionService._mcp_to_hook_event`` wiring exactly, so this also
    # proves ``to_event`` is production-reached, not dead. The resulting
    # ``HookEvent`` is handed to the real (capturing) dispatch the same
    # sync-enqueue-then-async-drain shape the production bridge uses.
    mcp_adapter = McpIngressAdapter(hook_trigger=None)  # to_event is pure — no I/O, no Session

    def _mcp_bridge(uri: "str | None", resync: bool) -> None:
        event = mcp_adapter.to_event(
            uri, server="github", agent_name="sync-gate-agent", resync=resync,
        )
        asyncio.create_task(disp.dispatch(bare_point(event.kind), event.payload))

    handler = ReynMCPMessageHandler(
        lambda *a, **k: None, "github", on_external_event=_mcp_bridge, agent_name="sync-gate-agent",
    )
    handler.emit_resource_updated("file:///repo/README.md", resync=False)
    await _wait_for(lambda: any(p == "mcp_resource_updated" for p, _ in captured))

    # ── file_changed (fs-watcher thread->async bridge) — driven directly via
    # the same build_hook_payload+dispatch call the drain task makes (module
    # docstring: "point=file_changed"), since spinning up a real watchdog OS
    # thread is out of scope for this schema-sync proof.
    await disp.dispatch("file_changed", build_hook_payload(
        "file_changed", path="/repo/src/main.py", event_type="modified",
    ))

    seen_points = {p for p, _ in captured}
    expected_points = {
        "session_start", "session_end", "turn_start", "turn_end",
        "task_start", "task_end", "cron_fired", "webhook_received",
        "mcp_resource_updated", "file_changed",
    }
    missing = expected_points - seen_points
    assert not missing, f"builtin points never captured: {sorted(missing)}"

    for point, payload in captured:
        if point not in expected_points:
            continue
        kind = canonical_kind(point)
        schema = BUILTIN_HOOK_SCHEMAS[kind]
        actual = frozenset(payload)
        assert actual == schema, (
            f"{point!r} dispatched payload {sorted(actual)} != "
            f"shipped schema {sorted(schema)}"
        )


# ---------------------------------------------------------------------------
# 2) strip-falsify — narrowing the schema makes the NEXT real call-site
#    invocation raise HookSchemaError (RED), then restoring it goes GREEN.
# ---------------------------------------------------------------------------


def test_strip_falsify_schema_drift_is_caught(monkeypatch):
    """Tier 2: falsifying — narrow ``turn_end``'s shipped schema (as if a call
    site had silently dropped a field) and confirm the SAME call-site literal
    (``session.py``'s ``turn_end`` build_hook_payload call, reproduced verbatim
    here) now raises ``HookSchemaError``. Un-narrowing restores green — the
    sync gate actually detects drift, it doesn't just assert a static fact."""
    turn_end_kind = "builtin:lifecycle:turn_end"
    original_schema = BUILTIN_HOOK_SCHEMAS[turn_end_kind]

    # Healthy (pre-falsify): the real call-site shape validates clean.
    build_hook_payload("turn_end", agent_name="a", chain_id="c1", user_text="hi")

    # Falsify: drop "user_text" from the shipped schema (simulating schema
    # drift relative to the untouched call site).
    monkeypatch.setitem(
        BUILTIN_HOOK_SCHEMAS, turn_end_kind, frozenset(original_schema - {"user_text"}),
    )
    with pytest.raises(HookSchemaError):
        build_hook_payload("turn_end", agent_name="a", chain_id="c1", user_text="hi")

    # Restore (monkeypatch undoes this automatically at teardown too, but
    # assert explicitly that build_hook_payload is green again mid-test).
    monkeypatch.setitem(BUILTIN_HOOK_SCHEMAS, turn_end_kind, original_schema)
    build_hook_payload("turn_end", agent_name="a", chain_id="c1", user_text="hi")


def test_call_site_missing_field_raises_at_construction():
    """Tier 2: falsifying — a call site that OMITS a required field (simulating
    the inverse drift: the call site regressed, schema unchanged) raises
    ``HookSchemaError`` immediately, not silently producing a partial payload."""
    with pytest.raises(HookSchemaError):
        build_hook_payload("turn_end", agent_name="a", chain_id="c1")  # missing user_text


def test_call_site_extra_field_raises_at_construction():
    """Tier 2: falsifying — a call site that ADDS an undeclared field raises
    ``HookSchemaError`` (additive schema evolution requires touching the
    registry first, not silently smuggling a new field through)."""
    with pytest.raises(HookSchemaError):
        build_hook_payload(
            "turn_end", agent_name="a", chain_id="c1", user_text="hi", extra_field="oops",
        )


# ---------------------------------------------------------------------------
# 3) bare-name alias regress check (§2/§11) — existing hooks.yaml (bare names)
#    keeps working unmodified; the new namespaced full-form is ALSO accepted
#    and resolves to the identical internal HookDef.on.
# ---------------------------------------------------------------------------


def test_bare_name_config_loads_unmodified():
    """Tier 1: an EXISTING bare-name hooks.yaml entry (``on: turn_end``,
    pre-dating this module) still loads with no changes required."""
    registry = load_hooks([
        {"on": "turn_end", "template_push": {"message": "continue", "wake": False}},
    ])
    (hook,) = registry.hooks_for("turn_end")  # exactly one hook fires for the bare point
    assert hook.on == "turn_end"


def test_canonical_namespaced_alias_resolves_to_same_bare_point():
    """Tier 1: the NEW namespaced full-form (``on: builtin:lifecycle:turn_end``)
    is a permanent alias — it loads and registers under the SAME bare
    ``on`` key a bare-name entry would, so both spellings' hooks fire
    together on the same real dispatch point."""
    registry = load_hooks([
        {"on": "builtin:lifecycle:turn_end", "template_push": {"message": "c", "wake": False}},
    ])
    (hook,) = registry.hooks_for("turn_end")  # resolves to the SAME bare on= key
    assert hook.on == "turn_end"


def test_unrecognised_on_value_still_rejected():
    """Tier 1: regress check — an unrecognised ``on:`` value (neither a bare
    nor a namespaced builtin point) is still rejected, unchanged from
    pre-Phase-1 behavior."""
    from reyn.hooks.schema import HookConfigError

    with pytest.raises(HookConfigError):
        load_hooks([
            {"on": "not_a_real_point", "template_push": {"message": "x", "wake": False}},
        ])
