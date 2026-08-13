"""#3327 — ``/answer`` must dispatch IMMEDIATELY, bypassing the #3300 sent-queue,
when it targets a pending intervention.

Root cause: the #3300 sent-queue durably HOLDS a queued Composer submit, but
only DISPATCHES it once the blocking turn frees — and for an ``/answer``
aimed at the SAME pending intervention, the turn only frees once THAT
intervention resolves. A queued ``/answer`` therefore chases its own
precondition and can never fire (chicken-and-egg). Combined with #3299 P1's
``Esc`` returning focus WITHOUT answering (the intended escape hatch), a
keyboard-only Textual-chat user who dismissed the panel had no way back at
all — the deadlock issue #3327 reports.

The fix: ``TextualChatApp._submit`` runs an ``/answer`` line as a COMMAND
rather than submitting it as a turn, through the same un-queued funnel
(``_deliver_answer_to``) the ``InterventionPanel``'s own (never-queued) answer
delivery already uses.

★ #3595 S5 GENERALIZED that fix and this file follows it. The narrow
``/answer``-only fast path (``Session.maybe_deliver_answer_command`` →
``InProcessTransport.deliver_pending_answer``) is gone; the shared client-side
slash layer (``reyn.interfaces.slash.dispatch.maybe_dispatch_slash``, called
from ``_submit`` and from the CUI's ``route_input_line`` BEFORE
``submit_user_text``) runs EVERY command that way, because a client-side layer
has no inbox to queue one on. The deadlock argument was never specific to
``/answer``: any command meant to act on a busy session chases the same
precondition. What is deliberately NOT widened, and is still witnessed below, is
the sent-queue contract for ORDINARY turns — bare text still queues.

Policy (docs/deep-dives/contributing/testing.md): real instances only — a
real ``Session`` + real ``PermissionResolver`` + the real intervention
machinery + the real ``InProcessTransport``, driven exactly as
``TextualChatApp._submit`` drives it. The ONLY faked boundary is the LLM call
(the established idiom, see ``test_cui_permission_answer_resumes_2690.py``).
No MagicMock / AsyncMock / patch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.security.permissions.permissions import PermissionResolver
from tests._support.agent_session import make_session
from tests._support.slash import drain_display, local_transport

_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _transport_for(session) -> "tuple[InProcessTransport, asyncio.Queue]":
    """The local ``ClientTransport`` over a single-session registry — the SAME
    seam ``TextualChatApp._submit`` sends every Composer submission through —
    paired with the client display queue its replies land on."""
    return local_transport(session)


def _tool_call_result(name: str, args_json: str) -> LLMToolCallResult:
    return LLMToolCallResult(
        content=None,
        tool_calls=[{
            "id": "tc_1",
            "type": "function",
            "function": {"name": name, "arguments": args_json},
        }],
        finish_reason="tool_calls",
        usage=_USAGE,
    )


def _text_result() -> LLMToolCallResult:
    return LLMToolCallResult(
        content="done", tool_calls=[], finish_reason="stop", usage=_USAGE,
    )


def _sequenced_llm_stub(results: "list[LLMToolCallResult]"):
    """Real async callable mimicking ``call_llm_tools`` — the only faked
    boundary permitted by policy."""
    state = {"n": 0}

    async def _stub(**_kwargs) -> LLMToolCallResult:
        i = state["n"]
        state["n"] += 1
        return results[min(i, len(results) - 1)]

    return _stub


async def _poll(pred, *, task: "asyncio.Task | None" = None, delay: float = 0.02) -> None:
    """Poll for ``pred`` (#3748: unbounded, owner policy). A hang surfaces
    via CI's own kill, naming this exact loop -- callers' own assertions
    still fail for the real reason if the wait ever resolves on a false
    signal.

    ``task``, when given, is the producer this predicate is waiting on
    (``session.run()``'s task, mirrors ``test_cui_permission_answer_
    resumes_2690.py``'s fix): if it dies before the predicate goes true,
    that exception IS the real failure -- re-raising it beats hanging on a
    producer that will never satisfy the predicate again."""
    while not pred():
        if task is not None and task.done():
            task.result()
        await asyncio.sleep(delay)


def _make_session(project_root: Path, *, wal: Path, snap: Path) -> Session:
    perm = PermissionResolver({}, project_root=project_root, interactive=True)
    session = make_session(
        agent_name="test-agent",
        permission_resolver=perm,
        state_log=StateLog(wal),
        snapshot_path=snap,
        workspace_base_dir=project_root,
    )
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    return session


async def _start_blocked_write_turn(
    session: Session, monkeypatch, *, out: Path, run_task: "asyncio.Task",
) -> None:
    """Drives the router to the out-of-zone write permission prompt and
    leaves the turn suspended awaiting the answer — the exact precondition
    #3327's deadlock needs (a turn blocked on the SAME intervention an
    ``/answer`` would target)."""
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _sequenced_llm_stub([
            _tool_call_result(
                "write_file",
                f'{{"path": "{out}", "content": "written ok"}}',
            ),
            _text_result(),
        ]),
    )
    await session.submit_user_text("write the file")
    await _poll(lambda: session.interventions.head() is not None, task=run_task)


@pytest.mark.asyncio
async def test_answer_command_dispatches_immediately_while_turn_blocked(
    tmp_path, monkeypatch,
):
    """Tier 2: GATE 1 + GATE 2 — an ``/answer <id-prefix> y`` submitted through
    the SAME seam ``TextualChatApp._submit`` uses
    (``maybe_dispatch_slash`` tried FIRST, THEN
    ``submit_user_text``) resolves the blocked turn's intervention promptly —
    it is not stuck behind the #3300 sent-queue, which could never drain it
    (the turn only frees once this SAME intervention resolves).

    RED before the fix: no command path existed at this seam (or
    ``_submit`` never called it) — every ``/answer`` line at this seam went
    straight to ``submit_user_text`` and sat in the inbox until the poll
    budget exhausted, since the turn stayed blocked forever. Verified by the
    companion strip test below AND by the negative control in this same test
    (the OLD ``submit_user_text``-only path, exercised on a SEPARATE fresh
    session, is shown to still hang)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await _start_blocked_write_turn(session, monkeypatch, out=out, run_task=run_task)
        head = session.interventions.head()
        assert head.kind == "permission.file.write"
        prefix = head.id[:8]

        transport, display = _transport_for(session)
        # THE PRODUCTION CALLSITE under test: TextualChatApp._submit runs
        # maybe_dispatch_slash() BEFORE submit_user_text().
        delivered = await maybe_dispatch_slash(transport, f"/answer {prefix} y")
        assert delivered is True, (
            "the client slash layer did not claim the /answer line"
        )

        await _poll(lambda: out.exists(), task=run_task)
        assert session.interventions.head() is None
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_strip_falsify_queued_answer_path_deadlocks(tmp_path, monkeypatch):
    """Tier 2: ★non-vacuity / strip-falsify for gate 1+2 — proves the deadlock
    this PR fixes is REAL: with ``/answer`` submitted the OLD way (straight
    to ``submit_user_text``, the sent-queue's normal path, never through
    the command path), the message sits durably QUEUED (never dispatched)
    forever, because the turn that would dequeue it is the SAME turn
    blocked awaiting the intervention it targets.

    #3748 decomposition (owner policy: no test may bet on elapsed time,
    including betting that N ticks of *nothing happening* proves a
    deadlock — a real deadlock never resolves, so no wait, bounded or
    not, could honestly stand in for it): asserted directly and
    synchronously via the mechanism itself, ``queued_user_messages()``
    (the server-authoritative undispatched-sent-queue accessor,
    #3300 P2a) — ``submit_user_text`` durably persists to it before
    returning, so no race and no wait are needed to observe the message
    is stuck there.

    This demonstrates the OLD seam (``submit_user_text`` alone) still
    deadlocks on the current tree exactly as before #3327 — the fix only
    changed WHICH seam ``TextualChatApp._submit`` calls FIRST
    (the client-side command path, proven above), it did not change
    ``submit_user_text``'s own queueing mechanics. This is the RED the
    companion positive test above is GREEN against."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await _start_blocked_write_turn(session, monkeypatch, out=out, run_task=run_task)
        head = session.interventions.head()
        prefix = head.id[:8]

        # The OLD (pre-#3327) seam: straight to submit_user_text, exactly
        # what a queued Composer submission does — no direct-delivery bypass.
        msg_id = await session.submit_user_text(f"/answer {prefix} y")

        # The message must land in the undispatched sent-queue -- structural
        # proof it can never be consumed (the sole inbox consumer is this
        # same still-blocked turn), not a timing observation.
        queued_ids = {m["msg_id"] for m in session.queued_user_messages()}
        assert msg_id in queued_ids, (
            "the queued-only /answer path was never durably queued — this "
            "test can't demonstrate the deadlock if the message never "
            "entered the sent-queue to begin with"
        )
        assert session.interventions.head() is not None, (
            "the intervention resolved some other way — the queued /answer "
            "must remain stuck for this to be a genuine deadlock witness"
        )
        assert not out.exists(), (
            "the write completed despite the /answer never dispatching — "
            "contradicts the deadlock this test documents"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_ordinary_submission_still_queues_during_busy_turn(tmp_path, monkeypatch):
    """Tier 2: GATE 3 — the bypass is narrow. An ORDINARY (non-``/answer``)
    Composer submission made while a turn is blocked on a pending
    intervention must still go through ``maybe_dispatch_slash`` → False →
    ``submit_user_text`` → the inbox queue, exactly per #3300 — never
    resolve the intervention, never dispatch early. ★ This is the invariant
    #3595 S5 must not touch, and the one that says what "narrow" means once
    slash itself has left the queue: TURNS queue, commands do not."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await _start_blocked_write_turn(session, monkeypatch, out=out, run_task=run_task)
        transport, display = _transport_for(session)

        delivered = await maybe_dispatch_slash(transport, "just a normal message")
        assert delivered is False, (
            "an ordinary (non-slash) submission must NOT be claimed by the "
            "client slash layer — a turn is not a command"
        )

        msg_id = await transport.submit_user_text("just a normal message")
        assert msg_id, "submit_user_text did not accept the ordinary submission"

        # It must sit UNDISPATCHED in the inbox (#3300's sent-queue), not
        # resolve anything or vanish.
        queued_texts = [
            item.get("text") for item in session.queued_user_messages()
        ]
        assert "just a normal message" in queued_texts, (
            "the ordinary submission was not queued — #3300's sent-queue "
            "contract regressed"
        )
        assert session.interventions.head() is not None, (
            "an ordinary submission must never resolve the pending "
            "intervention"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unrelated_slash_command_during_pending_intervention_never_answers_it(
    tmp_path, monkeypatch,
):
    """Tier 2: an UNRELATED command (``/model …``) run while a turn is blocked on
    a pending intervention runs, and does NOT resolve that intervention.

    ★ This test's claim CHANGED with #3595 S5, deliberately, and the change is
    the reason it is stated here rather than quietly deleted. Before S5 the
    ``/answer`` bypass was narrow by an explicit ``cmd == "answer"`` guard, and
    this test witnessed that guard: an unrelated command fell through to
    ``submit_user_text`` and QUEUED. S5 removed the guard by removing the thing
    it guarded — the session-side dispatch — so no command queues, because the
    client layer that runs them has no inbox.

    What must survive is the property the guard was protecting, and it is not
    "slash queues": it is that a command must never be mistaken for an answer to
    a pending question. That is asserted directly below (the intervention is
    still pending afterwards) instead of indirectly through the queue, plus the
    #3300 invariant that actually remains — a command is not a submission, so it
    never appears in the sent queue at all. ``test_ordinary_submission_still_
    queues_during_busy_turn`` above holds the queueing half for TURNS, which is
    what #3300 is about."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await _start_blocked_write_turn(session, monkeypatch, out=out, run_task=run_task)
        transport, display = _transport_for(session)

        consumed = await maybe_dispatch_slash(transport, "/model gpt-fake")
        assert consumed is True, (
            "the client slash layer did not claim /model — a registered command "
            "must be run as a command, not submitted as a turn"
        )
        assert session.interventions.head() is not None, (
            "an unrelated slash command RESOLVED the pending intervention — the "
            "property the old cmd == 'answer' guard existed to protect"
        )
        queued_texts = [
            item.get("text") for item in session.queued_user_messages()
        ]
        assert "/model gpt-fake" not in queued_texts, (
            "a command reached the sent queue, which renders what this operator "
            "SUBMITTED as a turn — after #3595 S5 a command is never a submission"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_answer_command_with_nothing_pending_is_not_claimed(tmp_path):
    """Tier 2: GATE 3 (mirror) — ``/answer`` typed with NOTHING pending runs and
    reports its own failure, rather than being silently swallowed.

    Pre-S5 this fell through to the queued path and the handler reported
    'nothing pending' once dispatched; the visible outcome is the same message,
    reached without the queue."""
    proj = tmp_path / "proj"
    proj.mkdir()
    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        transport, display = _transport_for(session)
        consumed = await maybe_dispatch_slash(transport, "/answer abc123 y")
        assert consumed is True, (
            "/answer with nothing pending must still be claimed as a command"
        )
        shown = " ".join(m.text for m in drain_display(display) if m.kind == "error")
        assert shown, (
            "/answer with nothing pending produced no error line — it was "
            "silently swallowed, the failure this mirror test exists to catch"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
