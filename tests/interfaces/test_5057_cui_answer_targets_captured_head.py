"""Tier 2: #5057 -- plain ``--cui``'s intervention-answer path delivers BY the
head's OWN id, not the retired ``answer_oldest_intervention_text`` fallback
(which re-derived "whichever intervention is head" fresh, independent of
what ``pending_intervention_head()`` itself had just returned to the caller).

Real chain of causes (architect's own trace, issuecomment-5378442342;
lead-coder's relay): ``stream_client.route_input_line`` already reads
``transport.pending_intervention_head()`` to decide WHETHER to deliver
directly -- but, before this fix, threw that value away and called
``transport.answer_intervention_text(text)`` with no id at all. That fell
through to ``InProcessTransport``/``SessionBoundTransport``'s own
id-less branch, which called ``Session.answer_oldest_intervention_text`` --
a SEPARATE, independent re-read of ``self._interventions.head()`` one layer
down. Two layers agreeing "there is a pending intervention" is not the same
as both agreeing on WHICH one: #3299 P2's own ``answer_intervention_by_id``
(R1) was invented precisely so a reply is delivered to the id the caller
was actually shown, never re-derived by position at the delivery layer --
every OTHER surface (Textual's panel, the AG-UI wire) already worked this
way; plain ``--cui`` was the one producer #5057 measured that still hadn't
migrated (#5047's own scoping thread).

This test proves the id actually flows end-to-end with TWO real, distinct
pending interventions -- with only one pending, "answered by id" and
"answered by re-derived position" are indistinguishable (both single
plausible targets), so a second, non-head intervention is required as a
witness that the SPECIFIC id -- not merely "some pending intervention" --
was threaded through and left the non-target untouched.

Strip-falsifier: disabling ``route_input_line``'s direct-delivery branch
(the pre-#5057 shape, minus even the id-less fallback call, since
``InProcessTransport``'s own id-less branch is now fail-closed rather than
"answer_oldest") turns this RED -- verified locally: A's ``asyncio.wait_for``
below TIMES OUT rather than resolving, since nothing answers it at all.

Real ``Session`` + real ``InProcessTransport`` + the real
``InterventionHandler`` dispatch path -- no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.repl.stream_client import route_input_line
from reyn.interfaces.transport.in_process import InProcessTransport
from reyn.runtime.session import DEFAULT_CHAT_CHANNEL_ID
from reyn.user_intervention import UserIntervention
from tests._async_wait import wait_until
from tests._support.agent_session import make_session


def _transport_for(session) -> InProcessTransport:
    """The local ClientTransport over a single-session registry -- the
    production send seam the ``--cui`` client routes input through (mirrors
    ``test_cui_permission_answer_resumes_2690.py``'s own helper)."""
    return InProcessTransport(
        SimpleNamespace(attached_session=lambda: session),
        intervention_channel=DEFAULT_CHAT_CHANNEL_ID,
    )


@pytest.mark.asyncio
async def test_cui_answer_targets_the_head_intervention_leaving_the_second_untouched(
    tmp_path: Path,
) -> None:
    """Tier 2: two real pending interventions (A dispatched first = head, B
    queued behind it); one plain-``--cui`` reply through the real production
    ``route_input_line`` resolves ONLY A, with A's own text, leaving B
    genuinely still pending -- not "some intervention got answered", the
    SPECIFIC one the transport's own head reported."""
    session = make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / ".reyn" / "wal.jsonl"),
        snapshot_path=tmp_path / ".reyn" / "snap.json",
        workspace_base_dir=tmp_path,
    )
    # run_repl registers this listener on the attached session -- without it
    # the listener-presence guard auto-refuses every intervention.
    session.register_intervention_listener(DEFAULT_CHAT_CHANNEL_ID)
    transport = _transport_for(session)

    iv_a = UserIntervention(kind="ask_user", prompt="Proceed with A?", run_id="r-a")
    iv_b = UserIntervention(kind="ask_user", prompt="Proceed with B?", run_id="r-b")
    task_a = asyncio.ensure_future(session._intervention_handler.dispatch(iv_a))
    await wait_until(lambda: bool(session.interventions.list_active()))
    task_b = asyncio.ensure_future(session._intervention_handler.dispatch(iv_b))
    await wait_until(lambda: len(session.interventions.list_active()) == 2)

    head = transport.pending_intervention_head()
    assert getattr(head, "id", head) == iv_a.id, (
        "the head must be the FIRST-dispatched intervention (FIFO) -- the "
        "test's own premise, not the fix under test"
    )

    await route_input_line(transport, "answer meant for A", None)

    answer_a = await asyncio.wait_for(task_a, timeout=5.0)
    assert answer_a.text == "answer meant for A", (
        f"A must resolve with its own reply text, got {answer_a!r}"
    )
    assert not task_b.done(), (
        "B must remain genuinely pending -- a misdelivery to B (the "
        "retired oldest-fallback re-reading head at a lower layer) would "
        "resolve it with text meant for A"
    )
    assert iv_b.id in {iv.id for iv in session.interventions.list_active()}, (
        "B must still be the one active, untouched intervention"
    )

    # Clean up B so the test doesn't leak a dangling task.
    resolved_b = await session.answer_intervention_by_id(iv_b.id, "answer meant for B")
    assert resolved_b is True
    answer_b = await asyncio.wait_for(task_b, timeout=5.0)
    assert answer_b.text == "answer meant for B"


# ── _pending_head_id's own shape-check (architect's PR #5083 review finding) ─


def test_pending_head_id_accepts_a_bare_string_and_an_object_with_a_string_id() -> None:
    """Tier 2: the two RECOGNIZED shapes -- ``AgUiTransport``'s own bare id
    string, and ``InProcessTransport``/``SessionBoundTransport``'s real
    ``UserIntervention`` object (any object exposing a genuine string
    ``.id`` is treated identically, matching the family, not just the one
    concrete class)."""
    from reyn.interfaces.repl.stream_client import _pending_head_id

    assert _pending_head_id("iv-remote") == "iv-remote"

    iv = UserIntervention(kind="ask_user", prompt="x?", run_id="r")
    assert _pending_head_id(iv) == iv.id

    assert _pending_head_id(None) is None


def test_pending_head_id_refuses_to_derive_a_garbage_id_from_an_unrecognized_shape() -> (
    None
):
    """Tier 2: strip-falsifier for architect's PR #5083 review finding -- a
    THIRD shape (neither a bare string nor an object with a genuine string
    ``.id``) must return ``None``, never a silently-coerced ``str(...)`` of
    the whole value. A dict is the concrete near-miss named in review: a
    genuinely similar-looking but DIFFERENT contract
    (``RemoteReadModel.intervention_head()``) really does return a dict
    shape nearby in this codebase, so this is not a hypothetical.

    Strip-falsifier: reverting to the naive ``getattr(head, "id", head)``
    (this function's own pre-review form) turns this red -- it would
    return ``str({"id": "iv-x", ...})`` instead of ``None``, verified
    locally."""
    from reyn.interfaces.repl.stream_client import _pending_head_id

    dict_shaped = {"id": "iv-x", "prompt": "Allow?", "choices": []}
    assert _pending_head_id(dict_shaped) is None, (
        f"a dict-shaped value must never be silently coerced into an id "
        f"string; got {_pending_head_id(dict_shaped)!r}"
    )

    # An object whose own .id is itself the WRONG type (not a string) is the
    # same hazard one layer down -- also refused, never str()-coerced.
    class _WrongIdType:
        id = 12345

    assert _pending_head_id(_WrongIdType()) is None
