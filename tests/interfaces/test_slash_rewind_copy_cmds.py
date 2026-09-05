"""Tier 2: /rewind + /copy slash — handler behavioural paths.

/rewind has five distinct paths: bare-no-checkpoints, bare-with-checkpoints,
non-int arg error, no-registry error, checkout-raises error, checkout-success
summary.  /copy is a thin sentinel emitter; its contract is that the sentinel
kind and verbatim args land in the outbox.

#5787: real ``AgentRegistry``/``Session``/``StateLog`` throughout (no
mocks) — replaces the earlier hand-rolled ``_FakeRegistry``/``_FakeSession``
(CLAUDE.md: "never fake a collaborator when a real instance is cheaply
constructible"; a real ``AgentRegistry`` is #5769/#5786/#5789's own
established test pattern, see ``tests/core/test_5769_stage3_checkout_
scope.py``'s own ``_make_registry``). Real collaborators are what caught a
genuine premise defect in the ORIGINAL fake-based test (see
``test_rewind_direct_global_success_mentions_agent_count``'s own docstring
below): a scoped (non-``global``) checkout can only ever reset the ONE
session named in its scope — "session-local rewind resets 3 agents" was
never a reachable production shape, only something the fake happened to
allow.

One genuinely undrivable case remains hand-rolled, disclosed inline where
it's used: ``test_rewind_direct_unknown_identity_without_global_is_an_
error`` needs a session that cannot report its own ``agent_name``/
``session_id`` — impossible with a real ``Session`` (its identity comes
from ``Agent``, which requires a real name at construction; the defensive
``getattr(..., None)`` branch this test exercises is documented, in
``rewind.py``'s own module docstring, as "always true in production" —
i.e. a shape a real Session cannot produce at all).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.agent_snapshot import AgentSnapshot
from reyn.core.events.snapshot_generations import GLOBAL_SCOPE
from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.copy import copy_cmd
from reyn.interfaces.slash.rewind import rewind_cmd
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session_params import PresentationWiring
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx

# ── real-collaborator construction helpers ──────────────────────────────


def _make_registry(tmp_path: Path) -> AgentRegistry:
    """Real ``AgentRegistry`` + real ``Session`` factory — same shape as
    ``tests/core/test_pipeline_is2_driver_session.py``'s own
    ``_agent_registry`` (no LLM ever runs in these tests, so no scripted
    reply is wired)."""
    state_log = StateLog(tmp_path / ".reyn" / "wal.jsonl")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None):
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            presentation_wiring=PresentationWiring(
                presentation_consumer=presentation_consumer,
                intervention_bridge=intervention_bridge,
            ),
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    return reg


def _spawn(reg: AgentRegistry, name: str, sid: str):
    """A real, attached ``Session`` for ``(name, sid)`` — what ``rewind_cmd``
    actually receives as ``ctx.session`` in production."""
    if not reg.exists(name):
        reg.create(name)
    reg.spawn_session(name, sid=sid, presentation_consumer=None, intervention_bridge=None)
    return reg.get_session(name, sid)


def _record_checkpoint(reg: AgentRegistry, name: str, seq: int) -> None:
    """Persist a rewind-point generation for ``name`` at WAL boundary
    ``seq`` — same helper shape as ``test_registry_list_rewind_points_1f.
    py``'s own ``_record_gen``."""
    snap = AgentSnapshot.empty(name)
    snap.applied_seq = seq
    reg._store_for(name).record(snap)


def _ctx(session):
    """The context the production dispatch hands a slash handler."""
    return slash_ctx(session)


# ── /rewind bare (no arg) paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_bare_no_registry_no_crash(tmp_path: Path) -> None:
    """Tier 2: bare /rewind with no registry attached replies a graceful no-checkpoints message."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    session._registry = None  # #5787: the one production-real shape this path covers -- a
    # session genuinely not yet attached to any registry (early construction window).
    ctx = _ctx(session)
    await rewind_cmd(ctx, "")
    assert ctx.transport.system_text(), "expected a system reply"
    assert not ctx.transport.error_text()


@pytest.mark.asyncio
async def test_rewind_bare_empty_points_replies_no_checkpoints(tmp_path: Path) -> None:
    """Tier 2: bare /rewind with registry that has no points → 'no earlier checkpoints' reply."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    ctx = _ctx(session)
    await rewind_cmd(ctx, "")
    assert "no earlier checkpoints" in ctx.transport.system_text()


@pytest.mark.asyncio
async def test_rewind_bare_with_points_emits_rewind_list_sentinel(tmp_path: Path) -> None:
    """Tier 2: bare /rewind with checkpoint points emits __rewind_list__ OutboxMessage."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    s2 = await log.append("inbox_consume", target="alpha", msg_id="m2")
    _record_checkpoint(reg, "alpha", s1)
    _record_checkpoint(reg, "alpha", s2)

    ctx = _ctx(session)
    await rewind_cmd(ctx, "")
    assert "__rewind_list__" in ctx.transport.kinds()


@pytest.mark.asyncio
async def test_rewind_bare_with_points_calls_set_pending_command_ui(tmp_path: Path) -> None:
    """Tier 2: bare /rewind with points calls set_pending_command_ui with kind='rewind'."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    log = reg.state_log
    s1 = await log.append("inbox_consume", target="alpha", msg_id="m1")
    _record_checkpoint(reg, "alpha", s1)

    ctx = _ctx(session)
    await rewind_cmd(ctx, "")
    assert session.pending_command_ui is not None, "set_pending_command_ui was not called"
    assert session.pending_command_ui.get("kind") == "rewind"


# ── /rewind <N> (direct) paths ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rewind_non_integer_arg_is_an_error(tmp_path: Path) -> None:
    """Tier 2: /rewind with a non-integer arg replies an error, not a crash."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    ctx = _ctx(session)
    await rewind_cmd(ctx, "notanumber")
    assert ctx.transport.error_text(), "expected error on non-integer arg"
    assert not ctx.transport.system_text()


@pytest.mark.asyncio
async def test_rewind_direct_no_registry_is_an_error(tmp_path: Path) -> None:
    """Tier 2: /rewind <N> with no registry attached replies an error."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    session._registry = None
    ctx = _ctx(session)
    await rewind_cmd(ctx, "3")
    assert ctx.transport.error_text(), "expected error when no registry"


@pytest.mark.asyncio
async def test_rewind_direct_checkout_raises_surfaces_error(tmp_path: Path) -> None:
    """Tier 2: /rewind <N> when checkout raises → error with the exception text.

    Real ``RewindBeyondRetentionError`` from a genuinely truncated WAL
    (same shape as ``test_5769_stage3_checkout_scope.py``'s own retention
    test) -- not a synthetic, hand-typed exception message."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    log = reg.state_log
    await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    await log.append("inbox_put", target="alpha", msg_id="a2", msg_kind="user", payload={"text": "a2"})
    await log.append("inbox_put", target="alpha", msg_id="a3", msg_kind="user", payload={"text": "a3"})
    await log.truncate_below(3)  # drop seq 1, 2 -- oldest kept = 3
    await log.flush()

    ctx = _ctx(session)
    await rewind_cmd(ctx, "2")  # seq 2 was truncated away
    err = ctx.transport.error_text()
    assert "2" in err and "retained" in err, f"expected a retention error naming seq 2, got {err!r}"


@pytest.mark.asyncio
async def test_rewind_direct_global_success_mentions_agent_count(tmp_path: Path) -> None:
    """Tier 2: /rewind <N> global success reply surfaces the number of agents reset.

    #5787: real checkout — and real checkout only resets the ONE session
    named in a session-local scope (#5769/ADR-0047 decision 3/4), never
    several. The original fake-based test drove this assertion through
    the session-LOCAL (bare, non-'global') path with a hand-typed
    ``{"agents": ["a1","a2","a3"]}`` result -- a combination that is not
    reachable in real production (a session-scoped checkout resetting 3
    OTHER agents). Real collaborators surfaced this: to genuinely reach
    "N agent(s) reset" with N > 1, the command must be the explicit
    'global' form, which resets every known agent. Rewritten accordingly
    -- same production code path (`rewind_cmd`'s own reply-formatting
    line), a reachable scenario driving it."""
    reg = _make_registry(tmp_path)
    for name in ("alpha", "beta", "gamma"):
        reg.create(name)
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)
    _record_checkpoint(reg, "beta", s)
    _record_checkpoint(reg, "gamma", s)

    session = _spawn(reg, "alpha", "sess-1")
    results: list = []
    real_checkout = reg.checkout

    async def _spy_checkout(seq, *, scope):
        result = await real_checkout(seq, scope=scope)
        results.append(result)
        return result

    reg.checkout = _spy_checkout

    ctx = _ctx(session)
    await rewind_cmd(ctx, f"{s} global")
    text = ctx.transport.system_text()
    (result,) = results
    expected_count = len(result["agents"])
    assert expected_count > 1, "sanity: this test's whole point is N > 1 agents reset"
    assert str(expected_count) in text, f"agent count {expected_count} not in reply: {text!r}"
    assert not ctx.transport.error_text()


@pytest.mark.asyncio
async def test_rewind_direct_success_calls_checkout_with_parsed_int(tmp_path: Path) -> None:
    """Tier 2: /rewind <N> parses arg to int and passes it to registry.checkout."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)

    calls: list = []
    real_checkout = reg.checkout

    async def _spy_checkout(seq, *, scope):
        calls.append(seq)
        return await real_checkout(seq, scope=scope)

    reg.checkout = _spy_checkout  # #5787: spies on the REAL checkout, doesn't replace it

    ctx = _ctx(session)
    await rewind_cmd(ctx, str(s))
    assert calls == [s]


# ── /rewind <N> [global] scope (#5769 stage 3 ④, ADR-0047 decision 3) ──────


@pytest.mark.asyncio
async def test_rewind_direct_defaults_to_session_local_scope(tmp_path: Path) -> None:
    """Tier 2: bare '/rewind <N>' (no 'global') passes the INVOKING session's
    own (agent_name, session_id) as checkout's scope -- the UI default
    (ADR-0047 decision 3), never a bare/unscoped call."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-9")
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)

    scopes: list = []
    real_checkout = reg.checkout

    async def _spy_checkout(seq, *, scope):
        scopes.append(scope)
        return await real_checkout(seq, scope=scope)

    reg.checkout = _spy_checkout

    ctx = _ctx(session)
    await rewind_cmd(ctx, str(s))
    assert scopes == [("alpha", "sess-9")]


@pytest.mark.asyncio
async def test_rewind_direct_global_keyword_passes_global_scope(tmp_path: Path) -> None:
    """Tier 2: '/rewind <N> global' explicitly requests the whole-substrate
    cut -- GLOBAL_SCOPE."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-9")
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)

    scopes: list = []
    real_checkout = reg.checkout

    async def _spy_checkout(seq, *, scope):
        scopes.append(scope)
        return await real_checkout(seq, scope=scope)

    reg.checkout = _spy_checkout

    ctx = _ctx(session)
    await rewind_cmd(ctx, f"{s} global")
    assert scopes == [GLOBAL_SCOPE]


@pytest.mark.asyncio
async def test_rewind_direct_states_scope_before_the_operation(tmp_path: Path) -> None:
    """Tier 2: #5769 stage 3 ④ (architect scope) -- which of the two shapes
    a '/rewind <N>' is must be visible BEFORE checkout runs, not only in
    the after-the-fact summary. A reply naming the scope must already be
    in the outbox at the moment checkout() is invoked."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-9")
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)

    seen_before_checkout: list[str] = []
    ctx = _ctx(session)
    real_checkout = reg.checkout

    async def _witness_checkout(seq, *, scope):
        # Captured INSIDE checkout(), so this proves order, not just
        # eventual presence in the transcript.
        seen_before_checkout.extend(ctx.transport.system_text().split())
        return await real_checkout(seq, scope=scope)

    reg.checkout = _witness_checkout

    await rewind_cmd(ctx, str(s))
    assert "session-local" in " ".join(seen_before_checkout), (
        f"scope must be visible in the reply BEFORE checkout runs; saw {seen_before_checkout!r}"
    )


@pytest.mark.asyncio
async def test_rewind_direct_scope_reply_never_says_gone(tmp_path: Path) -> None:
    """Tier 2: the pre-flight scope reply must never describe the rewound
    future as lost/gone (lead-coder/architect correction: it survives as
    an inactive, re-selectable branch)."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-9")
    log = reg.state_log
    s = await log.append("inbox_put", target="alpha", msg_id="a1", msg_kind="user", payload={"text": "a1"})
    _record_checkpoint(reg, "alpha", s)

    ctx = _ctx(session)
    await rewind_cmd(ctx, str(s))
    text = ctx.transport.system_text().lower()
    # #5785 review (lead-coder BLOCKING ①): an EMPTY reply also passes every
    # "forbidden word absent" check below -- assert the reply actually
    # happened first, so the forbidden-word checks bite on real content.
    assert text, "expected a non-empty pre-flight scope reply"
    for forbidden in ("gone", "disappear", "lost", "delete"):
        assert forbidden not in text, f"{forbidden!r} must not appear in the rewind reply: {text!r}"


@pytest.mark.asyncio
async def test_rewind_direct_unknown_identity_without_global_is_an_error(tmp_path: Path) -> None:
    """Tier 2: a session that cannot report its own agent_name/session_id
    (defensive -- always true in production) cannot default to
    session-local; the honest failure names the workaround rather than
    silently falling back to a scope that was never asked for.

    #5787: genuinely undrivable with a real ``Session`` -- ``Agent``
    requires a real ``agent_name`` at construction, so a real Session can
    never report ``None`` for its own identity. Minimal duck-typed stub
    (disclosed here, not a general-purpose fake) carrying only the 2
    attributes ``rewind_cmd``'s defensive ``getattr`` reads, plus a REAL
    registry so the ``registry is None`` branch above this one is not
    what's under test."""
    reg = _make_registry(tmp_path)
    identityless = SimpleNamespace(agent_name=None, session_id=None, _registry=reg)
    ctx = _ctx(identityless)
    await rewind_cmd(ctx, "5")
    err = ctx.transport.error_text()
    assert err, "expected a non-empty error reply"
    assert "global" in err, f"the error must name the workaround ('global'); got {err!r}"


# ── /copy sentinel emitter ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_copy_emits_copy_sentinel_kind(tmp_path: Path) -> None:
    """Tier 2: /copy always emits the __copy_last_reply__ sentinel kind."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    ctx = _ctx(session)
    await copy_cmd(ctx, "")
    assert "__copy_last_reply__" in ctx.transport.kinds()


@pytest.mark.asyncio
async def test_copy_passes_args_verbatim_as_text(tmp_path: Path) -> None:
    """Tier 2: /copy <N> puts the raw arg string in the sentinel's text field."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    ctx = _ctx(session)
    await copy_cmd(ctx, "2")
    assert ctx.transport.texts("__copy_last_reply__") == ["2"]


@pytest.mark.asyncio
async def test_copy_list_arg_passes_through(tmp_path: Path) -> None:
    """Tier 2: /copy list passes the 'list' token verbatim (the output loop validates)."""
    reg = _make_registry(tmp_path)
    session = _spawn(reg, "alpha", "sess-1")
    ctx = _ctx(session)
    await copy_cmd(ctx, "list")
    assert ctx.transport.texts("__copy_last_reply__") == ["list"]
