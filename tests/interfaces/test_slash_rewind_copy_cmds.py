"""Tier 2: /rewind + /copy slash — handler behavioural paths.

/rewind has five distinct paths: bare-no-checkpoints, bare-with-checkpoints,
non-int arg error, no-registry error, checkout-raises error, checkout-success
summary.  /copy is a thin sentinel emitter; its contract is that the sentinel
kind and verbatim args land in the outbox.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from reyn.interfaces.slash.copy import copy_cmd
from reyn.interfaces.slash.rewind import rewind_cmd
from reyn.runtime.outbox import OutboxMessage
from tests._support.slash import slash_ctx

# ── stubs ──────────────────────────────────────────────────────────────────


def _ctx(session):
    """The context the production dispatch hands a slash handler.

    The transport IS this test's display recorder — ``reply()`` writes
    through the client seam now, so the list the assertions read is the
    one the transport fills.
    """
    return slash_ctx(session, recorder=session._outbox)


class _FakeSession:
    def __init__(self, *, registry=None) -> None:
        if registry is not None:
            self._registry = registry
        self._outbox: list[OutboxMessage] = []
        self.pending_ui_calls: list[dict] = []  # public — records set_pending_command_ui calls

    async def _put_outbox(self, msg: OutboxMessage) -> None:
        self._outbox.append(msg)

    def set_pending_command_ui(self, payload: dict) -> None:
        self.pending_ui_calls.append(payload)

    def system_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "system")

    def error_text(self) -> str:
        return " ".join(m.text for m in self._outbox if m.kind == "error")

    def outbox_kinds(self) -> list[str]:
        return [m.kind for m in self._outbox]


@dataclass
class _Branch:
    """#3987 ②: the shape ``AgentRegistry.list_branches`` returns — a real
    ``Branch`` dataclass in production. Mirrored here rather than imported so
    this file keeps its no-heavy-import property."""

    branch_id: int
    fork_point_seq: int
    head_seq: int
    parent_branch_id: "int | None"
    is_active: bool


class _FakeRegistry:
    def __init__(
        self,
        *,
        points: list[dict] | None = None,
        branches: list | None = None,
        checkout_result: dict | None = None,
        checkout_raises: Exception | None = None,
    ) -> None:
        self._points = points or []
        # #3987 ②: the real registry has always taken ``include_abandoned`` and
        # has always had ``list_branches``; this fake models both now that the
        # bare-/rewind path reads them. Default = one active branch, which is
        # the single-branch shape these tests were written for.
        self._branches = branches if branches is not None else [
            _Branch(branch_id=0, fork_point_seq=0, head_seq=99,
                    parent_branch_id=None, is_active=True),
        ]
        self._checkout_result = checkout_result
        self._checkout_raises = checkout_raises
        self.checkout_calls: list[int] = []

    def list_rewind_points(self, *, include_abandoned: bool = False) -> list[dict]:
        return self._points

    def list_branches(self) -> list:
        return self._branches

    async def checkout(self, target: int) -> dict:
        self.checkout_calls.append(target)
        if self._checkout_raises is not None:
            raise self._checkout_raises
        return self._checkout_result or {"agents": [], "target_n": target}


# ── /rewind bare (no arg) paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_bare_no_registry_no_crash() -> None:
    """Tier 2: bare /rewind with no registry attached replies a graceful no-checkpoints message."""
    session = _FakeSession()  # no _registry attr
    await rewind_cmd(_ctx(session), "")
    assert session.system_text(), "expected a system reply"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_rewind_bare_empty_points_replies_no_checkpoints() -> None:
    """Tier 2: bare /rewind with registry that has no points → 'no earlier checkpoints' reply."""
    registry = _FakeRegistry(points=[])
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "")
    assert "no earlier checkpoints" in session.system_text()


@pytest.mark.asyncio
async def test_rewind_bare_with_points_emits_rewind_list_sentinel() -> None:
    """Tier 2: bare /rewind with checkpoint points emits __rewind_list__ OutboxMessage."""
    points = [{"seq": 1, "kind": "phase_start"}, {"seq": 2, "kind": "phase_end"}]
    registry = _FakeRegistry(points=points)
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "")
    assert "__rewind_list__" in session.outbox_kinds()


@pytest.mark.asyncio
async def test_rewind_bare_with_points_calls_set_pending_command_ui() -> None:
    """Tier 2: bare /rewind with points calls set_pending_command_ui with kind='rewind'."""
    points = [{"seq": 5, "kind": "phase_start"}]
    registry = _FakeRegistry(points=points)
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "")
    assert session.pending_ui_calls, "set_pending_command_ui was not called"
    assert session.pending_ui_calls[0].get("kind") == "rewind"


# ── /rewind <N> (direct) paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_non_integer_arg_is_an_error() -> None:
    """Tier 2: /rewind with a non-integer arg replies an error, not a crash."""
    session = _FakeSession()
    await rewind_cmd(_ctx(session), "notanumber")
    assert session.error_text(), "expected error on non-integer arg"
    assert not session.system_text()


@pytest.mark.asyncio
async def test_rewind_direct_no_registry_is_an_error() -> None:
    """Tier 2: /rewind <N> with no registry attached replies an error."""
    session = _FakeSession()  # no _registry
    await rewind_cmd(_ctx(session), "3")
    assert session.error_text(), "expected error when no registry"


@pytest.mark.asyncio
async def test_rewind_direct_checkout_raises_surfaces_error() -> None:
    """Tier 2: /rewind <N> when checkout raises → error with the exception text."""
    exc = RuntimeError("seq 99 not found in WAL")
    registry = _FakeRegistry(checkout_raises=exc)
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "99")
    err = session.error_text()
    assert "seq 99 not found" in err


@pytest.mark.asyncio
async def test_rewind_direct_success_mentions_agent_count() -> None:
    """Tier 2: /rewind <N> success reply surfaces the number of agents reset."""
    result = {"agents": ["a1", "a2", "a3"], "target_n": 7}
    registry = _FakeRegistry(checkout_result=result)
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "7")
    text = session.system_text()
    assert "3" in text, f"agent count not in reply: {text!r}"
    assert not session.error_text()


@pytest.mark.asyncio
async def test_rewind_direct_success_calls_checkout_with_parsed_int() -> None:
    """Tier 2: /rewind <N> parses arg to int and passes it to registry.checkout."""
    result = {"agents": [], "target_n": 42}
    registry = _FakeRegistry(checkout_result=result)
    session = _FakeSession(registry=registry)
    await rewind_cmd(_ctx(session), "42")
    assert registry.checkout_calls == [42]


# ── /copy sentinel emitter ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_emits_copy_sentinel_kind() -> None:
    """Tier 2: /copy always emits the __copy_last_reply__ sentinel kind."""
    session = _FakeSession()
    await copy_cmd(_ctx(session), "")
    assert "__copy_last_reply__" in session.outbox_kinds()


@pytest.mark.asyncio
async def test_copy_passes_args_verbatim_as_text() -> None:
    """Tier 2: /copy <N> puts the raw arg string in the sentinel's text field."""
    session = _FakeSession()
    await copy_cmd(_ctx(session), "2")
    msgs = [m for m in session._outbox if m.kind == "__copy_last_reply__"]
    assert msgs and msgs[0].text == "2"


@pytest.mark.asyncio
async def test_copy_list_arg_passes_through() -> None:
    """Tier 2: /copy list passes the 'list' token verbatim (the output loop validates)."""
    session = _FakeSession()
    await copy_cmd(_ctx(session), "list")
    msgs = [m for m in session._outbox if m.kind == "__copy_last_reply__"]
    assert msgs and msgs[0].text == "list"
