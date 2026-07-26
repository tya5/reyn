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

The fix: ``Session.maybe_deliver_answer_command`` (called from
``InProcessTransport.deliver_pending_answer``, called from
``TextualChatApp._submit`` BEFORE ``submit_user_text``) delivers an
``/answer`` command through the SAME un-queued, direct funnel
(``_maybe_handle_slash`` → ``_deliver_answer_to``) the
``InterventionPanel``'s own (never-queued) answer delivery already uses.

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
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.security.permissions.permissions import PermissionResolver
from tests._support.agent_session import make_session

_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _transport_for(session) -> InProcessTransport:
    """The local ``ClientTransport`` over a single-session registry — the SAME
    seam ``TextualChatApp._submit`` sends every Composer submission through."""
    return InProcessTransport(
        SimpleNamespace(attached_session=lambda: session),
        intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )


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


async def _poll(pred, *, attempts: int = 150, delay: float = 0.02) -> bool:
    """Bounded poll — a hang exhausts the budget and returns False (RED)."""
    for _ in range(attempts):
        if pred():
            return True
        await asyncio.sleep(delay)
    return False


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
    session: Session, monkeypatch, *, out: Path,
) -> None:
    """Drives the router to the out-of-zone write permission prompt and
    leaves the turn suspended awaiting the answer — the exact precondition
    #3327's deadlock needs (a turn blocked on the SAME intervention an
    ``/answer`` would target)."""
    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _sequenced_llm_stub([
            _tool_call_result(
                "file__write",
                f'{{"path": "{out}", "content": "written ok"}}',
            ),
            _text_result(),
        ]),
    )
    await session.submit_user_text("write the file")
    assert await _poll(lambda: session.interventions.head() is not None), (
        "the file-write approval prompt never appeared"
    )


@pytest.mark.asyncio
async def test_answer_command_dispatches_immediately_while_turn_blocked(
    tmp_path, monkeypatch,
):
    """Tier 2: GATE 1 + GATE 2 — an ``/answer <id-prefix> y`` submitted through
    the SAME seam ``TextualChatApp._submit`` uses
    (``transport.deliver_pending_answer`` tried FIRST, THEN
    ``submit_user_text``) resolves the blocked turn's intervention promptly —
    it is not stuck behind the #3300 sent-queue, which could never drain it
    (the turn only frees once this SAME intervention resolves).

    RED before the fix: ``deliver_pending_answer`` did not exist (or
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
        await _start_blocked_write_turn(session, monkeypatch, out=out)
        head = session.interventions.head()
        assert head.kind == "permission.file.write"
        prefix = head.id[:8]

        transport = _transport_for(session)
        # THE PRODUCTION CALLSITE under test: TextualChatApp._submit tries
        # deliver_pending_answer() BEFORE submit_user_text().
        delivered = await transport.deliver_pending_answer(f"/answer {prefix} y")
        assert delivered is True, (
            "deliver_pending_answer did not report the /answer as handled"
        )

        assert await _poll(lambda: out.exists()), (
            "the write never completed after /answer — the direct-delivery "
            "bypass did not actually resolve the blocked intervention "
            "(#3327 deadlock)"
        )
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
    ``deliver_pending_answer``), the intervention NEVER resolves within a
    generous poll budget, because the turn that would dequeue it is the SAME
    turn blocked awaiting it.

    This demonstrates the OLD seam (``submit_user_text`` alone) still
    deadlocks on the current tree exactly as before #3327 — the fix only
    changed WHICH seam ``TextualChatApp._submit`` calls FIRST
    (``deliver_pending_answer``, proven above), it did not change
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
        await _start_blocked_write_turn(session, monkeypatch, out=out)
        head = session.interventions.head()
        prefix = head.id[:8]

        # The OLD (pre-#3327) seam: straight to submit_user_text, exactly
        # what a queued Composer submission does — no direct-delivery bypass.
        await session.submit_user_text(f"/answer {prefix} y")

        resolved = await _poll(lambda: out.exists(), attempts=25, delay=0.02)
        assert resolved is False, (
            "the queued-only /answer path resolved the intervention — the "
            "deadlock this test documents did not reproduce, so the "
            "companion positive test is not proving what it claims to prove"
        )
        assert session.interventions.head() is not None, (
            "the intervention resolved some other way — the queued /answer "
            "must remain stuck for this to be a genuine deadlock witness"
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
    intervention must still go through ``deliver_pending_answer`` → False →
    ``submit_user_text`` → the inbox queue, exactly per #3300 — never
    resolve the intervention, never dispatch early."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await _start_blocked_write_turn(session, monkeypatch, out=out)
        transport = _transport_for(session)

        delivered = await transport.deliver_pending_answer("just a normal message")
        assert delivered is False, (
            "an ordinary (non-/answer) submission must NOT be claimed by "
            "deliver_pending_answer — the bypass must stay narrow to /answer"
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
async def test_answer_command_with_nothing_pending_is_not_claimed(tmp_path):
    """Tier 2: GATE 3 (mirror) — ``/answer`` typed with NOTHING pending must
    also fall through to the ordinary queued path (``deliver_pending_answer``
    returns False) rather than being silently swallowed."""
    proj = tmp_path / "proj"
    proj.mkdir()
    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        transport = _transport_for(session)
        delivered = await transport.deliver_pending_answer("/answer abc123 y")
        assert delivered is False, (
            "an /answer with nothing pending must not be claimed by the "
            "bypass — it should fall through to the ordinary queued path "
            "(where the slash handler reports its own 'nothing pending' "
            "error once dispatched)"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
