"""InboxArbiter — owns the inbox drain / dispatch-attribution state cluster
extracted from Session (proposal 0067 P1, #3978).

Before this extraction, four pieces of state (`_pending_inbox_item`,
`_cancelled_msg_ids`, `_next_turn_context`, `_last_sender`/`_last_reply_to`)
and the methods that read/write them (`_consume_inbox`,
`_peek_mid_turn_injections`, `_drain_to_wake`, `_stage_next_turn_context`,
`_handle_sender_attribution`) lived directly on ``Session`` alongside ~40
unrelated fields — one more example of the field-usage cluster the
`refactoring` skill's Extract Class step targets: these methods touch each
other's state and nothing else Session owns (external deps are threaded in
at construction, see below).

Byte-identical extraction where the prior inline body allowed it
(`consume_inbox`, `peek_mid_turn_injections`, `stage_next_turn_context`,
`drain_to_wake`) — moved, not rewritten. `handle_sender_attribution` is the
one exception: extraction is also where the reply_to staleness bug (see its
own docstring) is closed, since fixing it required threading `kind` through
to the attribution call, a signature change that only makes sense to make
once, at the same time as the move.

`Session` still owns `_commit_mid_turn_injection`, `cancel_queued`,
`restore_state`, `reset_for_rewind`, and the ``_next_turn_context`` flush in
``_run_router_loop`` — each of those reaches into this arbiter's public
attributes (`pending_inbox_items` / `cancelled_msg_ids` / `next_turn_context`)
rather than owning a private copy, keeping exactly one holder of each field.
Those methods were not named in the P1 dispatch and have responsibilities
(recovery, cancellation-API, router-turn bookkeeping) wider than "inbox
arbitration" — moving them too would widen this class past its own name.

External dependencies are injected as plain callables/objects at
construction (the same "real instance + plain stub callbacks" shape
``InterAgentMessaging`` already uses) rather than a back-reference to
``Session``, so a test can construct this class with hand-written stubs
without a full ``Session``.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from reyn.runtime.turn_origin import MID_TURN_INJECTABLE

if TYPE_CHECKING:
    from reyn.runtime.services.snapshot_journal import SnapshotJournal


# FP-0041 (#489) PR-A: humanic dispatch attribution helper — moved from
# ``session.py`` verbatim (byte-identical relocation, see module docstring).
#
# Sender envelope strings follow ``<transport>:<id>[:<display>]``. This
# helper produces a human-readable label for inclusion in state_change
# summaries so the LLM sees "bob (Slack)" instead of "slack:U456:bob".
# Unknown / malformed senders fall through to the raw string.
_SENDER_TRANSPORT_DISPLAY = {
    "user":     "user",
    "slack":    "Slack",
    "line":     "LINE",
    "cron":     "scheduled cron job",
    "a2a":      "peer agent",
    "webhook":  "external webhook",
}


def _format_sender_label(sender: "str | None") -> str:
    """Format a sender envelope string for LLM-visible state_change text.

    Examples
    --------
    ``"slack:U456:bob"`` → ``"bob (Slack)"``
    ``"slack:U456"`` → ``"slack user U456"``
    ``"cron:morning_news"`` → ``"scheduled cron job 'morning_news'"``
    ``"user:tui"`` → ``"user (TUI)"``
    ``"a2a:news_agent"`` → ``"peer agent 'news_agent'"``
    ``None`` → ``"an unknown sender"`` (= used in first-turn pre-state)

    Falls through to the raw string when the transport is not in the
    known list — keeps the dispatch resilient to new sources added by
    future PRs without label updates here.
    """
    if sender is None:
        return "an unknown sender"
    parts = sender.split(":", 2)
    if not parts or not parts[0]:
        return sender
    transport = parts[0]
    rest = parts[1:] if len(parts) > 1 else []
    transport_label = _SENDER_TRANSPORT_DISPLAY.get(transport)
    if transport_label is None:
        return sender
    if transport == "user":
        surface = rest[0].upper() if rest else ""
        return f"user ({surface})" if surface else "user"
    if transport == "slack" or transport == "line":
        if len(rest) >= 2 and rest[1]:
            return f"{rest[1]} ({transport_label})"
        if len(rest) >= 1 and rest[0]:
            return f"{transport_label.lower()} user {rest[0]}"
        return transport_label
    if transport == "cron":
        if rest and rest[0]:
            return f"{transport_label} '{rest[0]}'"
        return transport_label
    if transport == "a2a":
        if rest and rest[0]:
            return f"{transport_label} '{rest[0]}'"
        return transport_label
    if transport == "webhook":
        if rest and rest[0]:
            return f"{transport_label} ({rest[0]})"
        return transport_label
    return sender


class InboxArbiter:
    """Owns inbox-drain + dispatch-attribution state for one Session.

    Constructed once per Session, alongside its journal. All journal /
    history / audit side effects are injected as callables so this class
    has no back-reference to Session.
    """

    def __init__(
        self,
        *,
        inbox: "asyncio.Queue",
        journal: "SnapshotJournal",
        notify_state_change: "Callable[..., None]",
    ) -> None:
        self._inbox = inbox
        self._journal = journal
        self._notify_state_change = notify_state_change

        # #3792/#5647: a volatile (not WAL/snapshot-backed) peek buffer, in
        # ARRIVAL ORDER — see ``peek_mid_turn_injections``'s own docstring.
        # Was a 1 slot until #5647: injection now looks PAST an ineligible
        # head for the operator's own message, and the items it looked past
        # have to be held somewhere that preserves the order they arrived in.
        self.pending_inbox_items: "list[tuple[str, dict]]" = []
        # #3300 P3 (Y-server): msg_ids cancelled via ``Session.cancel_queued``
        # while still sitting in the (durable) ``asyncio.Queue`` — see
        # ``consume_inbox``/``drain_to_wake``.
        self.cancelled_msg_ids: "set[str]" = set()
        # In-memory staging for wake=false ride-along messages, durably
        # persisted in the snapshot (#1800 slice 4b).
        self.next_turn_context: "list[dict]" = []
        # Sender of the most-recently-dispatched inbox item, for
        # sender-transition state_change entries (FP-0041 #489 PR-A).
        self.last_sender: "str | None" = None
        # Reply-to attribution captured from an inbound payload's reply_to
        # (FP-0041 #489 PR-D2).
        self.last_reply_to: Any = None

    async def consume_inbox(self) -> "tuple[str, dict] | None":
        """Wait for next inbox message; on receive, record `inbox_consume`
        via journal (skipped for shutdown signals which are out-of-band).

        #3300 P3 (Y-server) skip-at-consume: if the dequeued item's msg_id was
        already cancelled (``Session.cancel_queued`` — its snapshot.inbox entry
        + WAL ``inbox_cancel`` tombstone were already recorded synchronously at
        cancel time), this discards the physical ``asyncio.Queue`` entry
        WITHOUT recording a redundant ``inbox_consume`` (the item is already
        gone from the snapshot) and returns ``None`` — the caller
        (``drain_to_wake``) must treat this as "nothing dequeued yet" and
        loop again, never dispatching a turn for a cancelled item.

        #3792: drains ``self.pending_inbox_items`` FIRST, from its head, ahead
        of a fresh ``self._inbox.get()``. Items land there via
        ``peek_mid_turn_injections`` — either because the running turn ended
        (cancel / cap / overflow / LLM exception) before
        ``Session._commit_mid_turn_injection`` ever fired (carry-forward: the
        item is simply processed as an ordinary new turn here, exactly as if
        the peek had never happened), or because injection looked PAST them to
        reach an operator message further back (#5647). Checking here first is
        what makes both cases land correctly instead of being stranded behind a
        ``self._inbox.get()`` that would never see them again.

        #5647, the consumption-order invariant: the buffer is FIFO and is
        drained before the queue, so items injection looked past are still
        consumed in arrival order, relative to each other AND to everything
        still queued behind them. Injection changes which message the model
        sees first inside a turn; it does not change the order turns are
        started in.
        """
        if self.pending_inbox_items:
            kind, payload = self.pending_inbox_items.pop(0)
        else:
            kind, payload = await self._inbox.get()
        return await self._journal_and_filter(kind, payload)

    def _next_nowait(self) -> "tuple[str, dict] | None":
        """#5647: the next item in arrival order without blocking — buffer
        head first, then the queue.

        Every non-blocking read of the inbox has to go through here, not
        ``self._inbox.get_nowait()`` directly. Before #5647 the peek buffer
        held at most ONE item and ``consume_inbox`` had just emptied it, so a
        bare ``get_nowait()`` next to it happened to be correct; now the buffer
        can hold several, and reading the queue directly would skip straight
        past them — silently reordering exactly what this design promises not
        to.
        """
        if self.pending_inbox_items:
            return self.pending_inbox_items.pop(0)
        try:
            return self._inbox.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _journal_and_filter(
        self, kind: str, payload: dict,
    ) -> "tuple[str, dict] | None":
        """Shared tail of ``consume_inbox``: skip-at-consume for a cancelled
        item, then journal the consume. Extracted so the buffer-aware read
        above and the original body stay one behaviour, not two copies."""
        msg_id = payload.get("_msg_id") if isinstance(payload, dict) else None
        if kind != "shutdown" and msg_id in self.cancelled_msg_ids:
            self.cancelled_msg_ids.discard(msg_id)
            return None
        if kind != "shutdown":
            await self._journal.consume_inbox(msg_id=msg_id)
        return kind, payload

    async def peek_mid_turn_injections(self) -> "list[dict]":
        """#3792/#5677: non-blocking, non-committing peek at the inbox for
        EVERY currently-eligible mid-turn injection candidate, in arrival
        order.

        Returns ``[{"payload": dict, "msg_id": str, "kind": str}, ...]`` —
        one entry per eligible (:data:`~reyn.runtime.turn_origin.
        MID_TURN_INJECTABLE`), uncancelled item found during ONE
        non-blocking scan of the combined buffer+queue; ``[]`` when there
        is none (empty queue, or nothing but ineligible origins).

        Does NOT commit — ``self._journal``/``snapshot.inbox`` (the SSoT) is
        untouched either way. Everything dequeued along the way is held in
        ``self.pending_inbox_items``, in arrival order, so nothing is lost:
        every returned candidate stays there until
        ``Session._commit_mid_turn_injection`` actually commits it (the
        caller must have spliced it into the wire first — see
        ``RouterLoop.run_loop``'s seam), and the items looked past stay
        there UNCONSUMED for the ordinary turn-boundary ``consume_inbox``.

        **#5647 changed the single-candidate rule, deliberately** (kept
        here — see git history for the pre-#5677 docstring's full
        reasoning): #3792 originally STOPPED at an ineligible head rather
        than looking further, which measured out to mid-turn injection
        almost never firing on a busy inbox (a continuous hook producer
        sitting in front of the one eligible item). #5647 fixed that by
        looking PAST ineligible items instead of stopping, while keeping
        arrival order intact (the FIFO buffer, drained before the queue)
        and leaving an explicit trace (``skipped_over`` on the
        ``turn_started`` event).

        **#5677 (owner ruling, verbatim): "溜まっているものは基本inject
        すべき… わざわざ分けるとコスト増えるだけ".** Collecting every
        eligible item in ONE scan, instead of returning after the first,
        is the direct application of that ruling: a caller that injects
        them all into the SAME completion round pays for one send, not
        one per item (re-sending the whole prior context on each would be
        pure waste). Non-eligible items BETWEEN two eligible ones are
        still looked past and remain in the buffer — only their own kind
        keeps them out, never their position.

        Eligibility is :data:`~reyn.runtime.turn_origin.
        MID_TURN_INJECTABLE` (#5677 — was hardcoded to ``CLIENT_INPUT``
        alone, the provenance gate #3595 built for an unrelated question;
        see that constant's own docstring for why the two questions are
        separate). What is looked past is every kind NOT in that set.

        A CANCELLED item is the one case this DOES discard outright — mirrors
        ``consume_inbox``'s own skip-at-consume handling, since a cancelled
        item was never going to be eligible for anything. That applies to items
        looked past as well as to any candidate.
        """
        collected: "list[dict]" = []
        scan = 0
        while True:
            # Walk the buffer first, then pull from the queue — one combined
            # arrival-ordered sequence, not two.
            if scan >= len(self.pending_inbox_items):
                try:
                    self.pending_inbox_items.append(self._inbox.get_nowait())
                except asyncio.QueueEmpty:
                    return collected
            kind, payload = self.pending_inbox_items[scan]
            msg_id = payload.get("_msg_id") if isinstance(payload, dict) else None
            if kind != "shutdown" and msg_id in self.cancelled_msg_ids:
                self.cancelled_msg_ids.discard(msg_id)
                del self.pending_inbox_items[scan]
                continue
            if kind not in MID_TURN_INJECTABLE:
                scan += 1
                continue
            collected.append({"payload": payload, "msg_id": msg_id, "kind": kind})
            scan += 1

    def skipped_over_before(self, msg_id: "str | None") -> "list[dict]":
        """#5647: the items injection looked past to reach ``msg_id``, as
        ``[{"kind", "msg_id"}]`` in arrival order.

        Derived from the buffer's own order at commit time rather than
        remembered from the peek: the buffer IS the arrival-ordered record, so
        reading it needs no second copy of the same fact to keep in sync (the
        peek and the commit are separated by a whole LLM round trip, and
        anything carried across that gap can go stale — a cancel can land in
        between).

        Empty list when ``msg_id`` is at the head, or is not held at all.
        """
        out: "list[dict]" = []
        for kind, payload in self.pending_inbox_items:
            held = payload.get("_msg_id") if isinstance(payload, dict) else None
            if held == msg_id:
                return out
            out.append({"kind": kind, "msg_id": held})
        return []

    async def stage_next_turn_context(self, kind: str, payload: dict) -> None:
        """Stage a wake=false ride-along (C) into next-turn context, durably
        (B=persist): append to the in-memory buffer + record the WAL/snapshot
        entry. Shared by ``drain_to_wake`` (inbox ride-alongs) and the
        ``HookDispatcher`` (#1800 slice 5b — direct C-staging that bypasses the
        inbox). Byte-behavior-identical extraction of the prior inline pair."""
        self.next_turn_context.append({"kind": kind, "payload": payload})
        await self._journal.record_next_turn_context_staged(kind=kind, payload=payload)

    async def drain_to_wake(
        self,
    ) -> "tuple[list[tuple[str, dict]], tuple[str, dict]] | tuple[None, None]":
        """Drain the inbox up to and including the first ``wake=true`` message.

        Each inbox payload carries an optional ``wake`` bool (default ``True``
        when absent).  Every producer but the hook dispatcher (``TurnOrigin``'s
        other members) never sets ``wake``; the absent-means-True default makes them
        all behaviorally identical to wake=true, so the common/back-compat
        path returns immediately after the first blocking get with no
        ride-alongs.

        Returns ``(ride_alongs, trigger)`` where:

        - ``ride_alongs``  — list of ``(kind, payload)`` tuples for every
          ``wake=false`` message drained before the trigger.  Staged for the
          next turn as context (slice 4b).  Empty in the common case.
        - ``trigger``      — the first ``wake=true`` (or absent-wake) message;
          this drives the turn.

        Special case: if the first blocking get yields ``shutdown``, returns
        ``(None, None)`` so the caller can signal loop exit.

        Decision A (RESOLVED, issuecomment-4773744053): if the queue empties
        while holding only ``wake=false`` ride-alongs (no trigger yet),
        re-enter the blocking wait.  Ride-alongs NEVER trigger a turn alone.

        Per-message ``inbox_consume`` is recorded via ``consume_inbox`` for
        EACH drained message (ride-alongs and the trigger alike), so the
        snapshot stays correct on crash+restore.
        """
        ride_alongs: "list[tuple[str, dict]]" = []

        while True:
            # (a) Blocking wait — preserves the idle-sleep property exactly
            # as the previous single-get path did.  Also records
            # inbox_consume via _consume_inbox (journaled, P6-clean).
            # #3300 P3 (Y-server): `None` means the dequeued item was
            # cancelled (skip-at-consume) — loop again without treating it
            # as a trigger or ride-along.
            step = await self.consume_inbox()
            if step is None:
                continue
            kind0, p0 = step

            # (b) Shutdown sentinel: propagate immediately regardless of any
            # already-accumulated ride-alongs.
            if kind0 == "shutdown":
                return None, None

            # (c) wake=true (or absent → default True): this is the trigger.
            # Common/back-compat path — returns after the first blocking get
            # with no ride-alongs.
            if p0.get("wake", True):
                return ride_alongs, (kind0, p0)

            # (d) wake=false ride-along: stage durably (B=persist) the
            # moment it is consumed, BEFORE re-entering the blocking wait
            # for the trigger.  This closes the gap: without this, a crash
            # in the blocking wait would lose the consumed-but-not-persisted
            # ride-along.
            await self.stage_next_turn_context(kind0, p0)
            ride_alongs.append((kind0, p0))

            # Non-blocking drain: collect additional wake=false messages until
            # either a wake=true trigger arrives or the queue is momentarily
            # empty (Decision A: re-enter the blocking wait in that case).
            while True:
                # #5647: buffer head first, then the queue — a bare
                # ``get_nowait()`` here would read straight past items the
                # mid-turn peek is holding, which is the one reordering this
                # design promises does not happen. See ``_next_nowait``.
                _nb = self._next_nowait()
                if _nb is None:
                    # Nothing pending, no trigger yet — re-enter outer
                    # blocking wait (Decision A).
                    break
                kind_nb, p_nb = _nb

                msg_id_nb = (
                    p_nb.get("_msg_id") if isinstance(p_nb, dict) else None
                )
                # #3300 P3 (Y-server) skip-at-consume: same discard as the
                # blocking path above — a cancelled item is never dispatched
                # and never gets a redundant inbox_consume.
                if kind_nb != "shutdown" and msg_id_nb in self.cancelled_msg_ids:
                    self.cancelled_msg_ids.discard(msg_id_nb)
                    continue
                if kind_nb != "shutdown":
                    await self._journal.consume_inbox(msg_id=msg_id_nb)

                if kind_nb == "shutdown":
                    return None, None

                if p_nb.get("wake", True):
                    return ride_alongs, (kind_nb, p_nb)

                # wake=false via non-blocking path: stage durably before
                # accumulating (same B=persist guarantee as the outer path).
                await self.stage_next_turn_context(kind_nb, p_nb)
                ride_alongs.append((kind_nb, p_nb))

    def handle_sender_attribution(self, payload: object) -> None:
        """Surface a sender transition to the LLM as a state_change entry
        (= FP-0041 (#489) PR-A humanic dispatch attribution).

        When the sender of an inbox item differs from the prior turn's
        sender, emit a state_change history entry so the LLM reads
        "[context shift] Now responding to <X> via <transport>.
        Previous turn was from <Y>." before processing the new turn.
        Without this, merged-inbox multi-consumer dispatch produces a
        confused linear feed where the LLM can't tell who's talking.

        ``sender`` convention (= envelope shape):
          - ``user:tui`` / ``user:web`` / ``user:cli`` — local human user
          - ``slack:<user_id>[:<display_name>]`` — Slack consumer
          - ``line:<user_id>[:<display_name>]`` — LINE consumer
          - ``cron:<job_name>`` — scheduled fire
          - ``a2a:<peer_agent>`` — peer-agent message
          - ``webhook:<source>`` — external event source (= Phase 2)

        Payloads without a ``sender`` field are dispatched unchanged
        (= backward compat for existing inbox producers that haven't
        adopted the convention yet). No state_change is emitted in
        that case; ``self.last_sender`` is unchanged.

        FP-0041 #489 PR-D2 originally captured ``reply_to`` from the payload
        ONLY when present, preserving the previous value otherwise, on the
        stated theory that a missing ``reply_to`` "falls through to the
        default surface" downstream. #3978 P1 co-vet (architect + lead-coder,
        two independently-run verifications converging on the same finding)
        established that theory was never true: ``Session._put_outbox`` /
        ``_put_outbox_nowait`` both default a message's ``reply_to`` from
        ``last_reply_to`` BEFORE the external-transport interceptor ever
        runs, so a preserved stale value IS delivered, not bypassed — a
        CRON/HOOK/AGENT_STEP/PIPELINE_RESULT turn (none of which ever carry
        ``reply_to`` — verified against every inbox-payload-constructing
        call site in ``src/reyn/``) running after a Slack/webhook-originated
        turn inherited and misdelivered to that turn's transport. No
        producer relies on the omission meaning "keep inheriting" (same
        sweep); the two producers that ever set ``reply_to`` at all
        (``gateway.api``'s webhook dispatch, ``interfaces.web.server``'s
        cron notify) each resend it on every payload that wants it, and the
        cron path's own comment already documents the intended fallback:
        "No notify → no reply_to → interceptor falls through → event-log
        only (current behaviour)". The fix below makes that the ACTUAL
        behavior instead of an aspirational comment: ``reply_to`` is always
        taken from THIS payload, never inherited from the last one.
        """
        if not isinstance(payload, dict):
            return
        self.last_reply_to = payload.get("reply_to")
        new_sender = payload.get("sender")
        if not new_sender or not isinstance(new_sender, str):
            return
        if new_sender == self.last_sender:
            return
        prev_label = _format_sender_label(self.last_sender)
        new_label = _format_sender_label(new_sender)
        if self.last_sender is None:
            summary = (
                f"[context shift] Now responding to {new_label}. "
                f"This is the first attributed turn this session."
            )
        else:
            summary = (
                f"[context shift] Now responding to {new_label}. "
                f"Previous turn was from {prev_label}."
            )
        try:
            self._notify_state_change(summary, source="dispatch_attribution")
        except Exception:
            # Defensive: attribution emission must not crash dispatch.
            pass
        self.last_sender = new_sender
