"""Tier 2: #6198 — the `_record_call_parent_collision` detector (landed
first, #6215) stays a general borrowed/reused-key witness after #6198's
own structural fix moved `_call_parents`'s key off `call_id`.

## Background (investigation stage, issue #6198, 0 observed instances)

`_call_parents` (`app.py`, #4691 Phase B) USED TO be keyed by `call_id`
— litellm's own response `id`, a THIRD PARTY's identifier
(`_response_call_id`'s own docstring, `llm.py`). Neither OpenAI's nor
litellm's own documentation states a uniqueness SCOPE (investigation,
issue thread). `_register_call_parent`'s own write was an UNCONDITIONAL
overwrite with no detector at all — the investigation's own closing
finding: "起きたと分かるか: 今日は分かりません" (would we know if it
happened? today, no). A collision would present ONLY as an owner-visible
symptom ("無関係な行が1つのgroupに束ねられる") — no log, no exception.
#6215 landed the detector this file exercises (below), deliberately
NOT changing the overwrite itself (issue thread, verbatim): "③ 上書き
そのものは今までどおり起きる（＝挙動を変えず、見えるようにするだけ）".

## #6198's own structural fix (THIS PR) — what changed under this file

The key moved to `_call_parent_key` (`turn:{chain_id}/round:
{round_index}`, reyn's own facts — see `app.py`'s own module-level
function) — `call_id` is no longer dict-key material anywhere in
`_call_parents`. The detector this file exercises (`_record_call_parent_
collision` / `call_parent_registration_collided`) is UNCHANGED code —
lead-coder's own framing: "検出器は`_call_parents`専用でなく借り物の鍵
一般の番人として残します" — so this file keeps exercising it, with its
own fixtures now supplying `round_index` (required for a row to
register at all post-fix) and its end-to-end scenarios keyed on the
ACTUAL collision axis now (`round_index`, not `call_id` — two rows
sharing a `call_id` no longer collide; two rows sharing a `round_index`
still would, though that should not happen for a real round — see
`_call_parent_key`'s own disclosed scope).

## Accept criteria (issue #6198, effect-based, unchanged from #6215)

① a repeated key durably records the fact (a `call_parent_
   registration_collided` audit-event, the SAME `emit_cli_event` +
   once-per-distinct-key-bounded shape #5732's own
   `_record_pump_swallow`/`PumpSwallowStats` established for the
   identical charter concern).
② the normal case (distinct keys) emits nothing — the deny-side
   sibling: without it, "always emit on registration" would pass ①
   trivially.
③ the overwrite itself is unchanged: `_call_parents[key]` ends up
   pointing at the SECOND (latest) entry, exactly as it did before this
   stage — a strip-falsify witness that a future "fix" here (dropping
   the second row, raising) would be caught, not silently accepted.

Real `TextualChatApp`, real `emit_cli_event`, real `.reyn/events`
read-back — mirrors `test_5732_pump_swallow_visibility.py`'s own idiom
for the identical class of witness (a real regression, not a mock).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator

import pytest
from textual_flowview import FlowView

from reyn.interfaces.inline.textual_chat import TextualChatApp
from reyn.interfaces.transport.client_transport import ClientTransportStub
from reyn.interfaces.transport.frames import DisplayFrame
from reyn.runtime.outbox import OutboxMessage


def _parent_row(
    call_id: str, *, round_index: int, text: str = "",
    dispatched_tool_calls: "bool | int" = 2,
) -> OutboxMessage:
    """The tool-turn-text row (#4691 ③'s own placeholder) — the SAME
    shape ``test_4691_phase_b_group_construction.py``'s own
    ``_parent_row`` uses, kept local per that file's own convention
    (neither module exports its collaborators). ``round_index`` is now
    REQUIRED, no default (#6198's structural fix — a row with no
    ``round_index`` never registers at all, see ``_call_parent_key``) —
    every caller in this file must say which round it means, since that
    is the actual collision axis now, not ``call_id``. The default
    ``dispatched_tool_calls=2`` (#6184 段4-B) is incidental to this
    file's own subject."""
    return OutboxMessage(
        kind="agent",
        text=text,
        meta={
            "chain_id": "chain-test",
            "source": "router_tool_turn_text",
            "call_id": call_id,
            "round_index": round_index,
            "finish_reason": "tool_calls",
            "dispatched_tool_calls": dispatched_tool_calls,
            "prompt_tokens": 100,
            "completion_tokens": 5,
        },
    )


class QueueTransport(ClientTransportStub):
    """A real, minimal :class:`ClientTransport` fed one frame at a time —
    same shape as the sibling #4691 test files' own local copy (neither
    module exports its collaborator)."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[object]" = asyncio.Queue()

    async def push_display(self, msg: OutboxMessage) -> None:
        await self._queue.put(DisplayFrame(msg))

    def start(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    async def frames(self) -> "AsyncIterator[object]":
        while True:
            yield await self._queue.get()

    async def submit_user_text(self, text: str, *, client_ref: "str | None" = None) -> str:  # pragma: no cover
        return "msg-1"

    async def answer_intervention_text(self, text: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    async def answer_intervention_choice(self, choice_id: str, *, intervention_id=None) -> bool:  # pragma: no cover
        return False

    def has_session(self) -> bool:
        return True

    def pending_intervention_head(self) -> "object | None":
        return None

    def put_display(self, msg: "OutboxMessage") -> None:  # pragma: no cover
        pass

    async def cancel_inflight(self) -> None:  # pragma: no cover - trivial
        pass

    async def shutdown(self) -> None:  # pragma: no cover - trivial
        pass


def _read_events_of_kind(events_dir: Path, kind: str) -> "list[dict]":
    """Mirrors ``test_5732_pump_swallow_visibility.py``'s own helper
    exactly — kept local per that file's own established convention."""
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


# ---------------------------------------------------------------------------
# _record_call_parent_collision — unit level (real App, no pump needed)
# ---------------------------------------------------------------------------


def test_record_call_parent_collision_emits_a_real_event_with_no_parameter_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: direct unit witness for the method itself — real
    ``emit_cli_event``, real ``.reyn/events`` read-back (the SAME
    ``kind=`` parameter-collision hazard ``_record_pump_swallow``'s own
    docstring names — this method makes the identical choice)."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=QueueTransport())
    app._record_call_parent_collision("resp-1")

    events = _read_events_of_kind(reyn_dir / "events", "call_parent_registration_collided")
    [event] = events
    assert event["data"]["call_id"] == "resp-1"


def test_record_call_parent_collision_emits_once_for_a_repeated_call_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept① 's own bound — the SAME ``call_id`` colliding
    repeatedly (a provider that reused one id across many rounds) must
    durably record exactly ONE event, not one per repeat, the same
    charter concern (#5732: "who bounds this if it repeats") the
    ``PumpSwallowStats`` precedent this method's own docstring cites
    was built to answer."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=QueueTransport())
    for _ in range(5):
        app._record_call_parent_collision("resp-1")

    events = _read_events_of_kind(reyn_dir / "events", "call_parent_registration_collided")
    [event] = events  # exactly one event captured -- unpack raises otherwise
    assert event["data"]["call_id"] == "resp-1"


def test_record_call_parent_collision_emits_separately_for_a_different_call_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: a DIFFERENT ``call_id`` colliding is its own, separate
    fact — not folded into an already-seen key's own bound."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    app = TextualChatApp(transport=QueueTransport())
    app._record_call_parent_collision("resp-1")
    app._record_call_parent_collision("resp-2")

    events = _read_events_of_kind(reyn_dir / "events", "call_parent_registration_collided")
    [event_a, event_b] = events  # exactly two events -- unpack raises otherwise
    assert {event_a["data"]["call_id"], event_b["data"]["call_id"]} == {"resp-1", "resp-2"}


# ---------------------------------------------------------------------------
# _register_call_parent — end to end, real frames through a real pilot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_repeated_round_key_at_registration_records_the_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept① end to end — the PR's own "body", updated for
    #6198's structural fix. Two ``kind="agent"`` rows sharing the SAME
    ``(chain_id, round_index)`` (the axis that actually collides now —
    ``call_id`` deliberately DIFFERS here, to prove it is no longer
    key material at all) must durably record the collision. Strip-
    falsify witness: reverting :meth:`TextualChatApp._register_call_
    parent`'s own collision check (or its call to :meth:`_record_call_
    parent_collision`) turns this red."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", round_index=1))
        await pilot.pause()
        await transport.push_display(_parent_row("resp-2", round_index=1))
        await pilot.pause()

    events = _read_events_of_kind(reyn_dir / "events", "call_parent_registration_collided")
    [event] = events
    assert event["data"]["call_id"] == "turn:chain-test/round:1"


@pytest.mark.asyncio
async def test_distinct_round_indexes_at_registration_record_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tier 2: accept② — the deny-side sibling. Two ordinary rounds with
    DIFFERENT ``round_index``es (the overwhelmingly common real shape —
    ``call_id`` is deliberately the SAME here, to prove IT no longer
    drives collision either way) must emit NO collision event at all —
    without this, an implementation that emits unconditionally on every
    registration would still pass the test above."""
    reyn_dir = tmp_path / ".reyn"
    reyn_dir.mkdir()
    monkeypatch.chdir(tmp_path)

    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", round_index=1))
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", round_index=2))
        await pilot.pause()

    events = _read_events_of_kind(reyn_dir / "events", "call_parent_registration_collided")
    assert events == []


@pytest.mark.asyncio
async def test_the_overwrite_itself_is_unchanged_the_latest_entry_wins() -> None:
    """Tier 2: accept③ — detection only, never a behavior change. A
    repeated round key still ends up pointing ``_call_parents`` at the
    SECOND (latest) ``Entry``, exactly as the unconditional overwrite
    did before this stage.

    The REAL shape this produces (verified, not assumed): the SECOND
    ``kind="agent"`` row itself nests under the FIRST via
    :meth:`_resolve_append_parent`'s own ① rule (a round-key lookup
    fires before a NEW registration exists to distinguish it from an
    ordinary same-round nesting) — so "second" is already
    ``first.children[0]`` BEFORE the overwrite even happens. This is
    exactly #6198's own named symptom, reproduced structurally: a
    later, unrelated row (here, the tool row) is proven to land under
    the SECOND (nested) entry — via the PUBLIC effect (nesting), not by
    reading ``_call_parents`` directly (testing.md Tier 4). A future
    "fix" that dropped the second row, kept the first, or raised would
    turn this red. ``call_id`` is deliberately the SAME on both rows
    (harmless, no longer key material) — ``round_index`` is what makes
    them collide now."""
    transport = QueueTransport()
    app = TextualChatApp(transport=transport, clock=lambda: 100.0)
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", round_index=1, text="first"))
        await pilot.pause()
        await transport.push_display(_parent_row("resp-1", round_index=1, text="second"))
        await pilot.pause()

        flow = app.query_one(FlowView)
        (first_entry,) = [e for e in flow.entries if e.item.text == "first"]
        (second_entry,) = first_entry.children
        assert second_entry.item.text == "second", (
            "setup: the second same-round-key row must nest under the "
            "first -- the ① round-key lookup in _resolve_append_parent "
            "fires before this stage's own detector exists"
        )

        started = OutboxMessage(
            kind="tool_call_started",
            text="grep",
            meta={
                "tool": "grep", "op_id": "op-1", "dispatch_id": "op-1",
                "args": {}, "call_id": "resp-1",
                "chain_id": "chain-test", "round_index": 1,
            },
        )
        await transport.push_display(started)
        await pilot.pause()

        (child,) = second_entry.children
        assert child.item.kind == "tool_call_started", (
            "a later same-round-key tool row must nest under the SECOND "
            "(latest, nested) registered parent -- the overwrite must "
            "still win, unchanged by this stage's own detector"
        )
