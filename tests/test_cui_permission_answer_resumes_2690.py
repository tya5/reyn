"""#2690 — the ``reyn chat --cui`` plain REPL input loop must resolve a pending
permission intervention so the blocked router turn resumes.

Bug reproduced (tui-coder, during #2688 real-env verification): an interactive
file-write approval prompt (``[y]es / [j]ust this path / [r]ecursive / [N]o``)
shown in ``--cui`` never resumed after the user answered ``y`` — indefinite
hang, ``file__write`` never completed, no ``permission_granted`` event.

Root cause: the ``--cui`` plain input loop routed EVERY typed line through
``submit_user_text`` → the session inbox. But a pending permission intervention
suspends the router turn on the intervention future, and that same turn is the
SOLE inbox consumer — so the ``y`` answer sits in the inbox forever, the future
is never resolved, and the turn hangs. (The inline CUI already answers
interventions directly via a concurrent region poll; the plain loop lacked the
equivalent.) The read-approval grants earlier in the reporter's session did
NOT hang because a default-zone (in-project) read is auto-allowed — it never
raises an intervention. The out-of-zone WRITE is the first intervention-
requiring permission, so it is the first to hit the dead answer-delivery path.

The fix (``stream_client.route_input_line``) delivers a non-slash line directly
to the pending intervention via the transport's ``answer_intervention_text``
seam (wrapping ``answer_oldest_intervention_text``), bypassing the inbox so the
future resolves. Here the client's ``ClientTransport`` is the local
``InProcessTransport`` over a single-session registry — the same seam production
uses.

Policy (docs/deep-dives/contributing/testing.md): real instances only — a real
``Session`` + real ``PermissionResolver`` + the real intervention/permission
machinery + the real production ``_route_input_line``. The ONLY faked boundary
is the LLM call (``reyn.runtime.router_loop.call_llm_tools``), replaced with a
real async stub — the established idiom for driving ``session.run()`` end-to-end
without a live model. No MagicMock / AsyncMock / patch.

Both scenarios are bounded: a hang trips the poll budget and the assertion
fails (RED); the fix resolves the future so the op completes well within the
budget (GREEN).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.stream_client import route_input_line
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.llm.llm import LLMToolCallResult
from reyn.llm.pricing import TokenUsage
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID, Session
from reyn.security.permissions.permissions import PermissionResolver
from tests._support.agent_session import make_session

_USAGE = TokenUsage(prompt_tokens=5, completion_tokens=3)


def _transport_for(session) -> InProcessTransport:
    """The local ClientTransport over a single-session registry — the production
    send seam the ``--cui`` client routes input through."""
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


def _sequenced_llm_stub(results: list[LLMToolCallResult]):
    """Real async callable mimicking ``call_llm_tools`` — returns each result in
    turn (the last repeats), the only faked boundary permitted by policy."""
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
    # run_repl registers this listener on the attached session (see
    # tests/test_repl_intervention_listener.py) — without it the
    # listener-presence guard auto-refuses every intervention.
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    return session


def _granted_paths(session: Session) -> list[str]:
    """Paths for which a ``permission_granted`` event fired — the event the
    reporter found MISSING for the hung write."""
    return [
        e.data.get("path")
        for e in session._chat_events.all()
        if e.type == "permission_granted"
    ]


@pytest.mark.asyncio
async def test_cui_write_approval_answer_resumes_blocked_turn(tmp_path, monkeypatch):
    """Tier 2: a ``y`` answered through the ``--cui`` input loop resolves the
    pending file-write permission so the blocked router turn resumes — the write
    completes and ``permission_granted`` (kind=file.write) is emitted. RED before
    the fix (the inbox-routed answer never reaches the suspended future → the
    write never lands → the bounded poll times out)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    out = proj / "out.txt"  # under project root but OUTSIDE the .reyn/ write zone

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

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await session.submit_user_text("write the file")
        # The router turn reaches the out-of-zone write gate and dispatches the
        # approval prompt — the turn is now suspended awaiting the answer.
        assert await _poll(lambda: session.interventions.head() is not None), (
            "the file-write approval prompt never appeared"
        )
        head = session.interventions.head()
        assert head.kind == "permission.file.write"
        assert {c.hotkey for c in head.choices} == {"y", "j", "r", "N"}

        # THE FIX under test: the --cui input loop delivers the typed answer to
        # the attached session. On the buggy tree this routes through
        # submit_user_text → inbox → never dequeued (deadlock).
        await route_input_line(_transport_for(session), "y", None)

        assert await _poll(lambda: out.exists()), (
            "write never completed after answering y — the blocked turn did "
            "not resume (#2690 hang)"
        )
        assert out.read_text() == "written ok"
        # The intervention future resolved: nothing pending, and the
        # permission_granted event the reporter found MISSING is now emitted.
        assert session.interventions.head() is None
        assert await _poll(lambda: str(out) in _granted_paths(session)), (
            "no permission_granted event was emitted for the write path"
        )
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cui_read_approval_answer_resumes_identically(tmp_path, monkeypatch):
    """Tier 2: the mirror of the working read path — an OUT-of-zone read
    approval (the read case only auto-allows when in-zone) is resolved by the
    SAME ``--cui`` input-loop seam as the write, proving write now behaves
    identically to read once both actually require a prompt."""
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside.txt"  # OUTSIDE the project read zone
    outside.write_text("secret payload")

    monkeypatch.setattr(
        "reyn.runtime.router_loop.call_llm_tools",
        _sequenced_llm_stub([
            _tool_call_result("file__read", f'{{"path": "{outside}"}}'),
            _text_result(),
        ]),
    )

    session = _make_session(
        proj, wal=tmp_path / "state.wal", snap=tmp_path / "snap.json",
    )
    run_task = asyncio.create_task(session.run())
    try:
        await session.submit_user_text("read the outside file")
        assert await _poll(lambda: session.interventions.head() is not None), (
            "the file-read approval prompt never appeared"
        )
        assert session.interventions.head().kind == "permission.file.read"

        await route_input_line(_transport_for(session), "y", None)

        assert await _poll(lambda: str(outside) in _granted_paths(session)), (
            "read never completed after answering y — the blocked turn did "
            "not resume"
        )
        assert session.interventions.head() is None
    finally:
        await session.shutdown()
        try:
            await asyncio.wait_for(run_task, timeout=2.0)
        except asyncio.TimeoutError:
            run_task.cancel()
            await asyncio.gather(run_task, return_exceptions=True)
