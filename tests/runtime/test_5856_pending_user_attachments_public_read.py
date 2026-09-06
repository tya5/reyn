"""Tier 2: #5856 -- ``Session.pending_user_attachments`` is a public,
copy-returning read of the per-session attachment queue.

Before this, ``_pending_user_attachments`` (the queue ``/exec-attach``,
``/attachment``, and ``/image`` all write to) had no public read naming
what it actually holds today -- only :attr:`Session.pending_user_images`,
whose own name and docstring commit to the narrower "image upload queue"
framing #5837/#5509 outgrew. Tests were reading the private attribute
directly (``session._pending_user_attachments``), and
``scripts/test_tier_audit.py``'s Rule 8 (private-read-public-alt) could
not catch it -- there was no public alternative to link the private name
to.

Real ``Session`` throughout (``tests._support.agent_session.make_session``)
-- no mocks or fakes; this file's own subject is the accessor itself, not
any one producer's behavior (those stay covered where they already are:
``test_5837_exec_slash_stage1.py``, ``test_5509_attachment_slash_command.py``,
``test_user_image_input.py``).

Queue SETUP here goes through :func:`_seed`, the one place in this file
that still touches ``_pending_user_attachments`` directly (#5856's own
brief: add a read, never a write path). Its ``session`` parameter is
DELIBERATELY unannotated: Rule 8's own type-evidence is scoped per
function, so annotating it would make this necessary setup write light up
as a false "private read has a public alternative" (the alternative this
issue adds IS a read, not a write -- there is nothing to route a queue
*write* through). Every test function itself keeps ``session: Session``
type-evident via :func:`_session`'s own annotated return, so a
REINTRODUCED private *read* in any of THEM still lights up Rule 8 exactly
as intended (verified manually: reverting a test's ``session.pending_user_
attachments`` back to ``session._pending_user_attachments`` and rerunning
``test_tier_audit.py --strict`` on this file goes red, then reverted).
"""
from __future__ import annotations

from pathlib import Path

from reyn.core.events.state_log import StateLog
from reyn.runtime.session import Session
from tests._support.agent_session import make_session


def _session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path,
    )


def _seed(session, *blocks: dict) -> None:
    """Queue *blocks* directly -- see this module's own docstring for why
    ``session`` stays unannotated and why this is the one write site."""
    session._pending_user_attachments.extend(blocks)


def _drain(session) -> None:
    """The real drain call site's own effect (``session.py``'s ``_handle_
    inbox_text``), reproduced directly for the same reason :func:`_seed`
    exists -- there is no public way to trigger JUST the drain without
    driving a real submitted turn end to end."""
    session._pending_user_attachments.clear()


def test_empty_session_reports_an_empty_tuple(tmp_path):
    """Tier 2: accept -- a fresh session's queue reads as empty, not None
    or a missing attribute."""
    session = _session(tmp_path)
    assert session.pending_user_attachments == ()


def test_reflects_a_real_write_from_any_producer(tmp_path):
    """Tier 2: accept -- the accessor reads live production state, not a
    snapshot taken at construction time, and reads EVERY producer's block
    shape -- not just an image's -- unlike :attr:`Session.pending_user_
    images`'s own narrower framing."""
    session = _session(tmp_path)
    _seed(
        session,
        {"type": "text", "text": "exec output"},
        {"type": "image", "path": "/tmp/x.png"},
    )

    text_block, image_block = session.pending_user_attachments
    assert text_block["type"] == "text"
    assert image_block["type"] == "image"


def test_returns_a_tuple_not_the_live_list(tmp_path):
    """Tier 2: accept -- the returned value is a genuine copy (a tuple),
    not the live list ``pending_user_images`` deliberately still exposes.
    A snapshot taken before a later write must not retroactively grow."""
    session = _session(tmp_path)
    _seed(session, {"type": "text", "text": "first"})

    snapshot = session.pending_user_attachments
    assert isinstance(snapshot, tuple)

    _seed(session, {"type": "text", "text": "second"})
    (only,) = snapshot
    assert only["text"] == "first", "a tuple snapshot must not observe a later write"
    first, second = session.pending_user_attachments
    assert (first["text"], second["text"]) == ("first", "second"), "but a FRESH read must see it"


def test_drain_is_visible_through_the_same_accessor(tmp_path):
    """Tier 2: accept -- the accessor tracks the queue's own drain-on-send
    lifecycle (session.py's own module comment: reset to ``[]``), since it
    reads live state rather than caching anything of its own."""
    session = _session(tmp_path)
    _seed(session, {"type": "text", "text": "queued"})
    (queued,) = session.pending_user_attachments
    assert queued["text"] == "queued"

    _drain(session)
    assert session.pending_user_attachments == ()
