"""Tier 2: #1726 FP-0043 Stage 4a — the `/session` REPL command (public surface).

`/session new|switch <sid>|list` drives per-agent multi-session in the REPL. The
handler reads the public registry surface (attached_name / spawn_session /
get_session / session_ids / attached_sid) and posts a `__session_switch_request__`
sentinel for switch (mirroring `/attach`, so the focus flip is sequenced by the
registry forwarder). These tests exercise the command flow + the graceful-error
paths via a stub registry + an outbox-capturing fake session — the same pattern as
test_slash_agent. Byte-identical when unused (a session that never runs `/session`
stays single-"main").
"""
from __future__ import annotations

import pytest

from reyn.interfaces.slash import REGISTRY
from reyn.interfaces.slash.session import session_cmd
from reyn.runtime.outbox import OutboxMessage


class _StubRegistry:
    """Stub of the registry surface the /session handler reads/calls."""

    def __init__(self, *, attached_name="default", sids=("main",), focused="main"):
        self._attached_name = attached_name
        self._sids = list(sids)
        self.attached_sid = focused
        self.spawned: list[str] = []

    @property
    def attached_name(self):
        return self._attached_name

    def spawn_session(self, name):
        sid = f"s{len(self._sids)}"
        self._sids.append(sid)
        self.spawned.append(name)
        return sid

    def get_session(self, name, sid):
        return object() if sid in self._sids else None

    def session_ids(self, name):
        return list(self._sids)


class _FakeSession:
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
    await session_cmd(s, "new")
    assert reg.spawned == ["default"], "spawn_session invoked for the attached agent"
    assert "s1" in s.reply_text(), "new sid surfaced to the user"


@pytest.mark.asyncio
async def test_session_switch_known_posts_sentinel():
    """Tier 2: #1726 — `/session switch <known>` posts __session_switch_request__
    (the focus flip is driven by the registry forwarder, mirroring /attach)."""
    reg = _StubRegistry(sids=("main", "s1"))
    s = _FakeSession(reg)
    await session_cmd(s, "switch s1")
    switch_sids = [m.text for m in s.outbox_calls if m.kind == "__session_switch_request__"]
    assert switch_sids == ["s1"], "exactly the one switch sentinel, carrying the target sid"


@pytest.mark.asyncio
async def test_session_switch_unknown_is_graceful():
    """Tier 2: #1726 — `/session switch <unknown>` replies an error and posts NO
    sentinel (no crash — lead completeness pt 1)."""
    reg = _StubRegistry(sids=("main",))
    s = _FakeSession(reg)
    await session_cmd(s, "switch nope")
    assert not [m for m in s.outbox_calls if m.kind == "__session_switch_request__"]
    assert "nope" in s.reply_text(), "user-facing error names the bad sid"
    assert any(m.kind == "error" for m in s.outbox_calls), "replied as an error"


@pytest.mark.asyncio
async def test_session_list_marks_focused():
    """Tier 2: #1726 — `/session list` lists the agent's sessions with a focus marker."""
    reg = _StubRegistry(sids=("main", "s1"), focused="s1")
    s = _FakeSession(reg)
    await session_cmd(s, "list")
    body = s.reply_text()
    assert "main" in body and "s1" in body
    assert "* s1" in body, "the focused session is marked"
