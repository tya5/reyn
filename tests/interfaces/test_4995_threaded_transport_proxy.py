"""Tier 2: #4995 — ``ThreadedTransportProxy`` puts a real ``ClientTransport``
on a dedicated worker thread with its own event loop — an OS-level
invariant (genuinely different threads, a real ``threading.Event``
blocking one of them) rather than a Reyn-internal contract, so this file
declares Tier 2 throughout, not a dual Tier 1/2 (lead-coder's own
#4995 review: a double declaration lets ``test_tier_audit.py``'s string
match pass either reading, which avoids rather than answers CLAUDE.md's
six-questions #6 — only a human can say which Tier a test is really
pinning, and the answer here is one, not both).

Scope, explicit (lead-coder's own #4995 review, said again here so it is
not read from the PR body alone): this file does NOT prove the TUI
becomes more responsive — that cutover (making ``TextualChatApp``/
``run_repl`` actually construct a ``ThreadedTransportProxy`` instead of
an ``InProcessTransport``) is #5048. What is proven here is the mechanism
itself: a real ``ClientTransport`` genuinely runs on a different thread,
and a caller on another thread keeps advancing while that thread is
deliberately held open — the load-bearing PRECONDITION for #5048's own
responsiveness claim, not that claim itself.

Puts a real ``ClientTransport`` on a dedicated worker thread with its own
event loop, so competing work (e.g. the router's own turn processing) no
longer starves the TUI's own scheduling on a shared loop.

**Design history, kept honest** (2 approaches rejected before this one
settled — see ``threaded.py``'s own module docstring for the full
rationale): a "one seam per offloaded function" shape (``asyncio.to_thread``
at each hot-path call site) was rejected first (architect: N seams where 1
— the transport — already exists and already has the right properties); a
``threading.Lock``-synchronized variant of the SAME single seam was
rejected second (lead-coder, measured self-contradiction: a lock held by
the core thread during I/O reproduces exactly the "TUI does not get
scheduled" symptom this issue exists to remove — witness② below would go
red under that design). Settled: immutable snapshot hand-off, no shared
mutable state, no lock.

**Both required witnesses, per lead-coder's own #4995 ruling — neither
alone is sufficient** (① alone only shows "somewhere else", not "the UI
still moves"; ② alone cannot tell a genuinely different thread apart from
a coincidence): no ``sleep``/``timeout``/``attempts=N`` anywhere (CLAUDE.md
floor/ceiling rule) — ① compares thread identities, never a duration; ②
gates the worker on a real ``threading.Event`` the test holds open and
waits on a second real ``threading.Event`` (via ``asyncio.to_thread``)
rather than any bounded/timed poll.

Real ``ThreadedTransportProxy`` — the "inner" transport under test is a
minimal, real ``ClientTransport`` subclass (not a mock/MagicMock — CLAUDE.md
forbids faking a collaborator that's cheaply constructible; this one has no
cheap real production instance to construct from without a live Session/
AgentRegistry, so a small real hand-written implementation is the
established pattern here, mirroring ``test_4983_session_switch_off_thread.
py``'s own ``QueueTransport``).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING, AsyncIterator

import pytest

from reyn.interfaces.transport.client_transport import ClientTransport
from reyn.interfaces.transport.threaded import ThreadedTransportProxy

if TYPE_CHECKING:
    from reyn.interfaces.transport.frames import DisplayFrame, EventFrame
    from reyn.runtime.outbox import OutboxMessage


class _RecordingTransport(ClientTransport):
    """A minimal, real ``ClientTransport``. ``frames()`` never yields
    anything in these tests (an unresolved real ``asyncio.Event`` — an
    unbounded wait, not a duration) since neither witness needs a frame to
    flow; ``submit_user_text`` is the one method under test, gated by a
    ``threading.Event`` the test controls."""

    def __init__(self, *, gate: "threading.Event | None" = None,
                 started: "threading.Event | None" = None) -> None:
        self._gate = gate
        self._started = started
        self.call_idents: "list[int]" = []
        self._never = asyncio.Event()

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def frames(self) -> "AsyncIterator[DisplayFrame | EventFrame]":
        await self._never.wait()
        yield  # pragma: no cover - never reached, satisfies the generator shape

    async def submit_user_text(self, text: str) -> str:
        self.call_idents.append(threading.get_ident())
        if self._started is not None:
            self._started.set()
        if self._gate is not None:
            # Blocks the WORKER thread only — a real threading.Event.wait(),
            # unbounded (CLAUDE.md: wait on the condition, never a sleep).
            self._gate.wait()
        return f"echo:{text}"

    async def answer_intervention_text(
        self, text: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    async def answer_intervention_choice(
        self, choice_id: str, *, intervention_id: "str | None" = None,
    ) -> bool:
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self):
        return None

    def put_display(self, msg: "OutboxMessage") -> None:
        pass

    async def cancel_inflight(self) -> str:
        return ""

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_witness_1_worker_work_runs_on_a_different_thread_than_the_caller():
    """Tier 2: structural witness. ``submit_user_text`` is dispatched
    through the proxy; the thread it actually executed on must differ from
    the calling (test/event-loop) thread.

    Strip-falsifier (performed manually before submission, not re-run in
    CI — reverting would mean calling the inner transport directly with no
    thread at all): calling ``inner.submit_user_text`` directly, bypassing
    the proxy, makes ``call_idents == [threading.get_ident()]`` — the SAME
    thread as the caller — turning this red."""
    caller_ident = threading.get_ident()
    inner = _RecordingTransport()
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        result = await proxy.submit_user_text("hello")
        assert result == "echo:hello"
        assert inner.call_idents == [proxy.worker_thread_ident]
        assert proxy.worker_thread_ident != caller_ident, (
            "the worker's own recorded thread identity must differ from "
            "the caller's — this is the whole point of the thread boundary"
        )
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_witness_2_the_caller_loop_keeps_advancing_while_the_worker_is_blocked():
    """Tier 2: progress witness. While the worker thread is deliberately
    held open inside ``submit_user_text`` (a real ``threading.Event.wait()``,
    released only at the end of this test), the CALLER's own event loop
    must keep scheduling other work — the actual defect #4995 exists to
    remove is the TUI not getting scheduled while the core does something
    slow on a SHARED loop.

    Strip-falsifier (performed manually, not re-run in CI — reverting would
    hang the test process): if ``ThreadedTransportProxy._call_on_worker``
    dispatched inline (called ``inner.submit_user_text`` directly on the
    caller's own thread, no ``run_coroutine_threadsafe``/thread hop at all)
    instead of on the worker thread, ``self._gate.wait()`` would block the
    CALLER's own thread — including its event loop — and the ``asyncio.
    sleep(0)`` loop below could never run even once: this test would hang
    until the harness's own CI timeout killed it, never reaching the
    ``assert progressed == 50`` line. This was verified by temporarily
    forcing ``_call_on_worker`` to await the inner coroutine directly (no
    thread) and observing the hang under a bounded ``pytest --timeout``
    invocation, then reverting — not left as an automated test, since a
    genuinely hanging test is exactly the failure mode CI's own kill switch
    exists to catch, not something to reproduce on every real run."""
    gate = threading.Event()
    started = threading.Event()
    inner = _RecordingTransport(gate=gate, started=started)
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        submit_task = asyncio.create_task(proxy.submit_user_text("hi"))
        # Unbounded wait on a real condition (started.wait()), off the
        # caller's own loop via to_thread — not a sleep, not a poll.
        await asyncio.to_thread(started.wait)

        # N is arbitrary — 50 carries no meaning of its own (not a
        # threshold; any N > 0 that lets the assertion below distinguish
        # "genuinely kept advancing" from "ran zero more times" would do).
        progressed = 0
        for _ in range(50):
            await asyncio.sleep(0)
            progressed += 1
        assert progressed == 50, (
            "the caller loop must keep scheduling other work while the "
            "worker thread sits inside a blocking call"
        )
        assert not submit_task.done(), (
            "the worker call must still be blocked at this point — proves "
            "the 50 iterations above genuinely overlapped it, rather than "
            "the worker having already finished before they ran"
        )

        gate.set()
        result = await submit_task
        assert result == "echo:hi"
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_accept_side_frames_and_sync_reads_flow_through_the_proxy():
    """Tier 2: accept-side — the proxy is not JUST a thread hop for one
    async method; the frame stream and the synchronous passthrough reads
    (``has_session`` etc., read from the single snapshot slot, refreshed
    alongside each frame — see ``threaded.py``'s own module docstring) work
    end-to-end with a real inner transport that actually produces frames
    and reports a real session.

    Strip-falsified for real (architect + lead-coder, post-CI): once the
    pre-frame phase is genuinely gated by a real ``threading.Event`` (not
    raced), a strip of the MECHANISM must go red DETERMINISTICALLY, not
    merely "not reproduced locally" — reverting ``has_session()`` to read
    ``self._inner.has_session()`` directly (bypassing the single-slot
    snapshot) turned this red immediately (``assert True is False``),
    every time, confirming the assertion genuinely depends on the slot
    mechanism rather than on timing. Restored before this commit."""
    from reyn.interfaces.transport.frames import DisplayFrame
    from reyn.runtime.outbox import OutboxMessage

    # #4995 CI finding (lead-coder): the pre-first-frame assertion below
    # previously raced the worker thread's own pump — nothing held the
    # first frame back, so whether the caller reached the assertion before
    # or after `_pump_frames` refreshed the snapshot slot was a coin flip
    # (green locally only because the caller happened to win on a fast
    # machine). Gated the same way witness② already gates the worker
    # thread — a real `threading.Event` the test controls — so the
    # pre-frame phase this test's own docstring claims is now genuinely
    # constructed, not merely hoped for. No sleep/timeout/attempts.
    release_first_frame = threading.Event()

    class _FramingTransport(_RecordingTransport):
        """Yields exactly one real frame, held back until the test releases
        ``release_first_frame`` — constructing the pre-frame phase this
        test actually asserts on, not racing it — then hangs on an
        unresolved real ``asyncio.Event`` forever (unbounded wait, not a
        duration) since this test needs no second frame."""

        async def frames(self):
            await asyncio.to_thread(release_first_frame.wait)
            yield DisplayFrame(OutboxMessage(kind="status", text="hello from core"))
            await self._never.wait()

    inner = _FramingTransport()
    proxy = ThreadedTransportProxy(
        lambda: inner, read_model_snapshot_fn=lambda: {"model": "test-model"},
    )
    proxy.start()
    try:
        try:
            # Pre-first-frame: the pre-attach contract (None), same shape
            # #5009's own cost/ctx panes already rely on. Genuinely
            # pre-frame now — the worker cannot have pumped anything yet,
            # since `_FramingTransport.frames()` is still blocked on
            # `release_first_frame` above.
            assert proxy.has_session() is False
            assert proxy.read_model_snapshot() is None
        finally:
            # Release regardless of the assertions' own outcome — the
            # generator's `await asyncio.to_thread(release_first_frame.
            # wait)` runs in a NON-daemon threadpool-executor thread; an
            # assertion failure here that left this Event unset would
            # leak that thread forever, and CPython's own atexit hook for
            # the default thread pool then hangs the WHOLE interpreter at
            # process exit waiting for it to join — turning a normal
            # AssertionError into what looks like a hang (found while
            # investigating a requested strip-falsify, not guessed).
            release_first_frame.set()

        frame = await proxy.frames().__anext__()
        assert frame.message.text == "hello from core"

        assert proxy.has_session() is True
        assert proxy.read_model_snapshot() == {"model": "test-model"}
    finally:
        await proxy.shutdown()


# ── #5044: extend_history_backward's own cross-thread pull call ──────────
#
# read_model_snapshot above is a PUSH: the worker refreshes it unconditionally
# alongside every frame, read-only, never touching disk. #5044's own issue
# body: RegistryReadModel.load_older_conversation_history() genuinely
# MUTATES Session.history and performs disk I/O — not satisfiable by that
# push design, so it needs its own PULL-style cross-thread call instead
# (extend_history_backward), on demand, returning the real post-mutation
# count. These two tests are that call's own witness — mirroring witness①
# above (a different code path: a caller-supplied closure marshalled via
# `run_coroutine_threadsafe`, not one of `_inner`'s own async methods,
# so #4995's own thread-identity claim needs re-proving for THIS path) —
# and the "nothing supplied" default, which #5044's issue body requires be
# 0 (never fabricated), mirroring ChatReadModel.load_older_conversation_
# history's own remote-impl convention.


@pytest.mark.asyncio
async def test_extend_history_backward_runs_the_closure_on_the_worker_thread():
    """Tier 2: structural witness, same shape as witness①. The supplied
    ``read_model_extend_history_fn`` must execute on the WORKER thread, not
    the caller's — the whole point of routing it through this proxy rather
    than calling a registry-bound closure directly from the caller thread
    (which would touch the worker-owned registry from a foreign thread).

    Strip-falsifier: calling ``fn`` directly from ``extend_history_backward``
    without the ``run_coroutine_threadsafe`` marshal (i.e. on the caller's
    own thread) makes ``call_idents == [threading.get_ident()]`` — the
    caller's own identity, not the worker's — turning this red."""
    caller_ident = threading.get_ident()
    call_idents: "list[int]" = []

    async def _extend(agent: "str | None", session_id: "str | None") -> int:
        # #5079/#4995: real closures are async and internally await an
        # off-loop step (architect ruling) -- a real (if trivial) await
        # here, not a bare sync function pretending to be one.
        await asyncio.sleep(0)
        call_idents.append(threading.get_ident())
        assert agent == "researcher"
        assert session_id == "abc"
        return 7

    inner = _RecordingTransport()
    proxy = ThreadedTransportProxy(
        lambda: inner, read_model_extend_history_fn=_extend,
    )
    proxy.start()
    try:
        result = await proxy.extend_history_backward(
            agent="researcher", session_id="abc",
        )
        assert result == 7
        assert call_idents == [proxy.worker_thread_ident]
        assert proxy.worker_thread_ident != caller_ident, (
            "the closure's own recorded thread identity must differ from "
            "the caller's — the same thread-boundary claim witness① makes "
            "for submit_user_text, re-proven here for a DIFFERENT code path"
        )
    finally:
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_extend_history_backward_returns_zero_when_nothing_was_supplied():
    """Tier 2: no ``read_model_extend_history_fn`` given at construction ->
    ``0``, never a fabricated count — #5044's own issue body states this
    convention explicitly, mirroring ``ChatReadModel.
    load_older_conversation_history``'s own remote-impl contract (a remote
    client has nothing to extend into either, and answers 0, never a made
    up value)."""
    inner = _RecordingTransport()
    proxy = ThreadedTransportProxy(lambda: inner)
    proxy.start()
    try:
        result = await proxy.extend_history_backward(agent="researcher")
        assert result == 0
    finally:
        await proxy.shutdown()


# ── #5079/#4995 architect ruling (issuecomment-5378398588) — the real
# Session.extend_history_backward_async split, exercised through THIS
# proxy: step ① (disk read) runs OFF the worker loop via asyncio.to_thread,
# step ② (the small in-memory splice) stays on it, guarded by the EXISTING
# before_seq value as the staleness token (no new generation mechanism —
# #4983's own precedent, applied here). These two witnesses are the ones
# architect's ruling names explicitly: ① paging must not stall the worker
# loop's own frame production (structural — counted iterations, never a
# duration) and ② a stale apply (history moved under the read) must be a
# no-op, not a corrupting splice.


def _write_history_lines(path, seqs) -> list[str]:
    lines = [
        json.dumps({"role": "user", "content": f"msg {n}", "seq": n}) for n in seqs
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def _make_history_session(tmp_path):
    """A real ``Session`` with a real ``history.jsonl`` on disk (seq
    1..10) and ``self.history`` manually seeded to "only the tail (6..10)
    is loaded" — the exact precondition ``extend_history_backward_async``
    is meant to satisfy (older entries on disk, nothing loaded yet).
    Bypasses ``load_history()``'s own 200-line tail sizing (irrelevant to
    what's under test here) by seeding ``self.history`` directly — the
    method under test only ever reads ``self.history[0].seq``, not how
    it got there."""
    from tests._support.agent_session import make_session

    project_root = tmp_path / "project"
    session = make_session(
        agent_name="alpha", workspace_state_dir=project_root / ".reyn",
        snapshot_path=(
            project_root / ".reyn" / "agents" / "alpha" / "state" / "snapshot.json"
        ),
    )
    lines = _write_history_lines(session.history_path, range(1, 11))
    session.history = [
        m for line in lines[5:] if (m := session._parse_history_line(line)) is not None
    ]
    return session


class _FramingTransport(_RecordingTransport):
    """Yields frames indefinitely, one per drain — lets a test COUNT how
    many the worker loop produces while something else is gated open,
    the same structural-progress shape witness② (in the #4995 file
    above) already established, applied to a frame producer instead of
    a blocked call."""

    async def frames(self):
        n = 0
        while True:
            from reyn.interfaces.transport.frames import DisplayFrame
            from reyn.runtime.outbox import OutboxMessage
            yield DisplayFrame(OutboxMessage(kind="status", text=f"frame {n}"))
            n += 1
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_extend_history_backward_async_does_not_stall_the_worker_loops_frame_drain(
    tmp_path,
):
    """Tier 2: architect ruling (#5079/#4995) — while
    ``Session.extend_history_backward_async``'s own disk-read step is
    gated open (a real ``threading.Event``, the SAME technique witness②
    above already establishes — never a duration), the worker loop must
    keep producing frames from the inner transport — structural proof the
    read genuinely left the worker loop (#4983's own precedent, applied
    to this second place).

    Strip-falsifier: reverting ``extend_history_backward_async`` to call
    ``read_history_before`` synchronously (no ``asyncio.to_thread``) makes
    this hang — the gated read would block the SAME worker loop
    ``_pump_frames`` needs to keep yielding frames, so the drain loop
    below would never get a chance to run at all."""
    import reyn.runtime.history_tail_reader as history_tail_reader

    real_read = history_tail_reader.read_history_before
    read_gate = threading.Event()
    read_started = threading.Event()

    def _gated_read(*args, **kwargs):
        read_started.set()
        read_gate.wait()
        return real_read(*args, **kwargs)

    session = _make_history_session(tmp_path)
    inner = _FramingTransport()

    async def _extend(agent, session_id):
        return await session.extend_history_backward_async()

    proxy = ThreadedTransportProxy(
        lambda: inner, read_model_extend_history_fn=_extend,
    )
    proxy.start()
    history_tail_reader.read_history_before = _gated_read
    try:
        extend_task = asyncio.create_task(
            proxy.extend_history_backward(agent="alpha"),
        )
        await asyncio.to_thread(read_started.wait)

        frame_iter = proxy.frames()
        drained = 0
        for _ in range(20):
            await frame_iter.__anext__()
            drained += 1
        assert drained == 20, (
            "the worker loop's own frame production stalled while "
            "extend_history_backward_async's disk read was gated open "
            "-- the read did not genuinely leave the worker loop"
        )
        assert not extend_task.done(), (
            "the gated read must still be in flight at this point -- "
            "proves the 20 drained frames genuinely overlapped it"
        )

        read_gate.set()
        result = await extend_task
        assert result == 5
        assert [m.content for m in session.history[:5]] == [
            "msg 1", "msg 2", "msg 3", "msg 4", "msg 5",
        ]
    finally:
        # Release the gate FIRST, before anything else: an assert above
        # can fail before read_gate.set() is reached, leaving the
        # asyncio.to_thread-spawned (non-daemon) worker blocked on
        # read_gate.wait() forever. proxy.shutdown() does not unblock an
        # Event-waiting thread, and CPython's default threadpool joins
        # its threads at atexit -- so an unreleased gate turns a clean
        # AssertionError into a process hang (architect finding,
        # issuecomment-5378539254). Safe to call twice (Event.set() is
        # idempotent).
        read_gate.set()
        history_tail_reader.read_history_before = real_read
        await proxy.shutdown()


@pytest.mark.asyncio
async def test_extend_history_backward_async_apply_is_a_no_op_when_history_moved_mid_read(
    tmp_path,
):
    """Tier 2: architect ruling (#5079/#4995) — #4983's own supersede-guard
    shape, applied here via the EXISTING ``before_seq`` value as the
    staleness token — no new generation mechanism. If ``self.history``'s
    own oldest ``seq`` no longer matches what ``before_seq`` captured by
    the time the gated disk read returns (another path already moved
    history out from under this call — simulated here directly), applying
    the stale read is a no-op: ``self.history`` is left exactly as the
    intervening mutation left it, never spliced with the now-stale read.

    Strip-falsifier: removing the ``current_oldest_seq != before_seq``
    guard in ``extend_history_backward_async`` (splicing unconditionally,
    as its sibling ``_load_older_entries`` does — correctly, since THAT
    one is never called across an await gap) turns this red — the stale
    entries would be prepended anyway, duplicating/misordering the
    intervening mutation's own state."""
    import reyn.runtime.history_tail_reader as history_tail_reader

    real_read = history_tail_reader.read_history_before
    read_gate = threading.Event()
    read_started = threading.Event()

    def _gated_read(*args, **kwargs):
        read_started.set()
        read_gate.wait()
        return real_read(*args, **kwargs)

    session = _make_history_session(tmp_path)
    inner = _FramingTransport()

    async def _extend(agent, session_id):
        return await session.extend_history_backward_async()

    proxy = ThreadedTransportProxy(
        lambda: inner, read_model_extend_history_fn=_extend,
    )
    proxy.start()
    history_tail_reader.read_history_before = _gated_read
    try:
        extend_task = asyncio.create_task(
            proxy.extend_history_backward(agent="alpha"),
        )
        await asyncio.to_thread(read_started.wait)

        # Simulate a concurrent path moving history out from under this
        # call, WHILE the read is still gated -- the exact race window
        # #4983's own docstring names ("a LATER switch may have claimed a
        # newer generation ... while the read above was still in
        # flight").
        intruder = session._parse_history_line(
            json.dumps({"role": "user", "content": "intruder", "seq": 99}),
        )
        session.history.insert(0, intruder)

        read_gate.set()
        result = await extend_task
        assert result == 0, (
            "a stale read (history moved mid-read) must report 0 "
            "prepended, not the count it would have prepended before "
            "the race"
        )
        assert session.history[0].content == "intruder", (
            "the intervening mutation must survive UNTOUCHED -- the "
            "stale read must not splice its own (now-wrong) entries in "
            "front of it"
        )
    finally:
        # Same reasoning as the sibling witness above: release the gate
        # FIRST. Even though read_gate.set() sits before every assert
        # here, _parse_history_line(...) / session.history.insert(...)
        # can themselves raise before reaching it -- an unreleased gate
        # is the same process-hang failure mode (architect finding,
        # issuecomment-5378577064).
        read_gate.set()
        history_tail_reader.read_history_before = real_read
        await proxy.shutdown()
