"""Tier 2: #1726 FP-0043 Stage 4a — the `/session` REPL command (public surface).

`/session new|switch <sid>|list` drives per-agent multi-session in the REPL.
`new` reads the public registry surface directly (attached_name /
spawn_session / per_session_narrowing) via `ctx.session`. `switch` and `list`
are `connection` locus (#5096 ②/#5099) — NEITHER touches `ctx.session` (which
is `None` for them in production); both ask the transport instead:
`request_session_switch` (#4534 PR-2b — a typed request, not the retired
`__session_switch_request__` display-channel sentinel) and
`request_session_list` (#5099 — the same shape, closing `_session_locus`'s own
prior remainder). These tests exercise the command flow + the graceful-error
paths via a stub registry (`new`'s own reads) + a `RecordingTransport` (the
`switch`/`list` typed-op results) + an outbox-capturing fake session — the same
pattern as test_slash_agent. Byte-identical when unused (a session that never
runs `/session` stays single-"main").
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.session import session_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx


def _ctx(session, *, switch_result: bool = True, session_list_result=None):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now (#3595 S4), so the list these assertions
    read is the one the transport fills.

    ``switch_result`` (#5096 ②) / ``session_list_result`` (#5099): what
    ``request_session_switch`` / ``request_session_list`` report back —
    see ``RecordingTransport``'s own docstring.
    """
    return slash_ctx(
        session, recorder=session.outbox_calls, switch_result=switch_result,
        session_list_result=session_list_result,
    )


class _StubRegistry:
    """Stub of the registry surface the /session handler reads/calls.

    #3562: ``per_session_narrowing`` answers None (this stub's sessions carry no
    per-session capability layer), so the handler's inheritance branch is inert here and
    these tests keep measuring what they were written for — the command flow and its
    replies. ★ The inheritance itself is NOT measured against this stub and must not be:
    a stand-in that answers None can only ever exercise the empty case. It is measured
    against real instances, by a denied tool's real side effect, in
    ``tests/runtime/test_3562_slash_session_new_narrowing_inheritance.py``.
    """

    def __init__(self, *, attached_name="default", sids=("main",), focused="main"):
        self._attached_name = attached_name
        self._sids = list(sids)
        self.attached_sid = focused
        self.spawned: list[str] = []

    @property
    def attached_name(self):
        return self._attached_name

    def per_session_narrowing(self, name, sid=None):
        return None

    def spawn_session(
        self, name, *, presentation_consumer=None, intervention_bridge=None,
        narrowing=None,
    ):
        sid = f"s{len(self._sids)}"
        self._sids.append(sid)
        self.spawned.append(name)
        return sid

    def get_session(self, name, sid):
        return object() if sid in self._sids else None

    def session_ids(self, name):
        return list(self._sids)


class _FakeSession:
    #: (#3562) The handler looks its OWN (agent, sid) up to find what to inherit, so the
    #: stand-in carries the two identity fields the real ``Session`` exposes.
    agent_name = "default"
    session_id = "main"

    def __init__(self, registry):
        self._registry = registry
        # reply()/reply_error() and the switch sentinel all route through
        # _put_outbox (reply wraps text in an OutboxMessage), so everything
        # lands here: kind ∈ {"system","error","__session_switch_request__"}.
        self.outbox_calls: list[OutboxMessage] = []

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self.outbox_calls.append(msg)

    def reply_text(self) -> str:
        return "\n".join(m.text for m in self.outbox_calls if m.text)


def test_session_command_registered():
    """Tier 2: #1726 — `/session` is in the slash registry."""
    cmd = REGISTRY.get("session")
    assert cmd is not None
    assert "session" in cmd.summary.lower()


@pytest.mark.asyncio
async def test_session_new_spawns_and_reports_sid():
    """Tier 2: #1726 — `/session new` calls spawn_session and reports the new sid."""
    reg = _StubRegistry()
    s = _FakeSession(reg)
    await session_cmd(_ctx(s), "new")
    assert reg.spawned == ["default"], "spawn_session invoked for the attached agent"
    assert "s1" in s.reply_text(), "new sid surfaced to the user"


@pytest.mark.asyncio
async def test_session_switch_known_requests_switch():
    """Tier 2: #1726/#4534 — `/session switch <known>` asks the transport
    to request_session_switch with the target sid (the retired
    __session_switch_request__ sentinel's named-operation replacement)."""
    reg = _StubRegistry(sids=("main", "s1"))
    s = _FakeSession(reg)
    ctx = _ctx(s)
    await session_cmd(ctx, "switch s1")
    assert ctx.transport.session_switch_requests == ["s1"]


@pytest.mark.asyncio
async def test_session_switch_unknown_is_graceful():
    """Tier 2: #1726/#5096 ② — `/session switch <unknown>` still replies a
    NAMED error (the sid the user typed, and an error kind — never a
    system note) and posts NO sentinel (no crash).

    ⚠️ Known degradation (lead-coder ruling, #5096 issuecomment-
    5379839120, documented in full at session_cmd's own "switch" branch):
    the pre-#5096 branch validated directly against the registry and could
    explain WHY a sid was rejected (partial-prefix unsupported, full
    name/ID required) — locus="connection" (#5096 ②) forbids reading
    ctx.session, so that reasoning is gone until #5099's
    request_session_list ships a connection-locus-safe way to read the
    sid list back. What survives, deliberately, per lead-coder's ruling:
    the reply is still error-kind, and the sid the user themselves typed
    (client-side input, never read FROM the registry) still appears in
    it — a real regression would be losing either of those, not losing
    the WHY."""
    reg = _StubRegistry(sids=("main",))
    s = _FakeSession(reg)
    await session_cmd(_ctx(s, switch_result=False), "switch nope")
    assert not [m for m in s.outbox_calls if m.kind == "__session_switch_request__"]
    err = s.reply_text()
    assert "nope" in err, "user-facing error names the bad sid"
    assert any(m.kind == "error" for m in s.outbox_calls), "replied as an error"


@pytest.mark.asyncio
async def test_session_list_marks_focused():
    """Tier 2: #1726/#5099 — `/session list` lists the agent's sessions with a
    focus marker. #5099: `list` is now `connection` locus (#5096 ②'s own
    remainder) — the roster comes from `ctx.transport.request_session_list()`,
    not `ctx.session._registry` (a real handler never even builds `ctx.session`
    for this sub in production, see `_session_locus`'s own docstring), so this
    test supplies the roster via the transport, mirroring `switch`'s own
    `switch_result` pattern rather than the (now-unread-by-`list`) stub
    registry."""
    s = _FakeSession(_StubRegistry())  # ctx.session is never touched for "list"
    ctx = _ctx(
        s,
        session_list_result=[
            {"sid": "main", "attached": False},
            {"sid": "s1", "attached": True},
        ],
    )
    await session_cmd(ctx, "list")
    assert ctx.transport.session_list_requests == 1
    body = s.reply_text()
    assert "main" in body and "s1" in body
    assert "* s1" in body, "the focused session is marked"


@pytest.mark.asyncio
async def test_session_list_empty_says_so():
    """Tier 2: #5099 — an empty `request_session_list()` result (nothing
    loaded — the ONLY meaning an empty result can carry now that the
    method is `@abstractmethod` with no "unsupported transport" case left,
    architect co-vet issuecomment-5379990811) replies plainly rather than
    fabricating a roster."""
    s = _FakeSession(_StubRegistry())
    await session_cmd(_ctx(s), "list")
    assert "no sessions loaded" in s.reply_text()


@pytest.mark.asyncio
async def test_session_switch_unknown_names_accepted_sids_when_listable():
    """Tier 2: #5099 — restores the pre-#5096 richer guidance: when the
    roster IS answerable (non-empty), an unknown sid's error names every
    accepted sid, entirely client-side, before ever calling
    request_session_switch (never reaching the fallback bool-only path)."""
    s = _FakeSession(_StubRegistry())
    ctx = _ctx(
        s,
        session_list_result=[
            {"sid": "main", "attached": True}, {"sid": "s1", "attached": False},
        ],
    )
    await session_cmd(ctx, "switch nope")
    assert ctx.transport.session_switch_requests == [], (
        "pre-validation must short-circuit before the switch op is even called"
    )
    err = s.reply_text()
    assert "nope" in err
    assert "main" in err and "s1" in err, "accepted sids named in the error"
