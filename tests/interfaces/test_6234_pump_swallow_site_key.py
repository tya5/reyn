"""Tier 2: #6234 (architect ruling, issuecomment-5772189312) — the
``PumpSwallowStats``/``_record_pump_swallow`` dedup key widened from
``(kind, exception type)`` to ``(site, exception type)`` when 18 more
``_pump_frames`` ``except`` blocks (turn-end cleanup, the two
chrome-refresh guards, the queue-view seed, every EVENT-frame handler)
were routed through the same counter. Most of those newly-routed sites
have NO frame at all, so they have no ``kind`` to key on — the architect's
own ruling names the exact failure a placeholder key would cause: "N は
増えるのに *どこで* が消えます" (N grows, but WHERE disappears) — every
site sharing the placeholder collapses into one dedup bucket, and
whichever fires first silently suppresses the audit-event for every
other site sharing it.

Required witness pair (architect ⑤, same PR): DENY (a genuine collision —
the SAME site, twice, must still dedup to one event — bounded stays alive)
and PRESENT (two DIFFERENT sites must NOT collide — the class of defect
this widening exists to prevent). Neither alone distinguishes "the key
dedups correctly" from "the key dedups everything" or "the key dedups
nothing" — see this file's own two tests below.

Real ``PumpSwallowStats`` + real ``TextualChatApp._record_pump_swallow``,
real ``emit_cli_event`` + a real ``.reyn/events`` read-back — no mocks,
mirrors ``test_5732_pump_swallow_visibility.py``'s own idiom.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from reyn.interfaces.inline.textual_chat.app import PumpSwallowStats, TextualChatApp


def _read_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    found: "list[dict]" = []
    if not events_dir.exists():
        return found
    for path in events_dir.rglob("*.jsonl"):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("type") == kind:
                found.append(rec)
    return found


def test_stats_record_dedups_the_same_site_and_exception_type() -> None:
    """Tier 2: unit-level PRESENT-of-bound witness — ``PumpSwallowStats.
    record`` keys on ``(site, exception type)``: the identical pair,
    repeated, returns True exactly once.

    Strip-falsified in-file (Edit only, ``if key in self._seen:``
    temporarily replaced with ``if False:``): RED with ``AssertionError:
    assert True is False``. Restored, confirmed GREEN."""
    stats = PumpSwallowStats()
    first = stats.record("_seed_queue_view", ValueError("boom"))
    second = stats.record("_seed_queue_view", ValueError("boom again"))
    assert first is True
    assert second is False


def test_stats_record_does_not_dedup_two_different_sites() -> None:
    """Tier 2: unit-level DENY-of-collapse witness, required alongside the
    test above — two DIFFERENT static-literal sites (the shape #6234's
    widening introduced: turn-end cleanup steps, frame-unrelated guards,
    none of which carry a ``kind``) sharing the SAME exception type must
    each get their own first-occurrence True. If the key collapsed to
    exception-type alone (the failure a missing/placeholder ``site`` would
    cause), this would incorrectly return False for the second call.

    Strip-falsified in-file (Edit only, ``key = (site, type(exc).__name__)``
    temporarily replaced with ``key = (type(exc).__name__,)``): RED with
    ``AssertionError: two distinct call sites with the same exception type
    must not collapse into one dedup bucket / assert False is True``.
    Restored, confirmed GREEN."""
    stats = PumpSwallowStats()
    first = stats.record("_turn_end_activity_clear", ValueError("boom"))
    second = stats.record("_apply_compact_layout", ValueError("boom"))
    assert first is True
    assert second is True, (
        "two distinct call sites with the same exception type must not "
        "collapse into one dedup bucket"
    )


def test_record_pump_swallow_end_to_end_present_two_sites_two_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: end-to-end PRESENT witness — two call sites that carry NO
    frame at all (a #6234-widened shape: no ``kind`` to pass), same
    exception type, must each durably record their OWN
    ``pump_exception_swallowed`` audit-event with a distinct ``site`` in
    the payload. This is the actual regression #6234's ruling guards
    against: a shared placeholder key would produce exactly ONE event
    here, silently hiding which of the two sites is broken.

    Strip-falsified in-file (Edit only, same key change as the unit-level
    test above): RED with ``AssertionError: two distinct sites must each
    produce their own durable event, not collapse into one shared bucket
    -- got [...one event, site='_turn_end_activity_clear'...] / assert
    {'_turn_end_activity_clear'} == {'_apply_compact_layout',
    '_turn_end_activity_clear'}``. Restored, confirmed GREEN."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
    from reyn.interfaces.transport.client_transport import ClientTransportStub

    class _StubReadModel(ChatReadModel):
        @property
        def capabilities(self):
            return LOCAL_CHAT_READ_CAPABILITIES

        def snapshot(self, config=None):
            return {"model_active_class": "opus"}

        def intervention_head(self):
            return None

        def pending_command_ui(self):
            return None

        def clear_pending_command_ui(self) -> None:
            return None

        @property
        def has_command_ui_region(self) -> bool:
            return True

        @property
        def history_path(self):
            return Path("/tmp/reyn_6234_history")

        def conversation_history(self, *, limit=None):
            return []

        def load_older_conversation_history(self, *, agent=None, session_id=None):
            return 0

    class _StubTransport(ClientTransportStub):
        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def has_session(self) -> bool:
            return True

        def attach_failed(self) -> bool:
            return False

        async def frames(self):
            import asyncio
            await asyncio.Event().wait()
            return
            yield  # pragma: no cover

        async def submit_user_text(self, text: str, *, client_ref=None) -> str:
            return "msg-1"

        async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
            return False

        async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
            return False

        def pending_intervention_head(self):
            return None

        def put_display(self, msg) -> None:
            pass

        async def cancel_inflight(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    app = TextualChatApp(transport=_StubTransport(), read_model=_StubReadModel())

    # Two of the #6234-widened, frame-unrelated call sites -- same
    # exception type, DIFFERENT sites.
    app._record_pump_swallow("_turn_end_activity_clear", ValueError("boom"))
    app._record_pump_swallow("_apply_compact_layout", ValueError("boom"))

    events = _read_events_of_kind(reyn_dir / "events", "pump_exception_swallowed")
    sites = {event["data"]["site"] for event in events}
    assert sites == {"_turn_end_activity_clear", "_apply_compact_layout"}, (
        "two distinct sites must each produce their own durable event, not "
        f"collapse into one shared bucket -- got {events!r}"
    )
    for event in events:
        assert "frame_kind" not in event["data"], (
            "a site with no frame must never populate frame_kind -- "
            f"got {event!r}"
        )


def test_record_pump_swallow_end_to_end_deny_same_site_twice_is_one_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: end-to-end DENY witness, the required pair to the PRESENT
    test above — the SAME site, failing repeatedly with the same exception
    type (the #5731 shape: a broken call site fails every frame), must
    still durably record exactly ONE event. Without this pair, the PRESENT
    test alone could not distinguish "the key dedups correctly" from "the
    key dedups nothing at all" (which would also make two DIFFERENT sites
    produce two events, but for the wrong reason).

    Strip-falsified in-file (Edit only, ``first_occurrence = self.
    _pump_swallow_stats.record(site, exc)`` temporarily replaced with
    ``first_occurrence = True``): RED with ``ValueError: too many values
    to unpack (expected 1)`` at the ``[event] = events`` line (5 events
    landed instead of 1). Restored, confirmed GREEN."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    from reyn.interfaces.repl.read_model import LOCAL_CHAT_READ_CAPABILITIES, ChatReadModel
    from reyn.interfaces.transport.client_transport import ClientTransportStub

    class _StubReadModel(ChatReadModel):
        @property
        def capabilities(self):
            return LOCAL_CHAT_READ_CAPABILITIES

        def snapshot(self, config=None):
            return {"model_active_class": "opus"}

        def intervention_head(self):
            return None

        def pending_command_ui(self):
            return None

        def clear_pending_command_ui(self) -> None:
            return None

        @property
        def has_command_ui_region(self) -> bool:
            return True

        @property
        def history_path(self):
            return Path("/tmp/reyn_6234_history_b")

        def conversation_history(self, *, limit=None):
            return []

        def load_older_conversation_history(self, *, agent=None, session_id=None):
            return 0

    class _StubTransport(ClientTransportStub):
        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

        def has_session(self) -> bool:
            return True

        def attach_failed(self) -> bool:
            return False

        async def frames(self):
            import asyncio
            await asyncio.Event().wait()
            return
            yield  # pragma: no cover

        async def submit_user_text(self, text: str, *, client_ref=None) -> str:
            return "msg-1"

        async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:
            return False

        async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:
            return False

        def pending_intervention_head(self):
            return None

        def put_display(self, msg) -> None:
            pass

        async def cancel_inflight(self) -> None:
            pass

        async def shutdown(self) -> None:
            pass

    app = TextualChatApp(transport=_StubTransport(), read_model=_StubReadModel())
    stats = PumpSwallowStats()
    app._pump_swallow_stats = stats  # mirrors test_5732's own app._stray_output_stats wiring

    for _ in range(5):
        app._record_pump_swallow("_turn_end_activity_clear", ValueError("boom"))

    events = _read_events_of_kind(reyn_dir / "events", "pump_exception_swallowed")
    [event] = events  # exactly one event captured — unpack raises otherwise
    assert event["data"]["site"] == "_turn_end_activity_clear"
    assert stats.count == 5, (
        "the count must stay complete (every occurrence) even while the "
        "event population is bounded"
    )
