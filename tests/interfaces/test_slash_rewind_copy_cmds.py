"""Tier 2: /rewind + /copy slash — handler behavioural paths.

/rewind has five distinct paths: bare-no-checkpoints, bare-with-checkpoints,
non-int arg error, no-registry error, checkout-raises error, checkout-success
summary.  /copy is a thin sentinel emitter; its contract is that the sentinel
kind and verbatim args land in the outbox.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from reyn.core.events.snapshot_generations import GLOBAL_SCOPE
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
    def __init__(
        self, *, registry=None,
        agent_name: "str | None" = "agent", session_id: "str | None" = "main",
    ) -> None:
        if registry is not None:
            self._registry = registry
        self._outbox: list[OutboxMessage] = []
        self.pending_ui_calls: list[dict] = []  # public — records set_pending_command_ui calls
        # #5769 stage 3 ④: the same PUBLIC identity a real Session exposes
        # (``Session.agent_name`` / ``Session.session_id``) — ``rewind_cmd``
        # now reads these to build its default session-local scope.
        self.agent_name = agent_name
        self.session_id = session_id

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
        # #5769/#5784: the real AgentRegistry.checkout takes `scope` as a
        # required keyword-only argument (no default) -- recorded here too
        # so a test can assert on it, matching the real signature this fake
        # stands in for.
        self.checkout_scopes: "list[tuple[str, str] | None]" = []

    def list_rewind_points(self, *, include_abandoned: bool = False) -> list[dict]:
        return self._points

    def list_branches(self) -> list:
        return self._branches

    async def checkout(self, target: int, *, scope: "tuple[str, str] | None") -> dict:
        self.checkout_calls.append(target)
        self.checkout_scopes.append(scope)
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
    # #5769 stage 3 ④: the default is session-local, to the INVOKING
    # session's own identity (`_FakeSession`'s own default here:
    # agent_name="agent", session_id="main") -- not GLOBAL_SCOPE. This
    # assertion is the witness that the UI default actually changed
    # (previously every bare `/rewind <N>` named GLOBAL_SCOPE, #5784's own
    # call site before this PR); the explicit-`global` and explicit-
    # session-local paths are covered separately below.
    assert registry.checkout_scopes == [("agent", "main")]


# ── /rewind <N> [global] scope (#5769 stage 3 ④, ADR-0047 decision 3) ──────


@pytest.mark.asyncio
async def test_rewind_direct_defaults_to_session_local_scope() -> None:
    """Tier 2: bare '/rewind <N>' (no 'global') passes the INVOKING session's
    own (agent_name, session_id) as checkout's scope -- the UI default
    (ADR-0047 decision 3), never a bare/unscoped call. The API keeps no
    default of its own (checkout's ``scope`` is required-keyword); this
    command layer is the one place that decides and always states it."""
    registry = _FakeRegistry(checkout_result={"agents": [], "target_n": 5})
    session = _FakeSession(registry=registry, agent_name="alpha", session_id="sess-9")
    await rewind_cmd(_ctx(session), "5")
    assert registry.checkout_scopes == [("alpha", "sess-9")]


@pytest.mark.asyncio
async def test_rewind_direct_global_keyword_passes_global_scope() -> None:
    """Tier 2: '/rewind <N> global' explicitly requests the whole-substrate
    cut -- GLOBAL_SCOPE, the architecture-enforced global shape unchanged
    since before ADR-0047 (named explicitly per #5784, not a bare `None`
    literal a forgetful caller could produce by accident)."""
    registry = _FakeRegistry(checkout_result={"agents": [], "target_n": 5})
    session = _FakeSession(registry=registry, agent_name="alpha", session_id="sess-9")
    await rewind_cmd(_ctx(session), "5 global")
    assert registry.checkout_scopes == [GLOBAL_SCOPE]


@pytest.mark.asyncio
async def test_rewind_direct_states_scope_before_the_operation() -> None:
    """Tier 2: #5769 stage 3 ④ (architect scope) -- which of the two shapes
    a '/rewind <N>' is must be visible BEFORE checkout runs, not only in
    the after-the-fact summary. A reply naming the scope must already be
    in the outbox at the moment checkout() is invoked."""
    seen_before_checkout: list[str] = []

    class _WitnessRegistry(_FakeRegistry):
        async def checkout(self, target: int, *, scope=None) -> dict:
            # Captured INSIDE checkout(), so this proves order, not just
            # eventual presence in the transcript.
            seen_before_checkout.extend(session.system_text().split())
            return await super().checkout(target, scope=scope)

    session = _FakeSession(agent_name="alpha", session_id="sess-9")
    registry = _WitnessRegistry(checkout_result={"agents": [], "target_n": 5})
    session._registry = registry
    await rewind_cmd(_ctx(session), "5")
    assert "session-local" in " ".join(seen_before_checkout), (
        f"scope must be visible in the reply BEFORE checkout runs; saw {seen_before_checkout!r}"
    )


@pytest.mark.asyncio
async def test_rewind_direct_scope_reply_never_says_gone() -> None:
    """Tier 2: the pre-flight scope reply must never describe the rewound
    future as lost/gone (lead-coder/architect correction: it survives as
    an inactive, re-selectable branch)."""
    registry = _FakeRegistry(checkout_result={"agents": [], "target_n": 5})
    session = _FakeSession(registry=registry, agent_name="alpha", session_id="sess-9")
    await rewind_cmd(_ctx(session), "5")
    text = session.system_text().lower()
    # #5785 review (lead-coder BLOCKING ①): an EMPTY reply also passes every
    # "forbidden word absent" check below -- assert the reply actually
    # happened first, so the forbidden-word checks bite on real content.
    assert text, "expected a non-empty pre-flight scope reply"
    for forbidden in ("gone", "disappear", "lost", "delete"):
        assert forbidden not in text, f"{forbidden!r} must not appear in the rewind reply: {text!r}"


@pytest.mark.asyncio
async def test_rewind_direct_unknown_identity_without_global_is_an_error() -> None:
    """Tier 2: a session that cannot report its own agent_name/session_id
    (defensive -- always true in production) cannot default to
    session-local; the honest failure names the workaround rather than
    silently falling back to a scope that was never asked for."""
    registry = _FakeRegistry(checkout_result={"agents": [], "target_n": 5})
    session = _FakeSession(registry=registry, agent_name=None, session_id=None)
    await rewind_cmd(_ctx(session), "5")
    assert registry.checkout_calls == [], "checkout must not run without a resolvable scope"
    # #5785 review (lead-coder BLOCKING ②): the docstring's own claim is
    # "the honest failure NAMES the workaround" -- assert an error reply
    # actually happened (an empty checkout_calls list is also true if
    # rewind_cmd crashed on its first line, so that assert alone is a
    # green-on-empty witness) before checking what it names.
    err = session.error_text()
    assert err, "expected a non-empty error reply"
    assert "global" in err, f"the error must name the workaround ('global'); got {err!r}"


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
