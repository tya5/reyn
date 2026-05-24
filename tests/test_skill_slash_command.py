"""Tier 2: PR-resume-ux U2 — /skill slash command.

Two sub-commands:
  /skill list                          — show active skill runs (id, name, current_phase)
  /skill discard <id>                  — preview discard (confirmation only)
  /skill discard <id> --force          — actually abort a specific run + cleanup

Mid-session ``/skill discard <id> --force`` must:
  - cancel the running task (if any) via ``session.running_skills`` + await
  - drop pending interventions for the run via ``_drop_interventions_for_run``
  - mark the skill discarded via ``SkillRegistry.complete(status="discarded")``

The bare ``/skill discard <id>`` (no ``--force``) emits a confirmation
warning instead of mutating state — protects long-running skills from
typos and Tab-completion accidents.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from reyn.chat.session import ChatSession
from reyn.events.state_log import StateLog


def _make_session(tmp_path: Path, *, agent_name: str = "alpha") -> ChatSession:
    """Build a ChatSession redirected to ``tmp_path`` via the public
    ``snapshot_path`` constructor kwarg.
    """
    return ChatSession(
        agent_name=agent_name,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / f"{agent_name}_snapshot.json",
    )


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
    status_texts = [m.text for m in msgs if m.kind == "system"]
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
    """Tier 2: /skill discard <id> --force emits ``skill_discarded`` to WAL."""
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_to_discard", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    consumed = await session._maybe_handle_slash(
        "/skill discard run_to_discard --force",
    )
    assert consumed is True

    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    assert discarded, "expected at least one skill_discarded event"
    assert discarded[0]["run_id"] == "run_to_discard"


@pytest.mark.asyncio
async def test_skill_discard_without_force_is_confirmation_only(tmp_path, monkeypatch):
    """Tier 2: /skill discard <id> without --force emits a warning and does NOT
    write a ``skill_discarded`` event.

    Protects long-running skills from typo / Tab-completion accidents. The
    warning must name the skill + run_id so the user can verify before
    re-running with --force.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_pending", skill_name="long_writer",
        skill_input={"type": "input", "data": {}},
    )

    consumed = await session._maybe_handle_slash("/skill discard run_pending")
    assert consumed is True

    # WAL must NOT contain a skill_discarded event yet
    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    assert discarded == [], (
        f"bare discard must not write skill_discarded; got {discarded}"
    )

    # Warning message must clearly identify what would be killed
    msgs = _drain_outbox(session)
    system_msgs = [m for m in msgs if m.kind == "system"]
    combined = "\n".join(m.text for m in system_msgs)
    assert "run_pending" in combined
    assert "long_writer" in combined
    assert "--force" in combined


@pytest.mark.asyncio
async def test_skill_discard_without_force_leaves_task_running(tmp_path, monkeypatch):
    """Tier 2: confirmation step must NOT cancel the asyncio.Task.

    A bare ``/skill discard <id>`` shows a preview only — the running task
    keeps going so the user can change their mind without losing work.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_keep_running", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    async def _run_forever():
        await asyncio.sleep(60)

    task = asyncio.ensure_future(_run_forever())
    session.running_skills["run_keep_running"] = task

    try:
        consumed = await session._maybe_handle_slash(
            "/skill discard run_keep_running",
        )
        assert consumed is True
        # Task must still be alive — confirmation does not cancel.
        assert not task.cancelled() and not task.done(), (
            "bare discard must NOT cancel the task"
        )
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


@pytest.mark.asyncio
async def test_skill_discard_force_flag_order_independent(tmp_path, monkeypatch):
    """Tier 2: --force may appear before or after the run_id.

    Tab-completion ordering is unpredictable; accept both forms.
    """
    monkeypatch.chdir(tmp_path)
    session = _make_session(tmp_path)
    session.is_attached = True

    reg = session._get_skill_registry()
    await reg.start(
        run_id="run_flag_first", skill_name="demo",
        skill_input={"type": "input", "data": {}},
    )

    consumed = await session._maybe_handle_slash(
        "/skill discard --force run_flag_first",
    )
    assert consumed is True

    log = StateLog(tmp_path / "state.wal")
    events = list(log.iter_from(0))
    discarded = [e for e in events if e["kind"] == "skill_discarded"]
    assert discarded, "expected at least one skill_discarded event"
    assert discarded[0]["run_id"] == "run_flag_first"


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

    consumed = await session._maybe_handle_slash(
        "/skill discard run_running --force",
    )
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

    consumed = await session._maybe_handle_slash(
        "/skill discard run_with_iv --force",
    )
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
