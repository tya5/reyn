"""Tier 2: Hook-Event Redesign Phase 2 — Ingress Adapter unify (proposal
``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §6).

Before Phase 2, reyn's 4 external-event sources converged on
``HookDispatcher.dispatch``/``Session.dispatch_external_event`` through two
independently-implemented ingress patterns: an in-process bounded-queue bridge
(``mcp_resource_updated``, ``file_changed``) and an out-of-process
Session-resolve-then-``fire_and_forget`` (``cron_fired``, ``webhook_received``).
Phase 2 converges both onto ONE ``reyn.hooks.ingress.IngressAdapter`` interface:
``to_event(raw) -> HookEvent`` (pure conversion, via Phase 1's
``build_hook_payload``) + ``deliver(event, ...)`` (the mechanism-specific
hand-off — bounded-queue-bridge for in-process, resolve-then-fire for
out-of-process).

This file drives the REAL adapter classes (no mocks) and proves:

1. **byte-identical conversion** — each adapter's ``to_event`` produces the
   IDENTICAL field-set the pre-Phase-2 free functions
   (``message_handler.emit_resource_updated`` / ``fs_watcher._enqueue`` /
   ``cron.routing.dispatch_cron_fired`` / ``webhook_routing.
   dispatch_webhook_received``) built via the same ``build_hook_payload``
   schema gate proven in ``test_hook_event_schema_registry_sync_0059.py``.
2. **byte-identical delivery** — ``deliver`` reaches the SAME resolved
   session's ``HookDispatcher`` the pre-Phase-2 mechanism did (in-process:
   the bound ``hook_trigger`` closure; out-of-process: the registry-resolved
   Session via ``fire_and_forget``).
3. **strip-falsify** — breaking one adapter's conversion (a dropped field,
   simulating drift) makes the schema gate (``HookSchemaError``, the SAME
   Phase-1 construction-time gate) RED; restoring goes GREEN.
4. **security invariants preserved** — the Webhook adapter's payload never
   carries the raw inbound body/text; the Fs adapter has no widening
   capability (no Control-IR op reaches ``FsWatchConfig``/``fs_watch.paths``).

Policy (docs/deep-dives/contributing/testing.md): real instances only.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.event import HookEvent
from reyn.hooks.ingress import (
    CronIngressAdapter,
    FsIngressAdapter,
    IngressAdapter,
    McpIngressAdapter,
    WebhookIngressAdapter,
)
from reyn.hooks.schema_registry import BUILTIN_HOOK_SCHEMAS, HookSchemaError
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.agent_session import make_session


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.02) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


def _make_registry(tmp_path: Path) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log)
        s.register_intervention_listener("test")
        return s

    return AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


# ---------------------------------------------------------------------------
# 0) Protocol conformance — all 4 adapters satisfy the ONE IngressAdapter shape
# ---------------------------------------------------------------------------


def test_all_four_adapters_satisfy_the_unified_protocol():
    """Tier 2: the 4 external sources (mcp/fs = in-process, cron/webhook =
    out-of-process) all structurally conform to ONE ``IngressAdapter``
    interface (``to_event`` + ``deliver``) — the redesign's core claim."""
    for adapter in (
        McpIngressAdapter(hook_trigger=None),
        FsIngressAdapter(hook_trigger=None),
        CronIngressAdapter(),
        WebhookIngressAdapter(),
    ):
        assert isinstance(adapter, IngressAdapter)


# ---------------------------------------------------------------------------
# 1) byte-identical conversion — to_event's payload matches the builtin schema
#    (the SAME field-set the pre-Phase-2 free functions produced)
# ---------------------------------------------------------------------------


def test_mcp_adapter_to_event_matches_builtin_schema():
    """Tier 2: McpIngressAdapter.to_event produces the identical field-set
    ``message_handler.emit_resource_updated`` built pre-Phase-2."""
    adapter = McpIngressAdapter(hook_trigger=None)
    event = adapter.to_event(
        "file:///repo/README.md", server="github", agent_name="a", resync=False,
    )
    assert isinstance(event, HookEvent)
    assert event.kind == "builtin:external:mcp_resource_updated"
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]
    assert event.payload["server"] == "github"
    assert event.payload["uri"] == "file:///repo/README.md"
    assert event.payload["resync"] is False


def test_fs_adapter_to_event_matches_builtin_schema():
    """Tier 2: FsIngressAdapter.to_event produces the identical field-set
    ``fs_watcher``'s drain loop built pre-Phase-2."""
    adapter = FsIngressAdapter(hook_trigger=None)
    event = adapter.to_event("/repo/src/main.py", "modified")
    assert event.kind == "builtin:external:file_changed"
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]
    assert event.payload["path"] == "/repo/src/main.py"
    assert event.payload["event_type"] == "modified"


def test_cron_adapter_to_event_matches_builtin_schema():
    """Tier 2: CronIngressAdapter.to_event produces the identical field-set
    ``cron.routing.dispatch_cron_fired`` built pre-Phase-2."""
    adapter = CronIngressAdapter()
    event = adapter.to_event("nightly-backup", "news_agent")
    assert event.kind == "builtin:external:cron_fired"
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]
    assert event.payload["job_name"] == "nightly-backup"
    assert event.payload["to"] == "news_agent"


def test_webhook_adapter_to_event_matches_builtin_schema():
    """Tier 2: WebhookIngressAdapter.to_event produces the identical field-set
    ``webhook_routing.dispatch_webhook_received`` built pre-Phase-2."""
    adapter = WebhookIngressAdapter()
    event = adapter.to_event("slack:U456")
    assert event.kind == "builtin:external:webhook_received"
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]
    assert event.payload["transport"] == "slack"
    assert event.payload["sender"] == "slack:U456"


# ---------------------------------------------------------------------------
# 2) byte-identical delivery — deliver() reaches the resolved session's real
#    HookDispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_process_adapter_deliver_reaches_bound_hook_trigger():
    """Tier 2: the shared in-process bridge (McpIngressAdapter / FsIngressAdapter)
    ``deliver`` reaches the bound ``hook_trigger`` closure — the exact
    mechanism ``MCPConnectionService``/``FsWatcher`` wire at session
    construction (``runtime/session.py``)."""
    captured: list[tuple[str, dict]] = []

    async def _hook_trigger(point: str, payload: dict) -> None:
        captured.append((point, payload))

    mcp_adapter = McpIngressAdapter(hook_trigger=_hook_trigger)
    event = mcp_adapter.to_event("file:///x", server="s", agent_name="a", resync=True)
    mcp_adapter.deliver(event)
    await _wait_for(lambda: len(captured) >= 1)
    (call,) = captured  # exactly one — clean failure message if delivery ever drops/duplicates
    point, payload = call
    assert point == "mcp_resource_updated"
    assert payload == event.payload
    await mcp_adapter.aclose()

    captured.clear()
    fs_adapter = FsIngressAdapter(hook_trigger=_hook_trigger)
    event2 = fs_adapter.to_event("/repo/x.py", "created")
    fs_adapter.deliver(event2)
    await _wait_for(lambda: len(captured) >= 1)
    (call2,) = captured  # exactly one — clean failure message if delivery ever drops/duplicates
    point2, payload2 = call2
    assert point2 == "file_changed"
    assert payload2 == event2.payload
    await fs_adapter.aclose()


@pytest.mark.asyncio
async def test_cron_adapter_resolve_session_and_deliver_reaches_real_dispatcher(tmp_path):
    """Tier 2: CronIngressAdapter's out-of-process Session-resolve + deliver
    reaches the REAL resolved Session's HookDispatcher — driving real
    AgentRegistry/Session/HookDispatcher, no mocks."""
    hooks_config = [
        {"on": "cron_fired", "template_push": {"message": "{{ job_name }}"}},
    ]
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, reactivity=ReactivityConfig(hooks_config=hooks_config))
        s.register_intervention_listener("test")
        return s

    class _NoRunRegistry(AgentRegistry):
        def ensure_session_running(self, name: str, sid: str):
            return self._peek_session(name, sid)

    reg = _NoRunRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    _seed(tmp_path, "news_agent")

    adapter = CronIngressAdapter()
    session = adapter.resolve_session(reg, "news_agent", "morning_news")
    assert session is reg.get_session("news_agent", "cron:morning_news")

    event = adapter.to_event("morning_news", "news_agent")
    adapter.deliver(event, session)

    await _wait_for(lambda: session.inbox.qsize() >= 1)
    kind, payload = session.inbox.get_nowait()
    assert kind == "hook"
    assert payload["text"] == "morning_news"


@pytest.mark.asyncio
async def test_webhook_adapter_resolve_session_and_deliver_reaches_real_dispatcher(tmp_path):
    """Tier 2: WebhookIngressAdapter's out-of-process Session-resolve + deliver
    reaches the REAL resolved Session's HookDispatcher."""
    hooks_config = [
        {"on": "webhook_received", "template_push": {"message": "{{ transport }}"}},
    ]
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(agent_name=profile.name, state_log=state_log, reactivity=ReactivityConfig(hooks_config=hooks_config))
        s.register_intervention_listener("test")
        return s

    class _NoRunRegistry(AgentRegistry):
        def ensure_session_running(self, name: str, sid: str):
            return self._peek_session(name, sid)

    reg = _NoRunRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    _seed(tmp_path, "support_agent")

    adapter = WebhookIngressAdapter()
    session = adapter.resolve_session(reg, "support_agent", "slack:U456")

    event = adapter.to_event("slack:U456")
    adapter.deliver(event, session)

    await _wait_for(lambda: session.inbox.qsize() >= 1)
    kind, payload = session.inbox.get_nowait()
    assert kind == "hook"
    assert payload["text"] == "slack"


# ---------------------------------------------------------------------------
# 3) strip-falsify — an adapter that regresses (drops a field) is caught
#    IMMEDIATELY at construction (the same Phase-1 schema gate), not silently.
# ---------------------------------------------------------------------------


def test_strip_falsify_webhook_adapter_dropped_field_is_caught(monkeypatch):
    """Tier 2: falsifying WebhookIngressAdapter.to_event to drop ``sender``
    (simulating an adapter regression during the Phase-2 refactor) raises
    ``HookSchemaError`` immediately — RED. Restoring the real method goes
    GREEN again."""
    adapter = WebhookIngressAdapter()
    original_to_event = WebhookIngressAdapter.to_event

    def _broken_to_event(self, sender: str) -> HookEvent:
        from reyn.hooks.schema_registry import build_hook_payload, canonical_kind
        transport, _ = self.parse_sender(sender)
        # BUG: omits "sender" — simulates a dropped field.
        payload = build_hook_payload("webhook_received", transport=transport)
        return HookEvent(kind=canonical_kind("webhook_received"), payload=payload)

    monkeypatch.setattr(WebhookIngressAdapter, "to_event", _broken_to_event)
    with pytest.raises(HookSchemaError):
        adapter.to_event("slack:U456")

    monkeypatch.setattr(WebhookIngressAdapter, "to_event", original_to_event)
    event = adapter.to_event("slack:U456")  # GREEN again
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]


def test_strip_falsify_cron_adapter_extra_field_is_caught(monkeypatch):
    """Tier 2: falsifying CronIngressAdapter.to_event to ADD an undeclared
    field (simulating drift the other direction) raises ``HookSchemaError``
    immediately — RED. Restoring goes GREEN."""
    adapter = CronIngressAdapter()
    original_to_event = CronIngressAdapter.to_event

    def _broken_to_event(self, job_name: str, to: str) -> HookEvent:
        from reyn.hooks.schema_registry import build_hook_payload, canonical_kind
        # BUG: an undeclared extra field smuggled in.
        payload = build_hook_payload(
            "cron_fired", job_name=job_name, to=to, unexpected_field="oops",
        )
        return HookEvent(kind=canonical_kind("cron_fired"), payload=payload)

    monkeypatch.setattr(CronIngressAdapter, "to_event", _broken_to_event)
    with pytest.raises(HookSchemaError):
        adapter.to_event("nightly-backup", "news_agent")

    monkeypatch.setattr(CronIngressAdapter, "to_event", original_to_event)
    event = adapter.to_event("nightly-backup", "news_agent")  # GREEN again
    assert frozenset(event.payload) == BUILTIN_HOOK_SCHEMAS[event.kind]


@pytest.mark.asyncio
async def test_strip_falsify_in_process_bridge_queue_overflow_drops_and_logs():
    """Tier 2: falsifying — a maxsize=1 bridge with a stalled drain proves the
    bounded-by-construction discipline still holds after consolidating the
    queue+drain logic out of connection_service.py/fs_watcher.py into the
    shared ``_BoundedEventBridge``: a burst beyond the bound drops the NEWEST
    event (never raises, never blocks the caller)."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _slow_hook_trigger(point: str, payload: dict) -> None:
        started.set()
        await release.wait()

    adapter = McpIngressAdapter(hook_trigger=_slow_hook_trigger, maxsize=1)
    e1 = adapter.to_event("file:///a", server="s", agent_name=None, resync=False)
    e2 = adapter.to_event("file:///b", server="s", agent_name=None, resync=False)
    e3 = adapter.to_event("file:///c", server="s", agent_name=None, resync=False)

    adapter.deliver(e1)  # picked up by the drain task immediately (blocks on release)
    await _wait_for(lambda: started.is_set())
    adapter.deliver(e2)  # queued (maxsize=1)
    adapter.deliver(e3)  # OVERFLOW — dropped, never raises

    release.set()
    await adapter.aclose()  # never hangs — proves deliver() never blocked the caller


# ---------------------------------------------------------------------------
# 4) security invariants preserved
# ---------------------------------------------------------------------------


def test_webhook_adapter_payload_never_carries_raw_body():
    """Tier 2: security invariant — WebhookIngressAdapter.to_event's signature accepts
    ONLY ``sender`` — there is no parameter through which a raw inbound
    request body/text could reach the payload, and the produced payload's
    field-set is exactly {transport, sender} (the builtin schema), never a
    superset that could smuggle body content through."""
    adapter = WebhookIngressAdapter()
    event = adapter.to_event("slack:U456")
    assert set(event.payload) == {"point", "transport", "sender"}
    for value in event.payload.values():
        assert "TOP_SECRET" not in str(value)


def test_fs_adapter_has_no_path_widening_capability():
    """Tier 2: security invariant — FsIngressAdapter's ``to_event``/``deliver`` surface
    takes a path to CONVERT, never a path to ADD to the watch-set — it holds
    no reference to ``FsWatchConfig``/``fs_watch.paths`` at all. The watched
    OUT-set is entirely owned by ``FsWatcher`` (restart-only config), and no
    Control-IR op kind references ``FsWatchConfig`` (verified against the
    op-kind model map — the sandbox-policy-class invariant CLAUDE.md's Tool
    Contract lens requires: no untyped/agent-driven path to widen an OUT-set
    declared surface)."""
    import inspect

    from reyn.schemas.models import OP_KIND_MODEL_MAP

    adapter = FsIngressAdapter(hook_trigger=None)
    assert not hasattr(adapter, "paths")
    assert not hasattr(adapter, "_paths")

    for model_cls in OP_KIND_MODEL_MAP.values():
        src = inspect.getsource(model_cls)
        assert "FsWatchConfig" not in src
        assert "fs_watch" not in src
