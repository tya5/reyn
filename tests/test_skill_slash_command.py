"""Tier 2: PR-resume-ux U2 — /skill slash command.

Two sub-commands:
  /skill list                — show active skill runs (id, name, current_phase)
  /skill discard <id>        — abort a specific run + cleanup

Mid-session ``/skill discard`` must:
  - cancel the running task (if any) via ``session.running_skills`` + await
  - drop pending interventions for the run via ``_drop_interventions_for_run``
  - mark the skill discarded via ``SkillRegistry.complete(status="discarded")``
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.chat.services.snapshot_journal import SnapshotJournal
from reyn.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    wal_path = tmp_path / "state.wal"
    session = ChatSession(
        agent_name=agent_name,
        state_log=StateLog(wal_path),
    )
    session._snapshot_path = tmp_path / f"{agent_name}_snapshot.json"
    session._journal = SnapshotJournal(
        agent_name=agent_name,
        snapshot_path=session._snapshot_path,
        state_log=session._journal._state_log,
    )
    return session


def _drain_outbox(session: ChatSession) -> list:
    out = []
    while not session.outbox.empty():
        out.append(session.outbox.get_nowait())
    return out


# ---------------------------------------------------------------------------
# /skill list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_list_shows_active_runs(tmp_path, monkeypatch):
    """Tier 2: /skill list emits a status message with each active run.

    Format: ``run_id <skill_name> @ <current_phase>`` (or similar concise).
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    assert reg is not None
    await reg.start(
        run_id="run_A", skill_name="blog_writer",
        skill_input={"type": "input", "data": {}},
    )
    await reg.advance_phase(run_id="run_A", next_phase="draft")
    await reg.start(
        run_id="run_B", skill_name="eval_runner",
        skill_input={"type": "input", "data": {}},
    )

    # Invoke /skill list via the session's slash dispatch
    consumed = await session._maybe_handle_slash("/skill list")
    assert consumed is True

    msgs = _drain_outbox(session)
    status_texts = [m.text for m in msgs if m.kind == "status"]
    combined = "\n".join(status_texts)
    assert "run_A" in combined
    assert "blog_writer" in combined
    assert "run_B" in combined
    assert "eval_runner" in combined


@pytest.mark.asyncio
async def test_skill_list_with_no_active(tmp_path, monkeypatch):
    """Tier 2: /skill list with no active runs reports a hint."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill list")
    assert consumed is True

    msgs = _drain_outbox(session)
    combined = "\n".join(m.text for m in msgs)
    # Some sort of "none" / "no active" hint
    assert "no active" in combined.lower() or \
        "no skill" in combined.lower() or \
        "中断中の skill はありません" in combined or \
        "(none)" in combined.lower()


# ---------------------------------------------------------------------------
# /skill discard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skill_discard_completes_with_discarded_status(tmp_path, monkeypatch):
    """Tier 2: /skill discard <id> emits ``skill_discarded`` to WAL."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_to_discard", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    consumed = await session._maybe_handle_slash("/skill discard run_to_discard")
    assert consumed is True

    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    assert len(discarded) == 1
    assert discarded[0]["run_id"] == "run_to_discard"


@pytest.mark.asyncio
async def test_skill_discard_unknown_id_reports_error(tmp_path, monkeypatch):
    """Tier 2: discarding an unknown run_id surfaces an error message."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill discard nonexistent_id")
    assert consumed is True

    msgs = _drain_outbox(session)
    error_msgs = [m for m in msgs if m.kind == "error"]
    assert error_msgs, f"unknown id discard should emit error; got {msgs}"


@pytest.mark.asyncio
async def test_skill_discard_cancels_running_task(tmp_path, monkeypatch):
    """Tier 2: mid-session discard cancels the asyncio.Task to prevent zombie."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_running", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    # Simulate a running skill task: a coroutine that just sleeps forever
    async def _run_forever():
        await asyncio.sleep(60)

    task = asyncio.ensure_future(_run_forever())
    session.running_skills["run_running"] = task

    consumed = await session._maybe_handle_slash("/skill discard run_running")
    assert consumed is True

    # Task should be cancelled
    assert task.cancelled() or task.done(), (
        "discarding a running skill must cancel its task"
    )


@pytest.mark.asyncio
async def test_skill_discard_drops_interventions(tmp_path, monkeypatch):
    """Tier 2: discarding emits ``intervention_resolved`` for any pending interventions for that run."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_with_iv", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    # Dispatch a fake intervention tied to this run_id
    from reyn.user_intervention import UserIntervention
    iv = UserIntervention(kind="ask_user", prompt="Q?", run_id="run_with_iv")
    iv.future = asyncio.get_running_loop().create_future()
    dispatch_task = asyncio.ensure_future(session._dispatch_intervention(iv))
    for _ in range(3):
        await asyncio.sleep(0)

    consumed = await session._maybe_handle_slash("/skill discard run_with_iv")
    assert consumed is True

    # Yield so intervention finally clauses run
    for _ in range(3):
        await asyncio.sleep(0)

    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    resolved = [e for e in events if e["kind"] == "intervention_resolved"]
    assert any(e["intervention_id"] == iv.id for e in resolved), (
        f"discarding a run with pending iv must resolve the iv; "
        f"got {[e for e in events if 'intervention' in e['kind']]}"
    )

    await asyncio.gather(dispatch_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_skill_discard_no_args_reports_error(tmp_path, monkeypatch):
    """Tier 2: /skill discard without an id is a usage error (not silent no-op)."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill discard")
    assert consumed is True
    msgs = _drain_outbox(session)
    error_msgs = [m for m in msgs if m.kind == "error"]
    assert error_msgs, "missing arg should emit error usage hint"


@pytest.mark.asyncio
async def test_skill_command_unknown_subcommand_reports_error(tmp_path, monkeypatch):
    """Tier 2: /skill foobar surfaces a usage hint."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    consumed = await session._maybe_handle_slash("/skill foobar")
    assert consumed is True
    msgs = _drain_outbox(session)
    # Either error or usage hint
    combined = "\n".join(m.text for m in msgs)
    assert "list" in combined.lower() and "discard" in combined.lower(), (
        f"unknown subcommand should hint at valid ones; got {combined}"
    )
