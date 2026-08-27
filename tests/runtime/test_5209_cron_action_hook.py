"""Tier 2: #5209 — ``action: hook`` cron jobs (a "token 0" periodic check).

Before this, every cron job was message-based: a fire ALWAYS pushed a
message into the target agent's inbox, which ALWAYS starts an LLM turn —
there was no way to build "run a script periodically, wake the agent only
if it found something" without paying an LLM turn (and its accumulated
history) on every tick regardless of outcome (owner constraint, 2026-08-23:
"pull するたびに token 消費するのは問題").

``action: hook`` (default remains ``"message"``, unchanged) closes that gap:
the runner only fires the pre-existing ``cron_fired`` external-event hook on
the job's host session — never pushes a message itself. A ``hooks.yaml``
``on: cron_fired`` entry's own ``push_when`` (already-existing machinery,
#2069/#1800) then decides whether anything happens next; an unsatisfied
``push_when`` costs zero LLM turns.

Real instances only, per the testing policy: no ``unittest.mock`` /
``MagicMock`` / ``AsyncMock`` / ``patch``. Every test drives a real
``CronJob`` through a real ``CronScheduler``-compatible runner (the REAL
``build_default_runner``) against a real ``AgentRegistry`` + real
``Session`` + real ``HookDispatcher``, observing effects on the session's
own (public) inbox and the public ``pending_dispatch_count`` snapshot read
(``reyn.hooks.external_fire`` — #2620's own witness for "has the
fire-and-forget hook dispatch settled", used here instead of a fixed
``sleep`` so a push_when=false negative is proven, not merely unraced by
luck) rather than any private state.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.hooks.external_fire import pending_dispatch_count
from reyn.runtime.cron import CronJob
from reyn.runtime.cron.routing import dispatch_cron_fired, resolve_cron_session
from reyn.runtime.cron.runners import build_default_runner
from reyn.runtime.profile import AgentProfile
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session


class _NoRunAgentRegistry(AgentRegistry):
    """Real AgentRegistry with ``ensure_session_running`` reduced to a peek
    (mirrors ``test_2608_h5_cron_webhook_hooks.py``'s own harness) — only
    booting ``Session.run()``'s background inbox-consumption loop is
    disabled; every other method is the real, unmodified implementation."""

    def ensure_session_running(self, name: str, sid: str):
        return self._peek_session(name, sid)


def _make_registry(tmp_path: Path, *, hooks_config=None) -> AgentRegistry:
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")

    def _factory(profile: AgentProfile) -> Session:
        s = make_session(
            agent_name=profile.name, state_log=state_log,
            reactivity=ReactivityConfig(hooks_config=hooks_config),
        )
        s.register_intervention_listener("test")
        return s

    return _NoRunAgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)


def _seed(tmp_path: Path, name: str) -> None:
    AgentProfile.new(name, role="").save(tmp_path / ".reyn" / "agents" / name)


def _drain(session) -> list[tuple[str, dict]]:
    items = []
    while not session.inbox.empty():
        items.append(session.inbox.get_nowait())
    return items


async def _wait_for(predicate, *, delay: float = 0.02) -> None:
    """Unbounded per the owner's testing policy (docs/deep-dives/contributing/
    testing.md, ## Time) — no test carries a time budget; CI's --timeout=120
    is the blast-radius kill-switch, not a contract."""
    while not predicate():
        await asyncio.sleep(delay)


async def _hook_only_dispatcher(reg, to: str, native_id: str) -> str:
    """Mirrors production's ``reyn.interfaces.web.server``'s
    ``_hook_only_dispatcher`` exactly: resolve the host session, fire
    ``cron_fired`` on it — no inbox push."""
    session = resolve_cron_session(reg, to, native_id)
    dispatch_cron_fired(session, native_id, to, action="hook")
    return "ok"


# ---------------------------------------------------------------------------
# ① the core existence-reason: push_when=false costs ZERO turns
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_hook_with_push_when_false_starts_zero_turns(tmp_path):
    """Tier 2: THE core #5209 proof. A real ``action="hook"`` CronJob fires
    through the REAL runner (``build_default_runner``'s hook_only_dispatcher
    branch); the configured ``on: cron_fired`` hook's own ``push_when``
    template renders false — nothing lands in the session's inbox, so no
    turn is ever triggered. Waits on the PUBLIC ``pending_dispatch_count``
    snapshot (not a fixed sleep) to know the fire-and-forget hook dispatch
    has actually settled before asserting the (negative) outcome."""
    hooks_config = [
        {
            "on": "cron_fired",
            "template_push": {"message": "should never be sent", "push_when": "false"},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "ops_agent")

    runner = build_default_runner(
        hook_only_dispatcher=lambda to, native_id: _hook_only_dispatcher(reg, to, native_id),
    )
    job = CronJob(name="poll_deploy", schedule="*/5 * * * *", to="ops_agent", action="hook")

    result = await runner(job)
    assert result == "ok"

    session = reg.get_session("ops_agent", "cron:poll_deploy")
    await _wait_for(lambda: pending_dispatch_count(session) == 0)
    assert _drain(session) == [], (
        "push_when=false must cost ZERO turns — nothing may land in the "
        "inbox, the #5209 existence reason"
    )


# ---------------------------------------------------------------------------
# ② push_when=true — the mechanism is alive, a turn IS triggered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_hook_with_push_when_true_starts_a_turn(tmp_path):
    """Tier 2: the positive control for ① — the SAME action="hook" fire, the
    SAME runner, only the hook's push_when differs (true). Something DOES
    land in the inbox — proves ① is a real gate finding zero, not the
    mechanism itself being dead."""
    hooks_config = [
        {
            "on": "cron_fired",
            "template_push": {"message": "deploy status changed", "push_when": "true", "wake": True},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "ops_agent")

    runner = build_default_runner(
        hook_only_dispatcher=lambda to, native_id: _hook_only_dispatcher(reg, to, native_id),
    )
    job = CronJob(name="poll_deploy", schedule="*/5 * * * *", to="ops_agent", action="hook")

    result = await runner(job)
    assert result == "ok"

    session = reg.get_session("ops_agent", "cron:poll_deploy")
    await _wait_for(lambda: session.inbox.qsize() >= 1)
    (only_item,) = _drain(session)
    kind, payload = only_item
    assert kind == TurnOrigin.HOOK  # the hook's own push, not a cron-direct message
    assert payload["text"] == "deploy status changed"


# ---------------------------------------------------------------------------
# ③ regression — action="message" (default) is byte-identical to pre-#5209
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_message_default_regression_unchanged(tmp_path):
    """Tier 2: a plain CronJob() (action defaults to "message") still pushes
    the job's own message via inbox_pusher, unaffected by the new
    hook_only_dispatcher branch — #5209 must not alter the pre-existing path."""
    reg = _make_registry(tmp_path, hooks_config=None)
    _seed(tmp_path, "news_agent")

    async def _inbox_pusher(to: str, envelope: dict, native_id: str) -> str:
        session = resolve_cron_session(reg, to, native_id)
        dispatch_cron_fired(session, native_id, to)  # action defaults to "message"
        await session._put_inbox(TurnOrigin.CRON, envelope)
        return "ok"

    runner = build_default_runner(inbox_pusher=_inbox_pusher)
    job = CronJob(name="morning_news", schedule="0 9 * * *", to="news_agent", message="hi")

    result = await runner(job)
    assert result == "ok"

    session = reg.get_session("news_agent", "cron:morning_news")
    await _wait_for(lambda: session.inbox.qsize() >= 1)
    (only_item,) = _drain(session)
    kind, payload = only_item
    assert kind == TurnOrigin.CRON
    assert payload["text"] == "hi"


# ---------------------------------------------------------------------------
# no hook_only_dispatcher injected — action=hook jobs fail loud, not silent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_hook_without_dispatcher_configured_returns_error() -> None:
    """Tier 2: mirrors the pre-existing "no inbox_pusher" contract for
    message jobs (CLI standalone `reyn cron run`, no session registry) —
    an action="hook" job with no hook_only_dispatcher returns "error", not
    a silent no-op."""
    runner = build_default_runner()  # neither collaborator injected
    job = CronJob(name="poll_deploy", schedule="*/5 * * * *", to="ops_agent", action="hook")
    result = await runner(job)
    assert result == "error"


# ---------------------------------------------------------------------------
# the "action" field is threaded onto the real cron_fired hook payload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_fired_payload_carries_action_hook(tmp_path):
    """Tier 2: a hooks.yaml ``matcher: {action: "hook"}`` entry — new in
    #5209's schema change — fires for an action="hook" job. Proves the
    ``action`` field actually reaches the real dispatched HookEvent's
    template_vars, not just the schema declaration."""
    hooks_config = [
        {
            "on": "cron_fired",
            "matcher": {"action": "hook"},
            "template_push": {"message": "hook-only fire seen", "push_when": "true", "wake": True},
        },
    ]
    reg = _make_registry(tmp_path, hooks_config=hooks_config)
    _seed(tmp_path, "ops_agent")

    runner = build_default_runner(
        hook_only_dispatcher=lambda to, native_id: _hook_only_dispatcher(reg, to, native_id),
    )
    job = CronJob(name="poll_deploy", schedule="*/5 * * * *", to="ops_agent", action="hook")
    await runner(job)

    session = reg.get_session("ops_agent", "cron:poll_deploy")
    await _wait_for(lambda: session.inbox.qsize() >= 1)
    (only_item,) = _drain(session)
    _kind, payload = only_item
    assert payload["text"] == "hook-only fire seen"
