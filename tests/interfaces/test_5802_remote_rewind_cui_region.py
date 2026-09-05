"""Tier 2: #5802 — web/connect's ``/rewind`` gets the real picker, not just
the text list.

Owner-hit: ``web/connect`` で ``/rewind`` すると候補リストが会話画面に流れ
るだけで、選択パネルが出てこない ("only the text list streams into the chat
pane, the picker never appears"). Root cause (architect): ``REMOTE_CHAT_READ_
CAPABILITIES.pending_command_ui``/``.has_command_ui_region`` were declared
``False`` — a #5773 baseline disposition whose own comment claimed
"permanently session-local; no ``project_status`` twin is ever planned",
falsified by this report. ``RemoteReadModel`` now reads a real
``pending_command_ui_request`` key off the SAME STATE_SNAPSHOT/STATE_DELTA
channel every other remote-wired field (#5774, #5771) already rides.

Real ``project_status``/``StatusModel``/``RemoteReadModel`` throughout — no
mocks (mirrors #5774's own established pattern in this file's sibling).
"""
from __future__ import annotations

from reyn.interfaces.repl.read_model import REMOTE_CHAT_READ_CAPABILITIES, RemoteReadModel
from reyn.interfaces.transport.agui.client import AgUiTransport
from reyn.interfaces.transport.agui.state import StatusModel, project_status


def _local_snapshot(*, pending_command_ui_request=None) -> dict:
    """A LOCAL ``_snapshot()``-shaped dict carrying the one field this
    issue's own fix wires, mirroring ``status.py``'s real producer."""
    return {"pending_command_ui_request": pending_command_ui_request}


_REWIND_REQUEST = {
    "kind": "rewind",
    "points": [{"turn": 3, "text": "abc"}],
    "branches": [],
    "default_scope": {"turn": 3},
}


def test_project_status_carries_the_real_pending_request() -> None:
    """Tier 2: ``project_status`` emits the real dict, not a fixed None."""
    out = project_status(_local_snapshot(pending_command_ui_request=_REWIND_REQUEST))
    assert out["pending_command_ui_request"] == _REWIND_REQUEST


def test_project_status_stays_none_when_nothing_pending() -> None:
    """Tier 2: no fabricated placeholder — None stays None, never a stand-in
    dict, when no picker request is pending."""
    out = project_status(_local_snapshot(pending_command_ui_request=None))
    assert out["pending_command_ui_request"] is None


def test_delta_carries_the_field_only_on_open_and_close_not_every_frame() -> None:
    """Tier 2: architect's explicit cost/observability requirement — STATE_
    DELTA only carries CHANGED keys, so a picker open→same→close sequence
    must put ``pending_command_ui_request`` on the wire exactly twice (the
    None→dict open transition, the dict→None close transition), never on an
    unchanged in-between tick. A regression that re-sends the field every
    frame (e.g. a snapshot rebuilt with a fresh, `==`-equal-but-not-`is`-
    tracked dict on each tick) must turn this red."""
    model = StatusModel()

    # Connect-time snapshot: nothing pending yet.
    snap = model.snapshot(project_status(_local_snapshot(pending_command_ui_request=None)))
    assert snap["pending_command_ui_request"] is None

    # Open: a picker request appears — must ride this delta.
    opened = model.delta(project_status(_local_snapshot(pending_command_ui_request=_REWIND_REQUEST)))
    assert opened.get("pending_command_ui_request") == _REWIND_REQUEST

    # Unchanged tick (some OTHER field moves, this one doesn't): must NOT
    # reappear in the delta.
    unchanged_snapshot = _local_snapshot(pending_command_ui_request=_REWIND_REQUEST)
    still = model.delta(project_status(unchanged_snapshot))
    assert "pending_command_ui_request" not in still, (
        f"pending_command_ui_request rode an unchanged-value delta: {still}"
    )

    # A second unchanged tick, to rule out a one-tick-late suppression bug.
    still_again = model.delta(project_status(_local_snapshot(pending_command_ui_request=_REWIND_REQUEST)))
    assert "pending_command_ui_request" not in still_again

    # Close: request cleared — must ride this delta too.
    closed = model.delta(project_status(_local_snapshot(pending_command_ui_request=None)))
    assert "pending_command_ui_request" in closed
    assert closed["pending_command_ui_request"] is None


def test_remote_read_model_capabilities_declare_true_now() -> None:
    """Tier 2: the #5773 baseline's ``False``/``False`` disposition for
    these 2 flags is corrected — no longer "permanently session-local"."""
    assert REMOTE_CHAT_READ_CAPABILITIES.pending_command_ui is True
    assert REMOTE_CHAT_READ_CAPABILITIES.has_command_ui_region is True


def _real_transport() -> AgUiTransport:
    """A real :class:`AgUiTransport` (never exercised for its own frame
    stream here — only as the real collaborator :class:`RemoteReadModel` is
    constructed with, mirroring test_5050's own established pattern in this
    directory). ``.status`` is the real :class:`RemoteStatusView`."""

    async def _empty_lines():
        return
        yield  # pragma: no cover

    async def _send(_payload):
        return None

    return AgUiTransport(_empty_lines(), _send)


def test_remote_read_model_returns_the_real_pending_request() -> None:
    """Tier 2: end-to-end witness — ``RemoteReadModel.pending_command_ui()``
    returns the real wire value now, not an unconditional None."""
    transport = _real_transport()
    transport.status.apply_snapshot({"pending_command_ui_request": _REWIND_REQUEST})
    read_model = RemoteReadModel(transport)
    assert read_model.pending_command_ui() == _REWIND_REQUEST
    assert read_model.has_command_ui_region is True


def test_remote_read_model_returns_none_when_nothing_pending() -> None:
    """Tier 2: no fabricated placeholder on the read-model side either."""
    transport = _real_transport()
    transport.status.apply_snapshot({"pending_command_ui_request": None})
    read_model = RemoteReadModel(transport)
    assert read_model.pending_command_ui() is None


def test_remote_read_model_degrades_gracefully_for_a_pre_5802_server() -> None:
    """Tier 2: backward compat — a pre-#5802 server's STATE_SNAPSHOT never
    populated this key at all; the client must not KeyError."""
    transport = _real_transport()
    read_model = RemoteReadModel(transport)
    assert read_model.pending_command_ui() is None
