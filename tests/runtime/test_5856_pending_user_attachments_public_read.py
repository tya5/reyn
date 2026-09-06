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

Real ``Session`` throughout (``tests._support.agent_session.make_session``),
driven through the REAL ``/exec-attach``/``/attachment`` slash handlers to
populate the queue (mirrors ``test_5837_exec_slash_stage1.py``'s own
``REGISTRY.get("exec-attach").handler(ctx, …)`` pattern) -- no mocks, no
fakes, and no direct write onto ``_pending_user_attachments`` anywhere in
this file. This file's own subject is the accessor, not any one
producer's full behavior (already covered where it lives:
``test_5837_exec_slash_stage1.py``, ``test_5509_attachment_slash_command.py``,
``test_user_image_input.py``).

No drain test here: the real drain call site (``Session._handle_inbox_
text``, triggered by a submitted user turn) has no seam this file can
drive without depending on the full turn-submission machinery those
other files' own subject is, not this one's -- disclosed rather than
worked around with a private write (BLOCKING, PR #5859: an earlier
version of this file used ``session._pending_user_attachments.clear()``
directly for this, which is the exact private-state dependency this
issue exists to remove).

Verified manually (not committed): reverting
``session.pending_user_attachments`` back to
``session._pending_user_attachments`` in any test below and rerunning
``python scripts/test_tier_audit.py --strict`` on this file goes red on
Rule 8 (private-read-public-alt); reverted after confirming.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from reyn.core.events.state_log import StateLog
from reyn.interfaces.slash import REGISTRY
from reyn.runtime.session import Session
from tests._support.agent_session import make_session
from tests._support.slash import slash_ctx


def _session(tmp_path: Path) -> Session:
    return make_session(
        agent_name="alpha",
        state_log=StateLog(tmp_path / "state.wal"),
        snapshot_path=tmp_path / "snap.json",
        workspace_state_dir=tmp_path,
    )


def test_empty_session_reports_an_empty_tuple(tmp_path):
    """Tier 2: accept -- a fresh session's queue reads as empty, not None
    or a missing attribute."""
    session = _session(tmp_path)
    assert session.pending_user_attachments == ()


@pytest.mark.asyncio
async def test_reflects_real_writes_from_two_different_producers(tmp_path):
    """Tier 2: accept -- the accessor reads live production state, not a
    snapshot taken at construction time, and reads block shapes from TWO
    different real producers -- not just an image's -- unlike
    :attr:`Session.pending_user_images`'s own narrower framing.

    ``/exec-attach`` queues a plain ``{"type": "text", ...}`` block
    (#5837); ``/attachment`` queues a path-ref block for an arbitrary
    file (#5509). Both write onto the SAME queue this accessor reads."""
    session = _session(tmp_path)
    ctx = slash_ctx(session)

    exec_attach = REGISTRY.get("exec-attach")
    await exec_attach.handler(ctx, 'python3 -c "print(1+1)"')

    a_file = tmp_path / "notes.txt"
    a_file.write_text("hello")
    attachment = REGISTRY.get("attachment")
    await attachment.handler(ctx, str(a_file))

    exec_block, file_block = session.pending_user_attachments
    assert exec_block["type"] == "text" and "2" in exec_block["text"]
    assert file_block["path"] == str(a_file)


@pytest.mark.asyncio
async def test_returns_a_tuple_not_the_live_list(tmp_path):
    """Tier 2: accept -- the returned value is a genuine copy (a tuple),
    not the live list ``pending_user_images`` deliberately still exposes.
    A snapshot taken before a later write must not retroactively grow."""
    session = _session(tmp_path)
    ctx = slash_ctx(session)
    exec_attach = REGISTRY.get("exec-attach")
    await exec_attach.handler(ctx, 'python3 -c "print(1)"')

    snapshot = session.pending_user_attachments
    assert isinstance(snapshot, tuple)
    (only,) = snapshot

    await exec_attach.handler(ctx, 'python3 -c "print(2)"')
    (still_only,) = snapshot
    assert still_only is only, "a tuple snapshot must not observe a later write"
    first, second = session.pending_user_attachments
    assert first is only, "the earlier block is unchanged"
    assert "2" in second["text"], "but a FRESH read must see the new one"
