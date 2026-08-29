"""Tier 2: the attached-pipeline run nudge is not an operator's client input.

``session_api.run_pipeline_attached`` starts an attached pipeline run by putting
ONE empty-text message on the driver-session's inbox and pumping it. That message
claimed ``CLIENT_INPUT`` until #3595 S2 — the fifth and last producer to do so,
and the only one the slash defect could never expose, because its text is ``""``
and ``"".startswith("/")`` is false. It survived four censuses for exactly that
reason: nothing it did was wrong, so nothing went looking for it.

It was still a wrong claim, and these are what the claim was buying that it
should not have been:

  * the sent-queue (``queued_user_messages``) is the server-authoritative list of
    what THIS operator submitted from a client. A pump with no author and no text
    was appearing in it, exactly as a Slack peer's message and a cron fire did
    before #3595 step 1b removed them for the same reason;
  * slash dispatch. Empty text cannot reach a command, so this leg measured the
    MECHANISM rather than a closed hole: text of this kind routed to the shared
    turn body and never past ``_handle_user_message``'s ``startswith("/")``.
    #3595 S5 has since deleted that entry outright — no inbox member reaches a
    command any more — so this leg is now a standing check that the nudge runs a
    turn and nothing else, with the client-side dispatch as its positive
    control.

★ What must NOT change is that the nudge still runs a turn — that is the whole
job of the message, and it is how an attached pipeline run starts at all. One
test here holds that; ``tests/core/test_pipeline_is6_attached.py`` holds the real
end-to-end.

The last test in this file is about the TYPE rather than about the nudge, and it
lives here because it is the same change's other risk: the members keep their
pre-#3595-S2 wire values, so a kind read back off a snapshot as a plain ``str``
must still be the same value as its member. It is the crash-recovery leg of "the
rename changed no behaviour".
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash.dispatch import maybe_dispatch_slash
from reyn.runtime.registry import AgentRegistry
from reyn.runtime.session import Session
from reyn.runtime.turn_origin import TurnOrigin
from tests._support.agent_session import make_session
from tests._support.minimal_reyn_yaml import MINIMAL_REYN_YAML
from tests._support.slash import local_transport

AGENT = "s2-nudge-agent"


def _session(tmp_path: Path) -> Session:
    return make_session(
        agent_name=AGENT,
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snapshot.json",
    )


@pytest.mark.asyncio
async def test_a_pipeline_nudge_is_not_a_queued_operator_submission(tmp_path: Path) -> None:
    """Tier 2: a nudge on the inbox does not appear in the sent queue; an
    operator's submission on the SAME inbox does.

    The control is the second half and it is load-bearing: an accessor that
    returned ``[]`` unconditionally — a filter that stopped matching anything —
    would satisfy the first assertion perfectly. Both messages are put on the
    same session's inbox, in the same test, so the only variable is the member.
    """
    session = _session(tmp_path)
    assert session.queued_user_messages() == []

    await session._put_inbox(
        TurnOrigin.PIPELINE_NUDGE, {"text": "", "chain_id": "c-nudge"},
    )
    assert session.queued_user_messages() == [], (
        "the attached-pipeline run nudge appears in the sent queue, which renders "
        "what THIS operator submitted from a client. Nobody authored the nudge — "
        "its text is empty and its only job is to pump the driver's executor"
    )

    await session.submit_user_text("a line the operator typed")
    assert [item["text"] for item in session.queued_user_messages()] == [
        "a line the operator typed",
    ], (
        "an operator's own submission stopped reaching the sent queue — the filter "
        "matches nothing, so the assertion above was passing vacuously"
    )


@pytest.mark.asyncio
async def test_a_pipeline_nudge_turn_cannot_execute_a_slash_command(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a nudge-kinded message whose text begins with ``/`` is read, not
    executed — and an operator's identical line still executes.

    ``/session new`` is the registered command whose side effect is observable
    from OUTSIDE the session that would run it (a session is born under the
    attached agent), which is what lets both legs assert on a real effect rather
    than on the kind string that decided it.
    """
    monkeypatch.chdir(tmp_path)
    state_log = StateLog(tmp_path / "state.wal")
    (tmp_path / "reyn.yaml").write_text(MINIMAL_REYN_YAML, encoding="utf-8")
    holder: dict = {}

    def _factory(profile, *, presentation_consumer=None, intervention_bridge=None) -> Session:
        return make_session(
            agent_name=profile.name, state_log=state_log,
            registry=holder.get("reg"), non_interactive=True,
            snapshot_path=tmp_path / f"{profile.name}_snapshot.json",
        )

    reg = AgentRegistry(project_root=tmp_path, session_factory=_factory, state_log=state_log)
    holder["reg"] = reg
    reg.create("operator")
    reg.get_or_load("operator")
    await reg.attach_session("operator", "main")
    session = reg.get_session("operator", "main")
    assert session is not None
    before = set(reg.session_ids("operator"))

    await session._run_turn_body(
        TurnOrigin.PIPELINE_NUDGE, {"text": "/session new", "chain_id": "c-nudge-slash"},
    )
    assert not set(reg.session_ids("operator")) - before, (
        "a slash line delivered under the pipeline-nudge kind EXECUTED — the nudge "
        "is reaching a slash dispatch on the claim that a human typed it — "
        "which since #3595 S5 would mean Session grew one back"
    )

    # POSITIVE CONTROL. #3595 S5 deleted Session._maybe_handle_slash, so the
    # operator's own '/session new' is driven where it now runs: the shared
    # client-side layer, over the real transport a local attach holds. The
    # control's job is unchanged — prove the command CAN spawn, so the absence
    # above is about the nudge and not about slash being dead.
    transport, _display = local_transport(session)
    handled = await maybe_dispatch_slash(transport, "/session new")
    assert handled is True and set(reg.session_ids("operator")) - before, (
        "the operator's own '/session new' did not execute either, so the absence "
        "above says nothing about the nudge — slash dispatch may simply be dead"
    )


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_a_pipeline_nudge_still_runs_one_turn(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: the nudge still drives a turn — the behaviour the rename must NOT
    change, since pumping the driver-session's executor is its entire purpose.

    ``_run_turn_body`` is driven directly (it is called this way in
    production too — by ``run_one_iteration`` — no seam is invented for
    this test) with a REAL ``RouterLoopDriver.run_turn`` dispatch
    (``@pytest.mark.llm_stub``, only ``litellm.acompletion`` is stubbed,
    #5103 ③). ``_run_turn_body`` is called directly here rather than
    through the inbox/``run_one_iteration`` path, so ``turn_started``
    (emitted one level up, in ``run_one_iteration``) never fires — the
    public read available AT this level is ``stall_trace_armed``, the
    first statement inside ``_run_turn_body`` itself, unconditionally
    reached before dispatch regardless of ``kind``. A member that fell
    through the dispatch table to nothing at all — what a new member
    with no branch does, silently — never reaches that statement, so
    this is RED for exactly the same failure this test always caught."""
    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    session = _session(tmp_path)
    collected: list = []
    session.subscribe_audit_events(collected.append)

    chain_id = "c-nudge-turn"
    await session._run_turn_body(
        TurnOrigin.PIPELINE_NUDGE, {"text": "", "chain_id": chain_id},
    )
    armed = [e for e in collected if e.type == "stall_trace_armed"]
    assert armed and armed[0].data["chain_id"] == chain_id, (
        "the pipeline-nudge kind ran no turn (stall_trace_armed never fired "
        "for chain_id={!r}). A member with no branch in _run_turn_body is "
        "dropped silently — the attached pipeline run it was supposed to "
        "start would hang until its timeout: {!r}".format(chain_id, collected)
    )


@pytest.mark.asyncio
@pytest.mark.llm_stub
async def test_a_kind_restored_from_a_snapshot_as_a_plain_string_still_dispatches(
    tmp_path: Path, monkeypatch,
) -> None:
    """Tier 2: a kind that comes back off disk as a bare ``str`` is still the same
    value as the member — recovery needs no conversion step.

    ★ This is the load-bearing half of "the wire values did not change". A
    snapshot's inbox is JSON, and ``Session.restore_state`` re-enqueues
    ``msg["kind"]`` verbatim, so what a recovered session dispatches on is a plain
    string written by whichever build saved it. Had the members been given fresh
    values, or had the type not been a ``StrEnum``, every message queued before a
    restart would fall through ``_run_turn_body`` to no branch at all — silently,
    since an unknown kind raises nothing. The journal is driven for real and the
    payload is round-tripped through ``json`` rather than asserted on in memory,
    because in-memory equality is exactly the thing that would still hold under
    the broken version.

    #5103 ③ migration: dispatch is now observed via ``stall_trace_armed``
    (public, real dispatch — ``@pytest.mark.llm_stub``) instead of a
    private ``run_turn`` replacement — see
    ``test_a_pipeline_nudge_still_runs_one_turn`` above for the full
    rationale (same seam, same reason ``turn_started`` is unavailable at
    this call depth).
    """
    session = _session(tmp_path)
    await session.submit_user_text("queued before the crash")

    on_disk = json.loads(json.dumps(session.journal.snapshot.inbox))
    kinds = [item["kind"] for item in on_disk]
    assert kinds and all(isinstance(k, str) and type(k) is str for k in kinds), (
        f"the snapshot's inbox kinds did not survive JSON as plain strings: {kinds!r}"
    )

    restored_kind = kinds[0]
    assert restored_kind == TurnOrigin.CLIENT_INPUT, (
        f"a kind read back from the snapshot ({restored_kind!r}) no longer equals "
        "TurnOrigin.CLIENT_INPUT. Every inbox message queued before a restart would "
        "reach no branch in _run_turn_body, and nothing would say so"
    )

    monkeypatch.setenv("REYN_STALL_TRACE", "5")
    collected: list = []
    session.subscribe_audit_events(collected.append)

    chain_id = "c-restored"
    await session._run_turn_body(restored_kind, {"text": "hi", "chain_id": chain_id})
    armed = [e for e in collected if e.type == "stall_trace_armed"]
    assert armed and armed[0].data["chain_id"] == chain_id, (
        "the restored plain-string kind ran no turn (stall_trace_armed never "
        f"fired for chain_id={chain_id!r}) — a recovered session would drop "
        f"every message it had queued: {collected!r}"
    )
