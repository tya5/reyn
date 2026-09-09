"""Tier 1: #5989 ③ (lead-coder review, self-correction) — ``RemoteQueueView``
(``src/reyn/interfaces/transport/agui/state.py``) had ZERO ``logger`` calls
of its own. ``apply_user_submitted``'s own rejection is covered by its
CALLER (``TextualChatApp._handle_user_submitted_event``, already always
``logger.warning`` — a ratified, tested decision, see
``test_5886_own_client_ref_rejection_warns.py``, deliberately NOT touched
here) — but ``apply_turn_started`` and ``apply_inbox_cancel`` had no
observer anywhere: a wrongly-rejected dispatch/cancel delta was exactly as
silent as the #5989 owner-hit's original enqueue-side bug, just one layer
over.

Discriminator (lead-coder: "誤って拒否した／正しく拒否した を区別できる形
に、ただし判別子は実装者判断"): a rejection is EXPECTED (a genuine replay —
this exact delta was already applied, its target item is already gone from
:attr:`RemoteQueueView.items`) — logged at ``DEBUG``, no operational-log
noise for the routine case. It is SUSPICIOUS (the item this delta would
have promoted/removed is STILL present, un-promoted/un-cancelled — the
exact shape a wrongly-rejected delta leaves behind) — logged at
``WARNING``. Both directions are required (lead-coder's own accept
criteria): ① a wrong rejection reaches WARNING ② a correct rejection does
NOT flood the operational log at WARNING.

Real ``RemoteQueueView`` throughout, seeded via its own public
``apply_snapshot`` (never a private ``_last_seq`` poke) — no mocks, this
class has no collaborator to fake.
"""
from __future__ import annotations

import pytest

from reyn.interfaces.transport.agui.state import RemoteQueueView

_LOGGER = "reyn.interfaces.transport.agui.state"


def _levels(caplog: pytest.LogCaptureFixture) -> "list[str]":
    return [r.levelname for r in caplog.records if r.name == _LOGGER]


# ---------------------------------------------------------------------------
# apply_turn_started
# ---------------------------------------------------------------------------


def test_turn_started_rejection_of_a_still_queued_item_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: a ``turn_started`` delta rejected by the seq-gate while its
    own target item is STILL sitting in ``items``, unpromoted — the shape a
    WRONG rejection leaves behind. Must warn.

    Strip-falsifier (verified by hand: the ``stuck`` check replaced with a
    constant ``False``, so this branch is unconditionally logged at
    ``debug``): this test goes red — no ``WARNING`` record."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )
    caplog.set_level("DEBUG", logger=_LOGGER)

    applied = view.apply_turn_started(chain_id="c1", seq=3)  # stale: 3 <= 5

    assert applied is False
    assert "m1" in view.items, "the item must still be there — this IS the property under test"
    assert _levels(caplog) == ["WARNING"], (
        f"#5989 REGRESSION: a turn_started rejection whose target item is "
        f"still queued must warn — got {_levels(caplog)!r}"
    )


def test_turn_started_rejection_of_an_already_promoted_item_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: the SAME losing seq race, but the target item is already
    gone (a genuine, harmless replay of a dispatch already applied) — must
    NOT warn, only debug-log, so an operational log isn't flooded by the
    routine, correct case.

    Strip-falsifier (verified by hand: ``stuck`` forced to ``True``
    unconditionally): this test goes red — a spurious ``WARNING`` appears
    for the routine, already-resolved case."""
    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=5)
    caplog.set_level("DEBUG", logger=_LOGGER)

    applied = view.apply_turn_started(chain_id="c1", seq=3)  # stale: 3 <= 5

    assert applied is False
    assert _levels(caplog) == ["DEBUG"], (
        f"#5989 REGRESSION: a routine, already-resolved rejection must stay "
        f"traceable at DEBUG and never escalate to WARNING — got "
        f"{_levels(caplog)!r}"
    )


# ---------------------------------------------------------------------------
# apply_inbox_cancel
# ---------------------------------------------------------------------------


def test_inbox_cancel_rejection_of_a_still_queued_item_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: the SAME discriminator, through ``apply_inbox_cancel``: the
    cancel's own target ``msg_id`` is STILL present — a wrongly-rejected
    cancel would leave the operator's own cancel silently ignored. Must
    warn."""
    view = RemoteQueueView()
    view.apply_snapshot(
        queue=[{"msg_id": "m1", "chain_id": "c1", "text": "hi"}],
        turn_active=False, queue_seq=5,
    )
    caplog.set_level("DEBUG", logger=_LOGGER)

    applied = view.apply_inbox_cancel(msg_id="m1", seq=3)  # stale: 3 <= 5

    assert applied is False
    assert "m1" in view.items
    assert _levels(caplog) == ["WARNING"], (
        f"#5989 REGRESSION: an inbox_cancel rejection whose target item is "
        f"still queued must warn — got {_levels(caplog)!r}"
    )


def test_inbox_cancel_rejection_of_an_already_removed_item_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tier 1: the routine case: the cancel's target is already gone (a
    genuine replay of a cancel already applied, or the item was already
    dispatched — #3300 §6a's exclusivity) — must not warn."""
    view = RemoteQueueView()
    view.apply_snapshot(queue=[], turn_active=False, queue_seq=5)
    caplog.set_level("DEBUG", logger=_LOGGER)

    applied = view.apply_inbox_cancel(msg_id="m1", seq=3)  # stale: 3 <= 5

    assert applied is False
    assert _levels(caplog) == ["DEBUG"], (
        f"#5989 REGRESSION: a routine, already-resolved cancel rejection "
        f"must stay traceable at DEBUG and never escalate to WARNING — got "
        f"{_levels(caplog)!r}"
    )
