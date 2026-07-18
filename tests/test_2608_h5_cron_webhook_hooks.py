"""Tests for #2608 H5 — the LAST slice of the external-event->hooks arc.

H5 wires cron and webhook ingress to fire ``cron_fired`` / ``webhook_received``
external-event hooks on the resolved session, completing the source set
alongside H1 (``mcp_resource_updated``) and H4 (``file_changed``).

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. Tests drive the REAL production
functions (``resolve_cron_session`` / ``dispatch_cron_fired`` via a real
``CronScheduler``-compatible runner built by the REAL ``build_default_runner``;
``push_to_agent`` for webhook — the single stable ingress every webhook plugin
routes through) against a real ``AgentRegistry`` + real ``Session`` + real
``HookDispatcher``, observing effects on the session's own (public) inbox.

``_NoRunAgentRegistry`` disables ONE side effect of the real ``AgentRegistry``
— booting ``Session.run()``'s background inbox-consumption loop — so the test
can observe the hook's effect on the inbox deterministically instead of racing
a live turn-processing loop that would also need a real (or replayed) LLM to
avoid erroring. Every other registry method (``resolve_session``,
``get_session``, ``exists``, ...) is the unmodified real ``AgentRegistry``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.runtime.cron import CronJob
from reyn.runtime.cron.routing import dispatch_cron_fired, resolve_cron_session
from reyn.runtime.cron.runners import build_default_runner
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig


class _NoRunAgentRegistry(AgentRegistry):
    """Real AgentRegistry with ``ensure_session_running`` reduced to a peek —
    see module docstring. Only this one side effect (spawning the run() task)
    is disabled; every other method is the real, unmodified implementation."""

    def ensure_session_running(self, name: str, sid: str):
        return self._peek_session(name, sid)


def _make_registry(tmp_path: Path, *, hooks_config=None) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = Session(agent_name=profile.name, state_log=state_log, reactivity=ReactivityConfig(hooks_config=hooks_config))
        s.register_intervention_listener("test")
        return s

    return _NoRunAgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


async def _wait_for(predicate, *, attempts: int = 100, delay: float = 0.02) -> None:
    """Poll ``predicate()`` until True or give up — the hook fires on a
    background task (``fire_and_forget``), not synchronously with the
    triggering call (mirrors H1/H4's own polling helper)."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(delay)


def _drain(session) -> list[tuple[str, dict]]:
    items = []
    while not session.inbox.empty():
        items.append(session.inbox.get_nowait())
    return items


# ---------------------------------------------------------------------------
# Tier 1: schema — the two new hook-points are registered
# ---------------------------------------------------------------------------


def test_cron_fired_and_webhook_received_are_allowed_hook_points():
    """Tier 1: cron_fired/webhook_received join mcp_resource_updated/file_changed
    in ALLOWED_HOOK_POINTS — the schema-level gate a hooks.yaml entry must pass."""
    from reyn.hooks.schema import ALLOWED_HOOK_POINTS

    assert "cron_fired" in ALLOWED_HOOK_POINTS
    assert "webhook_received" in ALLOWED_HOOK_POINTS


def test_cron_and_webhook_hooks_load_via_production_loader():
    """Tier 1: hooks: entries with on: cron_fired / on: webhook_received parse
    through the REAL load_hooks seam into a HookRegistry that serves them back."""
    from reyn.hooks.loader import load_hooks

    raw = [
        {"on": "cron_fired", "template_push": {"message": "job {{ job_name }} fired"}},
        {"on": "webhook_received", "template_push": {"message": "{{ transport }}:{{ sender }}"}},
    ]
    registry = load_hooks(raw)
    (cron_hook,) = registry.hooks_for("cron_fired")
    (webhook_hook,) = registry.hooks_for("webhook_received")
    assert cron_hook.matcher is None
    assert webhook_hook.matcher is None


# ---------------------------------------------------------------------------
# Tier 2: (a) a real cron fire -> cron_fired hook fires with the job_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_cron_fire_dispatches_cron_fired_hook_into_session_inbox(tmp_path):
    """Tier 2: THE core H5 cron proof. A real CronScheduler-compatible runner
    (built by the REAL build_default_runner) whose inbox_pusher mirrors
    production's (``reyn.interfaces.web.server``'s ``_inbox_pusher``:
    resolve_cron_session -> dispatch_cron_fired -> _put_inbox) is driven with a
    real message-based CronJob. The cron_fired hook fires — via the REAL
    HookDispatcher, on the job's OWN resolved session — and lands its
    templated push in that session's public inbox, carrying job_name."""
    hooks_config = [
        {
            "on": "cron_fired",
            "template_push": {"message": "job {{ job_name }} fired for {{ to }}", "wake": True},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "news_agent")

    async def _inbox_pusher(to: str, envelope: dict, native_id: str) -> str:
        session = resolve_cron_session(reg, to, native_id)
        dispatch_cron_fired(session, native_id, to)
        await session._put_inbox("user", envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_inbox_pusher)
    job = CronJob(name="morning_news", schedule="0 9 * * *", to="news_agent", message="hi")

    result = await runner(job)
    assert result == "ok"

    session = reg.get_session("news_agent", "cron:morning_news")
    # Two items land in the inbox: the templated hook push (fire_and_forget
    # background task) and the cron job's own user message.
    await _wait_for(lambda: session.inbox.qsize() >= 2)
    items = _drain(session)
    hook_items = [p for k, p in items if k == "hook"]
    (hook_payload,) = hook_items  # exactly one hook fired — unpack asserts the count
    assert hook_payload["text"] == "job morning_news fired for news_agent"
    assert hook_payload["name"] == "cron_fired"  # no name: set -> defaults to the point


@pytest.mark.asyncio
async def test_cron_matcher_filters_by_job_name_exact(tmp_path):
    """Tier 2: (c) matcher filters — job_name is exact-match (not a glob field)."""
    hooks_config = [
        {
            "on": "cron_fired",
            "matcher": {"job_name": "backup"},
            "template_push": {"message": "backup ran"},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "news_agent")

    async def _inbox_pusher(to: str, envelope: dict, native_id: str) -> str:
        session = resolve_cron_session(reg, to, native_id)
        dispatch_cron_fired(session, native_id, to)
        await session._put_inbox("user", envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_inbox_pusher)
    non_matching_job = CronJob(
        name="morning_news", schedule="0 9 * * *", to="news_agent", message="hi",
    )
    await runner(non_matching_job)

    session = reg.get_session("news_agent", "cron:morning_news")
    await _wait_for(lambda: session.inbox.qsize() >= 1)  # the user message always lands
    await asyncio.sleep(0.1)  # give the (non-matching, no-op) hook dispatch a fair chance
    items = _drain(session)
    hook_items = [p for k, p in items if k == "hook"]
    assert hook_items == []  # matcher named job_name="backup" — this job is "morning_news"


@pytest.mark.asyncio
async def test_cron_empty_registry_leaves_ingress_delivery_unaffected(tmp_path):
    """Tier 2: (d) empty-registry equivalence — no hooks: configured, the cron
    ingress's own delivery (the job's user-message push) is unaffected; the
    hook side is a pure no-op."""
    reg = _make_registry(tmp_path, hooks_config=None)
    _seed(tmp_path, "news_agent")

    async def _inbox_pusher(to: str, envelope: dict, native_id: str) -> str:
        session = resolve_cron_session(reg, to, native_id)
        dispatch_cron_fired(session, native_id, to)
        await session._put_inbox("user", envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_inbox_pusher)
    job = CronJob(name="morning_news", schedule="0 9 * * *", to="news_agent", message="hi")
    result = await runner(job)
    assert result == "ok"

    session = reg.get_session("news_agent", "cron:morning_news")
    await asyncio.sleep(0.1)
    items = _drain(session)
    (only_item,) = items  # exactly one item — unpack asserts the count
    kind, payload = only_item
    assert kind == "user"
    assert payload["text"] == "hi"  # no hook message — pure no-op


# ---------------------------------------------------------------------------
# Tier 2: (b) an inbound webhook -> webhook_received hook fires with
# transport/sender
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_webhook_dispatches_webhook_received_hook_into_session_inbox(tmp_path):
    """Tier 2: THE core H5 webhook proof — drives the REAL production
    push_to_agent (the single stable ingress every webhook plugin routes
    through). The webhook_received hook fires — via the REAL HookDispatcher, on
    the sender's OWN resolved session — carrying transport + sender."""
    from reyn.gateway.api import push_to_agent

    hooks_config = [
        {
            "on": "webhook_received",
            "template_push": {"message": "{{ transport }}:{{ sender }}", "wake": True},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "support_agent")

    await push_to_agent(
        target_agent="support_agent",
        text="hello from slack",
        sender="slack:U456",
        registry=reg,
    )

    session = reg.get_session("support_agent", "slack:U456")
    await _wait_for(lambda: session.inbox.qsize() >= 2)
    items = _drain(session)
    hook_items = [p for k, p in items if k == "hook"]
    (hook_payload,) = hook_items  # exactly one hook fired — unpack asserts the count
    assert hook_payload["text"] == "slack:slack:U456"
    assert hook_payload["name"] == "webhook_received"


@pytest.mark.asyncio
async def test_webhook_matcher_filters_by_transport_exact(tmp_path):
    """Tier 2: (c) matcher filters — transport is exact-match (not a glob field)."""
    from reyn.gateway.api import push_to_agent

    hooks_config = [
        {
            "on": "webhook_received",
            "matcher": {"transport": "line"},
            "template_push": {"message": "line message"},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "support_agent")

    await push_to_agent(
        target_agent="support_agent", text="hi", sender="slack:U456", registry=reg,
    )

    session = reg.get_session("support_agent", "slack:U456")
    await _wait_for(lambda: session.inbox.qsize() >= 1)
    await asyncio.sleep(0.1)
    items = _drain(session)
    hook_items = [p for k, p in items if k == "hook"]
    assert hook_items == []  # matcher named transport="line" — this sender is "slack:..."


@pytest.mark.asyncio
async def test_webhook_empty_registry_leaves_ingress_delivery_unaffected(tmp_path):
    """Tier 2: (d) empty-registry equivalence — no hooks: configured, push_to_agent's
    own delivery is unaffected; the hook side is a pure no-op."""
    from reyn.gateway.api import push_to_agent

    reg = _make_registry(tmp_path, hooks_config=None)
    _seed(tmp_path, "support_agent")

    await push_to_agent(
        target_agent="support_agent", text="hi", sender="slack:U456", registry=reg,
    )

    session = reg.get_session("support_agent", "slack:U456")
    await asyncio.sleep(0.1)
    items = _drain(session)
    (only_item,) = items  # exactly one item — unpack asserts the count
    kind, payload = only_item
    assert kind == "user"
    assert payload["text"] == "hi"  # no hook message — pure no-op


@pytest.mark.asyncio
async def test_webhook_received_template_vars_carry_no_raw_payload_text(tmp_path):
    """Tier 2: (e) no-secret-leak guarantee — webhook_received's template_vars
    carry ONLY transport/sender, never the raw inbound `text` body (which may
    hold tokens/PII). A template referencing `{{ text }}` with a `default`
    fallback renders the fallback, not the pushed body — proving `text` is
    genuinely absent from template_vars (if it had leaked through, this would
    render the secret payload instead)."""
    from reyn.gateway.api import push_to_agent

    hooks_config = [
        {
            "on": "webhook_received",
            "template_push": {"message": "{{ text | default('NO_SECRET') }}"},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "support_agent")

    await push_to_agent(
        target_agent="support_agent",
        text="TOP_SECRET_TOKEN_abc123",
        sender="slack:U456",
        registry=reg,
    )

    session = reg.get_session("support_agent", "slack:U456")
    await _wait_for(lambda: session.inbox.qsize() >= 2)
    items = _drain(session)
    hook_items = [p for k, p in items if k == "hook"]
    (hook_payload,) = hook_items  # exactly one hook fired — unpack asserts the count
    assert hook_payload["text"] == "NO_SECRET"
    assert "TOP_SECRET_TOKEN_abc123" not in hook_payload["text"]
