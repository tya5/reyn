"""Tier 2: /cost, /budget, /session, /reload slash — handler behavioural paths.

These handlers have no pure helper functions — the testable surface is the
dispatch logic: which reply surfaces when the budget tracker is disabled, what
the transport is asked to do on /session switch (#4534 PR-2b —
request_session_switch, not a sentinel), etc.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash.budget import budget_cmd, cost_cmd
from reyn.interfaces.slash.reload import reload_cmd
from reyn.interfaces.slash.session import session_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx

# ── shared stub ────────────────────────────────────────────────────────────


def _ctx(session, *, switch_result: bool = True, session_list_result=None):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now, so the list the assertions read is the
    one the transport fills.

    ``switch_result`` (#5096 ②) / ``session_list_result`` (#5099): what
    ``request_session_switch`` / ``request_session_list`` report back —
    see ``RecordingTransport``'s own docstring.
    """
    return slash_ctx(
        session, recorder=session._outbox, switch_result=switch_result,
        session_list_result=session_list_result,
    )


class _FakeSession:
    #: (#3562) ``/session new`` looks the INVOKING session's own (agent, sid) up to find
    #: the per-session narrowing the child inherits, so the stand-in carries the two
    #: identity fields the real ``Session`` exposes.
    agent_name = "alpha"
    session_id = "main"

    def __init__(
        self,
        *,
        budget=None,
        registry=None,
        hot_reloader=None,
    ) -> None:
        self._budget = budget
        self._registry = registry
        if hot_reloader is not None:
            self._hot_reloader = hot_reloader
        self._outbox: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def reply_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")

    def outbox_kinds(self) -> list[str]:
        return [m.kind for m in self._outbox]


class _FakeBudget:
    def __init__(
        self,
        *,
        cost_line_result: str | None = "1 000 tokens / $0.02",
        budget_full_result: str | None = "Full budget text",
        reset_all_result: dict | None = None,
    ) -> None:
        self._cost_line = cost_line_result
        self._budget_full = budget_full_result
        self._reset_all = reset_all_result

    def cost_line(self) -> str | None:
        return self._cost_line

    def budget_full(self) -> str | None:
        return self._budget_full

    def reset_all(self) -> dict | None:
        return self._reset_all


class _FakeRegistry:
    def __init__(
        self,
        *,
        attached_name: str | None = "alpha",
        attached_sid: str | None = "main",
        session_ids: list[str] | None = None,
        spawn_result: str | None = "s2",
        spawn_raises: Exception | None = None,
        get_session_result: object = object(),
    ) -> None:
        self.attached_name = attached_name
        self.attached_sid = attached_sid
        self._session_ids = session_ids or ["main"]
        self._spawn_result = spawn_result
        self._spawn_raises = spawn_raises
        self._get_session_result = get_session_result

    def per_session_narrowing(self, name: str, sid: "str | None" = None) -> "dict | None":
        """#3562: no per-session capability layer on this stand-in's sessions, so the
        handler's inheritance branch is inert and these tests keep measuring the
        dispatch paths they were written for. ★ The inheritance is measured against real
        instances — by a denied tool's real side effect — in
        ``tests/runtime/test_3562_slash_session_new_narrowing_inheritance.py``, never here: a
        stand-in that answers None can only exercise the empty case."""
        return None

    def spawn_session(
        self, name: str, *, presentation_consumer=None, intervention_bridge=None,
        narrowing: "dict | None" = None,
    ) -> str:
        if self._spawn_raises is not None:
            raise self._spawn_raises
        return self._spawn_result  # type: ignore[return-value]

    def get_session(self, name: str, sid: str) -> object | None:
        return self._get_session_result

    def session_ids(self, name: str) -> list[str]:
        return list(self._session_ids)


class _FakeReloader:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request_reload(self, *, source: str) -> None:
        self.calls.append({"source": source})


# ── /cost ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cost_disabled_replies_tracker_note() -> None:
    """Tier 2: /cost with budget tracker disabled (cost_line returns None) → info note."""
    session = _FakeSession(budget=_FakeBudget(cost_line_result=None))
    await cost_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert "disabled" in session.reply_text()


@pytest.mark.asyncio
async def test_cost_enabled_replies_cost_line() -> None:
    """Tier 2: /cost with active tracker replies with the cost line."""
    session = _FakeSession(budget=_FakeBudget(cost_line_result="42 tok / $0.01"))
    await cost_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert "42 tok / $0.01" in session.reply_text()


# ── /budget ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_budget_full_disabled_replies_tracker_note() -> None:
    """Tier 2: /budget with tracker disabled → info note."""
    session = _FakeSession(budget=_FakeBudget(budget_full_result=None))
    await budget_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert "disabled" in session.reply_text()


@pytest.mark.asyncio
async def test_budget_full_replies_full_text() -> None:
    """Tier 2: /budget with active tracker replies with the full breakdown text."""
    session = _FakeSession(budget=_FakeBudget(budget_full_result="Breakdown here"))
    await budget_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert "Breakdown here" in session.reply_text()


@pytest.mark.asyncio
async def test_budget_reset_disabled_replies_tracker_note() -> None:
    """Tier 2: /budget reset with tracker disabled → info note."""
    session = _FakeSession(budget=_FakeBudget(reset_all_result=None))
    await budget_cmd(_ctx(session), "reset")  # type: ignore[arg-type]
    assert "disabled" in session.reply_text()


@pytest.mark.asyncio
async def test_budget_reset_no_agent_tokens_says_reset() -> None:
    """Tier 2: /budget reset with no per-agent data confirms reset without detail lines."""
    session = _FakeSession(budget=_FakeBudget(reset_all_result={}))
    await budget_cmd(_ctx(session), "reset")  # type: ignore[arg-type]
    text = session.reply_text()
    assert "reset" in text.lower()
    assert "daily" in text.lower()


@pytest.mark.asyncio
async def test_budget_reset_with_agent_tokens_includes_per_agent_lines() -> None:
    """Tier 2: /budget reset with per-agent token data includes per-agent detail lines."""
    before = {"agent_tokens": {"alpha": 1000}, "agent_cost_usd": {"alpha": 0.05}}
    session = _FakeSession(budget=_FakeBudget(reset_all_result=before))
    await budget_cmd(_ctx(session), "reset")  # type: ignore[arg-type]
    text = session.reply_text()
    assert "alpha" in text
    assert "1,000" in text


# ── /session ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_session_no_registry_replies_error() -> None:
    """Tier 2: /session without a registry → error."""
    session = _FakeSession(registry=None)
    await session_cmd(_ctx(session), "new")  # type: ignore[arg-type]
    assert session.error_text()


@pytest.mark.asyncio
async def test_session_no_attached_agent_replies_error() -> None:
    """Tier 2: /session with no agent attached → error."""
    reg = _FakeRegistry(attached_name=None)
    session = _FakeSession(registry=reg)
    await session_cmd(_ctx(session), "new")  # type: ignore[arg-type]
    assert session.error_text()


@pytest.mark.asyncio
async def test_session_new_replies_new_sid() -> None:
    """Tier 2: /session new with successful spawn → reply includes new session id."""
    reg = _FakeRegistry(spawn_result="s2")
    session = _FakeSession(registry=reg)
    await session_cmd(_ctx(session), "new")  # type: ignore[arg-type]
    assert "s2" in session.reply_text()


@pytest.mark.asyncio
async def test_session_new_spawn_error_replies_error() -> None:
    """Tier 2: /session new when spawn raises ValueError → error."""
    reg = _FakeRegistry(spawn_raises=ValueError("dup"))
    session = _FakeSession(registry=reg)
    await session_cmd(_ctx(session), "new")  # type: ignore[arg-type]
    assert session.error_text()


@pytest.mark.asyncio
async def test_session_switch_no_sid_replies_usage() -> None:
    """Tier 2: /session switch (no sid arg) → usage error."""
    session = _FakeSession(registry=_FakeRegistry())
    await session_cmd(_ctx(session), "switch")  # type: ignore[arg-type]
    assert session.error_text()


@pytest.mark.asyncio
async def test_session_switch_unknown_sid_replies_error() -> None:
    """Tier 2: #5096 ② -- /session switch to an unknown sid still replies
    an error naming the sid (see session.py's own switch-branch docstring
    for the known, ruled-on degradation: WHY is deferred to #5099, but the
    error kind and the user-typed sid both survive)."""
    reg = _FakeRegistry(get_session_result=None)
    session = _FakeSession(registry=reg)
    await session_cmd(_ctx(session, switch_result=False), "switch missing")  # type: ignore[arg-type]
    assert session.error_text()
    assert "missing" in session.error_text()


@pytest.mark.asyncio
async def test_session_switch_known_sid_requests_switch() -> None:
    """Tier 2: /session switch to a known sid asks the transport to
    request_session_switch with the target sid (#4534 PR-2b — the retired
    __session_switch_request__ sentinel's named-operation replacement)."""
    reg = _FakeRegistry(get_session_result=object())
    session = _FakeSession(registry=reg)
    ctx = _ctx(session)
    await session_cmd(ctx, "switch s2")  # type: ignore[arg-type]
    assert ctx.transport.session_switch_requests == ["s2"]


@pytest.mark.asyncio
async def test_session_list_no_sessions_replies_note() -> None:
    """Tier 2: #5099 -- /session list with an empty request_session_list()
    result → informational note. #5099: `list` is `connection` locus
    (#5096 ②'s own remainder) — the roster comes from
    `ctx.transport.request_session_list()`, never `ctx.session._registry`
    (which a real handler never even builds for this sub in production)."""
    session = _FakeSession(registry=_FakeRegistry())  # registry unread by "list"
    await session_cmd(_ctx(session, session_list_result=[]), "list")  # type: ignore[arg-type]
    assert session.reply_text()
    assert not session.error_text()


@pytest.mark.asyncio
async def test_session_list_marks_focused_with_star() -> None:
    """Tier 2: #5099 -- /session list marks the currently focused session with
    '*' — sourced from the transport's request_session_list(), not the
    registry (see test_session_list_no_sessions_replies_note's own note)."""
    session = _FakeSession(registry=_FakeRegistry())
    ctx = _ctx(
        session,
        session_list_result=[
            {"sid": "main", "attached": False}, {"sid": "s2", "attached": True},
        ],
    )
    await session_cmd(ctx, "list")  # type: ignore[arg-type]
    text = session.reply_text()
    assert "* s2" in text
    assert "main" in text


@pytest.mark.asyncio
async def test_session_unknown_sub_replies_usage_error() -> None:
    """Tier 2: /session with unrecognised sub-command → usage error."""
    session = _FakeSession(registry=_FakeRegistry())
    await session_cmd(_ctx(session), "frobnicate")  # type: ignore[arg-type]
    assert session.error_text()


# ── /reload ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_no_reloader_replies_error() -> None:
    """Tier 2: /reload when session has no hot-reloader → error reply."""
    session = _FakeSession()
    await reload_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert session.error_text()
    assert "not available" in session.error_text()


@pytest.mark.asyncio
async def test_reload_calls_request_reload_and_replies() -> None:
    """Tier 2: /reload with reloader wired → calls request_reload(source='operator') + success reply."""
    reloader = _FakeReloader()
    session = _FakeSession(hot_reloader=reloader)
    await reload_cmd(_ctx(session), "")  # type: ignore[arg-type]
    assert reloader.calls == [{"source": "operator"}]
    assert session.reply_text()
    assert not session.error_text()
