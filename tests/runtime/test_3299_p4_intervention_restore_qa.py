"""#3299 P4: a restored answered intervention shows the question AND the
answer as ONE self-contained flow entry.

**Design (owner-ratified in the issue thread — do not re-litigate).**
``InterventionHandler.announce`` publishes an intervention's PROMPT only to
the outbox; it never appends to history. So before this PR, only the ANSWER
half existed in ``history.jsonl`` — there was, and is, no separate "question"
record for a restore projection to correlate the answer with (unlike the
#3295 tool call/result coalesce, which really does have two records). The
approved fix (C) is NOT correlation: ``InterventionHandler.deliver_answer_to``
now folds the prompt (+ optional detail, + the resolved answer-display text)
onto the SAME answer record it already appends
(``INTERVENTION_PROMPT_META_KEY`` / ``INTERVENTION_DETAIL_META_KEY`` /
``INTERVENTION_ANSWER_META_KEY``, :mod:`reyn.runtime.chat_message`) — one
history entry is already self-contained, so restore needs no correlation key
at all (the #3287 / #3299 P2 "guessed key" defect class this arc hit twice).

The persisted values are RAW (untrusted / LLM-derived — ``ask_user``
prompts/suggestions and a matched choice's label all come from a model
tool-call); neutralization happens ONLY at the display boundary
(``ReynPresenter._present_intervention_pending``), never at persist time.

Gates (mirroring the issue's gate list, each strip-falsified independently):

1. One flow entry per answered intervention (not two).
2. Both Q and A are present in that entry's rendered content.
3. The LLM payload is byte-identical whether or not the new meta keys are
   present (``RouterHistoryBuffer.build_history`` never copies arbitrary
   ``meta`` into the wire dict).
4. Correct Q/A pairing when several interventions are answered OUT OF ORDER
   (P5's tabbed panel allows this) — each restored entry shows its OWN
   question, never another's (self-contained records make mis-pairing
   structurally impossible, no correlation step to get wrong).
5. An unanswered intervention leaves ZERO trace in the restored flow — the
   *specified* behavior (``announce`` never writes history), pinned so a
   future change cannot start fabricating a half-entry.
6. Restore-boundary neutralization — raw ESC/OSC in the persisted prompt /
   detail / answer must not reach the rendered entry. Two independent call
   sites (``_intervention_head`` for prompt/detail, the resolved-line
   neutralize for the answer) — each has its own falsification note below.

All tests use real ``ChatMessage`` / ``OutboxMessage`` / ``ReynPresenter``
instances (module 1-6) plus a real, fully-wired ``InterventionHandler`` +
``InterventionRegistry`` + ``SnapshotJournal`` (module 7 — the producer-side
round trip proving the fixture the projection tests assume actually matches
what the real handler writes) — no mocks, per the testing policy.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.console import Console

from reyn.core.events.event_store import EventStore
from reyn.core.events.events import EventLog
from reyn.core.events.state_log import StateLog
from reyn.interfaces.inline.textual_chat.presenter import ReynPresenter
from reyn.interfaces.inline.textual_chat.restore import project_restored_frames
from reyn.runtime.chat_message import (
    INTERVENTION_ANSWER_META_KEY,
    INTERVENTION_DETAIL_META_KEY,
    INTERVENTION_PROMPT_META_KEY,
    ChatMessage,
)
from reyn.runtime.outbox import OutboxMessage
from reyn.runtime.services.intervention_handler import InterventionHandler
from reyn.runtime.services.intervention_registry import InterventionRegistry
from reyn.runtime.services.snapshot_journal import SnapshotJournal
from reyn.user_intervention import InterventionChoice, UserIntervention
from tests._support.session import make_session as _make_router_session

_ESC_OSC_PAYLOAD = "\x1b[31mRED\x1b]0;pwn\x07"


def _render(msg: OutboxMessage) -> str:
    """Render ``msg`` through the SAME presenter path a live/restored
    intervention entry uses, returning the plain rendered text."""
    presentation = ReynPresenter()._present_intervention_pending(msg, 80)
    console = Console(width=80, no_color=True)
    with console.capture() as cap:
        console.print(presentation.renderable)
    return cap.get()


def _answered_message(
    *, iv_id: str, prompt: str, answer: str, detail: str = "",
) -> ChatMessage:
    """A ``role='user'`` history entry shaped exactly like
    ``InterventionHandler.deliver_answer_to``'s append (module docstring)."""
    meta = {
        "answered_actor": "demo",
        "answered_run_id": "rA",
        "intervention_id": iv_id,
        "intervention_kind": "ask_user",
        INTERVENTION_PROMPT_META_KEY: prompt,
        INTERVENTION_ANSWER_META_KEY: answer,
    }
    if detail:
        meta[INTERVENTION_DETAIL_META_KEY] = detail
    # A choice-selected answer's OWN content is "" (deliver_answer_to passes
    # text="" through the choice-id-override path) — modeled here too so the
    # fixture matches BOTH the free-text and closed-set producer shapes.
    return ChatMessage(role="user", content="", meta=meta)


# ── Gate 1 + 2: one entry, both Q and A present ──────────────────────────────


def test_restored_answered_intervention_is_one_entry_with_both_qa() -> None:
    """Tier 1: a single answered-intervention history record projects to
    EXACTLY ONE ``kind="intervention_resolved"`` frame (#5057 axis B — never
    two, never a bare ``kind="user"`` echo of the empty answer text) whose
    rendered content contains BOTH the question and the answer.

    NON-VACUITY: before this PR, ``project_restored_frames`` had no
    ``INTERVENTION_PROMPT_META_KEY`` branch at all — this message would have
    fallen through to the plain ``role == "user"`` case and rendered as an
    EMPTY row (``m.text == ""`` for a choice-style answer), losing BOTH the
    question and the answer. Verified locally by reverting the ``if prompt:``
    branch in ``restore.py``: the assertions below flip RED (no
    ``kind="intervention_resolved"`` frame is produced at all)."""
    msgs = [_answered_message(iv_id="iv-1", prompt="Proceed with deploy?", answer="Yes")]
    frames = [f for f in project_restored_frames(msgs) if f.kind != "system"]
    assert [f.kind for f in frames] == ["intervention_resolved"], (
        f"expected exactly one intervention entry, got kinds={[f.kind for f in frames]}"
    )

    rendered = _render(frames[0])
    assert "Proceed with deploy?" in rendered, f"question missing from render: {rendered!r}"
    assert "Yes" in rendered, f"answer missing from render: {rendered!r}"


def test_restored_free_text_answer_also_one_entry_with_both_qa() -> None:
    """Tier 1: the free-text answer path (non-empty ``content``) is ALSO
    folded into one entry — the new meta keys are read even though
    ``m.text`` itself is non-empty for this path (the branch is keyed on
    ``INTERVENTION_PROMPT_META_KEY`` presence, not on whether ``m.text`` is
    empty)."""
    msg = ChatMessage(
        role="user",
        content="Tokyo",
        meta={
            "intervention_id": "iv-2",
            "intervention_kind": "ask_user",
            INTERVENTION_PROMPT_META_KEY: "Which city?",
            INTERVENTION_ANSWER_META_KEY: "Tokyo",
        },
    )
    frames = [f for f in project_restored_frames([msg]) if f.kind != "system"]
    assert [f.kind for f in frames] == ["intervention_resolved"], (
        f"expected exactly one intervention entry, got kinds={[f.kind for f in frames]}"
    )
    rendered = _render(frames[0])
    assert "Which city?" in rendered
    assert "Tokyo" in rendered


# ── Gate 3: LLM payload unchanged ────────────────────────────────────────────


def test_llm_payload_unchanged_by_new_intervention_meta_keys(tmp_path: Path, monkeypatch) -> None:
    """Tier 2: ``RouterHistoryBuffer.build_history`` (the actual wire-dict
    builder RouterLoop sends to the provider) produces a BYTE-IDENTICAL
    result whether or not the history carries the new
    ``INTERVENTION_PROMPT_META_KEY`` / ``INTERVENTION_DETAIL_META_KEY`` /
    ``INTERVENTION_ANSWER_META_KEY`` meta — the owner's cost/context
    invariant this whole design hinges on.

    NON-VACUITY (verified locally): temporarily making
    ``RouterHistoryBuffer._serialise_turn`` copy ``m.meta`` wholesale into the
    wire dict (``msg["meta"] = m.meta``) flips this RED — the enriched
    history's wire dicts then differ from the baseline's. Reverted after
    confirming RED; the shipped code never does this."""
    bare_meta = {
        "answered_actor": "demo", "answered_run_id": "rA",
        "intervention_id": "iv-1", "intervention_kind": "ask_user",
    }
    enriched_meta = {
        **bare_meta,
        INTERVENTION_PROMPT_META_KEY: "Proceed with deploy?",
        INTERVENTION_DETAIL_META_KEY: "irreversible",
        INTERVENTION_ANSWER_META_KEY: "Yes",
    }

    session_bare = _make_router_session(tmp_path / "bare", monkeypatch=monkeypatch)
    session_bare.history.append(ChatMessage(role="user", content="", meta=bare_meta))
    session_bare.history.append(ChatMessage(role="assistant", content="Deploying now."))
    bare_payload = session_bare._history_buffer.build_history()

    session_enriched = _make_router_session(tmp_path / "enriched", monkeypatch=monkeypatch)
    session_enriched.history.append(ChatMessage(role="user", content="", meta=enriched_meta))
    session_enriched.history.append(ChatMessage(role="assistant", content="Deploying now."))
    enriched_payload = session_enriched._history_buffer.build_history()

    assert bare_payload == enriched_payload, (
        "LLM wire payload must be byte-identical regardless of the new "
        f"intervention meta keys; bare={bare_payload!r} enriched={enriched_payload!r}"
    )


# ── Gate 4: correct pairing under out-of-order answers ───────────────────────


def test_out_of_order_answers_each_restore_with_their_own_question() -> None:
    """Tier 1: P5's tabbed panel lets interventions be answered in ANY order.
    Persist two dispatched interventions' answer records in REVERSE order
    (B answered before A, i.e. B's history entry comes first) and confirm
    each restored entry shows its OWN question, never the other's — the
    self-contained-record design makes mis-pairing structurally impossible
    (there is no correlation step to get wrong)."""
    msgs = [
        _answered_message(iv_id="iv-B", prompt="Delete branch B?", answer="No"),
        _answered_message(iv_id="iv-A", prompt="Delete branch A?", answer="Yes"),
    ]
    frames = [f for f in project_restored_frames(msgs) if f.kind != "system"]
    assert [f.kind for f in frames] == ["intervention_resolved", "intervention_resolved"], (
        f"expected exactly two intervention entries, got kinds={[f.kind for f in frames]}"
    )
    first, second = (_render(f) for f in frames)
    assert "Delete branch B?" in first and "No" in first
    assert "Delete branch A?" in second and "Yes" in second
    # Cross-contamination would show up as the WRONG pairing:
    assert "Delete branch A?" not in first
    assert "Delete branch B?" not in second


# ── Gate 5: unanswered intervention leaves zero trace ────────────────────────


def test_unanswered_intervention_has_no_restored_trace() -> None:
    """Tier 1: an intervention that was announced but never answered has NO
    history entry at all (``InterventionHandler.announce`` never appends to
    history) — so the restore projection shows nothing for it. This is the
    SPECIFIED behavior (pinned here so a future change cannot start
    fabricating a half-entry for it), not an accidental gap. Only the
    surrounding, unrelated conversation turns are restored."""
    msgs = [
        ChatMessage(role="user", content="before"),
        ChatMessage(role="assistant", content="ack"),
        # No entry at all for the never-answered intervention.
        ChatMessage(role="user", content="after"),
    ]
    frames = [f for f in project_restored_frames(msgs) if f.kind != "system"]
    kinds = [f.kind for f in frames]
    assert "intervention_resolved" not in kinds
    assert kinds == ["user", "agent", "user"]


def test_pre_p4_answer_record_without_prompt_meta_falls_back_gracefully() -> None:
    """Tier 1: backward compat — a PRE-P4 persisted answer record (has
    ``intervention_id``/``intervention_kind`` but none of the new
    ``INTERVENTION_*_META_KEY`` keys, since it predates this PR) must not
    crash and must not fabricate a question. It falls through to the
    pre-existing plain ``user`` projection (empty text → no frame at all,
    identical to today's existing behavior for a closed-set answer)."""
    msg = ChatMessage(
        role="user", content="",
        meta={
            "answered_actor": "demo", "answered_run_id": "rA",
            "intervention_id": "iv-old", "intervention_kind": "ask_user",
        },
    )
    frames = [f for f in project_restored_frames([msg]) if f.kind != "system"]
    assert frames == []


# ── Gate 6: restore-boundary neutralization (two independent sites) ─────────


def test_restore_prompt_and_detail_neutralized_at_render() -> None:
    """Tier 2c: a RAW ESC/OSC payload in the persisted prompt/detail must not
    reach the rendered entry — neutralized at ``_intervention_head``'s call
    site (``presenter.py``), the SAME site the live panel's prompt/detail
    rendering already uses (#2770).

    NON-VACUITY: verified locally by reverting ONLY ``_intervention_head``'s
    ``_neutralized_label`` calls around ``prompt``/``detail`` — the raw
    ``\\x1b`` then leaks into the rendered head and this assertion flips RED.
    Reverting the ANSWER-label neutralize (the other site, see the next test)
    does NOT affect this assertion."""
    msgs = [_answered_message(
        iv_id="iv-3", prompt=_ESC_OSC_PAYLOAD, answer="ack", detail=_ESC_OSC_PAYLOAD,
    )]
    frames = [f for f in project_restored_frames(msgs) if f.kind != "system"]
    rendered = _render(frames[0])
    assert "\x1b" not in rendered, f"raw ESC leaked into restored head: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into restored head: {rendered!r}"
    assert "RED" in rendered


def test_restore_answer_label_neutralized_at_render() -> None:
    """Tier 2c: a RAW ESC/OSC payload in the persisted ANSWER (e.g. a
    selected closed-set choice's model-supplied label) must not reach the
    rendered "✓ answered: ..." line — neutralized at
    ``_present_intervention_pending``'s resolved-line call site, added by
    this PR.

    NON-VACUITY: verified locally by reverting ONLY the
    ``_neutralized_label(str(answer))`` call this PR adds around the answer
    (restoring the old ``str(answer)`` passthrough) — the raw ``\\x1b`` then
    leaks into the rendered resolved line and this assertion flips RED.
    Reverting the prompt/detail neutralize (the other site, previous test)
    does NOT affect this assertion — the payload here lives in ``answer``,
    not ``prompt``/``detail``, which stay plain."""
    msgs = [_answered_message(
        iv_id="iv-4", prompt="Proceed?", answer=_ESC_OSC_PAYLOAD,
    )]
    frames = [f for f in project_restored_frames(msgs) if f.kind != "system"]
    rendered = _render(frames[0])
    assert "\x1b" not in rendered, f"raw ESC leaked into restored answer line: {rendered!r}"
    assert "\x07" not in rendered, f"raw BEL leaked into restored answer line: {rendered!r}"
    assert "RED" in rendered


# ── Producer round trip: the real InterventionHandler writes this exact shape ─


def _build_handler(tmp_path: Path) -> tuple[InterventionHandler, InterventionRegistry, list]:
    """A real, fully-wired InterventionHandler + InterventionRegistry +
    SnapshotJournal (mirrors ``test_intervention_handler_invariants.py``'s
    ``_build_handler`` — no mocks). Returns ``(handler, registry,
    history_items)`` where ``history_items`` collects the exact
    ``(role, text, ts, meta)`` tuples ``_append_history`` receives."""
    state_log = StateLog(tmp_path / "state.wal")
    event_store = EventStore(tmp_path / "events")
    event_log = EventLog(subscribers=[event_store])
    journal = SnapshotJournal(
        agent_name="test_agent", snapshot_path=tmp_path / "snap.json", state_log=state_log,
    )
    history_items: list[dict] = []

    async def _put_outbox(msg: OutboxMessage) -> None:
        pass

    def _append_history(role: str, text: str, ts: str, meta: dict) -> None:
        history_items.append({"role": role, "text": text, "ts": ts, "meta": meta})

    handler_ref: list[InterventionHandler] = []

    async def _on_announce(iv: UserIntervention) -> None:
        if handler_ref:
            await handler_ref[0].announce(iv)

    registry = InterventionRegistry(on_announce=_on_announce)
    handler = InterventionHandler(
        intervention_registry=registry, journal=journal, event_log=event_log,
        put_outbox=_put_outbox, append_history=_append_history,
    )
    handler_ref.append(handler)
    return handler, registry, history_items


@pytest.mark.asyncio
async def test_producer_closed_set_answer_round_trips_through_restore(tmp_path: Path) -> None:
    """Tier 2: end-to-end — dispatch a REAL closed-set intervention through
    the REAL ``InterventionHandler``, answer it by ``choice_id_override``
    (the panel's delivery path), and feed the resulting ``history_items``
    entry (recast as a ``ChatMessage``, exactly as ``Session._append_history``
    would persist it) straight into ``project_restored_frames``. Proves the
    fixture the projection-only tests above assume is not a guess — it is
    what the real producer writes, including for the CLOSED-SET path where
    ``ChatMessage.content`` is empty and the answer only survives via
    ``INTERVENTION_ANSWER_META_KEY``."""
    handler, registry, history_items = _build_handler(tmp_path)
    iv = UserIntervention(
        kind="ask_user",
        prompt="Delete the branch?",
        run_id="run-1",
        choices=[
            InterventionChoice(id="yes", label="Yes", hotkey="y"),
            InterventionChoice(id="no", label="No", hotkey="n"),
        ],
    )
    dispatch_task = asyncio.ensure_future(handler.dispatch(iv))
    from tests._async_wait import wait_until  # noqa: E402 — shared #1751 test wait helper
    await wait_until(lambda: bool(registry.list_active()))

    resolved = await handler.deliver_answer_to(iv, "", choice_id_override="yes")
    assert resolved is True
    await asyncio.gather(dispatch_task, return_exceptions=True)

    assert [h["role"] for h in history_items] == ["user"], (
        f"expected exactly one appended history entry, got {history_items!r}"
    )
    entry = history_items[0]
    assert entry["meta"][INTERVENTION_PROMPT_META_KEY] == "Delete the branch?"
    assert entry["meta"][INTERVENTION_ANSWER_META_KEY] == "Yes"
    # The closed-set path's OWN history text is empty — confirming the
    # fixture used above (``content=""``) matches production.
    assert entry["text"] == ""

    msg = ChatMessage(role=entry["role"], content=entry["text"], meta=entry["meta"])
    frames = [f for f in project_restored_frames([msg]) if f.kind != "system"]
    assert [f.kind for f in frames] == ["intervention_resolved"], (
        f"expected exactly one intervention entry, got kinds={[f.kind for f in frames]}"
    )
    rendered = _render(frames[0])
    assert "Delete the branch?" in rendered
    assert "Yes" in rendered
