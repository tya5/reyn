"""InterventionRegistry — owns active-intervention queue (extracted from ChatSession wave 1C).

Crash-recovery persistence (PR-intervention-link L2/L5/L6 + R-D12):
in-flight interventions ARE durably tracked. ``SnapshotJournal`` records
``intervention_dispatched`` / ``intervention_resolved`` /
``intervention_answer_buffered`` / ``intervention_answer_consumed`` events
to the WAL and maintains ``outstanding_interventions`` +
``buffered_intervention_answers`` in the per-agent snapshot. After a
restart, ``ChatSession.restore_state`` calls :meth:`restore` to re-enqueue
the saved interventions with a watcher coroutine that fires
``intervention_resolved`` once the user answers — see
``test_intervention_restore`` + ``test_session_intervention_persistence``
for the e2e pins.

Announce callback is injected at construction so the registry has no direct
dependency on ChatSession.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Awaitable, Callable

from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
    match_choice,
)


class InterventionRegistry:
    """Owns active_interventions dict + announce/deliver/dispatch flows.

    Crash-recovery persistence is handled outside this class — the
    ``SnapshotJournal`` records dispatch/resolve/buffer events to the WAL
    and ``ChatSession.restore_state`` calls :meth:`restore` to re-enqueue
    saved interventions after a restart. See module docstring for the full
    flow.

    Parameters
    ----------
    on_announce:
        Async callback invoked when an intervention is ready to be shown to
        the user.  The session supplies its ``_announce_intervention`` method
        here; tests supply a mock.
    """

    def __init__(
        self,
        *,
        on_announce: Callable[[UserIntervention], Awaitable[None]],
        enforce_listener_presence: bool = False,
    ) -> None:
        """Construct an InterventionRegistry.

        Parameters
        ----------
        on_announce:
            Async callback invoked when an intervention is ready to be shown
            to the user. The session supplies its ``_announce_intervention``
            method here; tests supply a mock.
        enforce_listener_presence:
            When True, ``dispatch()`` short-circuits with an empty
            ``InterventionAnswer`` if no listener is registered (= "no one
            will call ``deliver_answer``, prompt would hang forever"). Caller
            interprets the empty answer as a refusal — matching the existing
            cancellation contract — and falls through to its legacy abort
            path. Default False preserves the pre-issue-#254 behaviour for
            direct registry users (= tests at this layer that construct a
            registry without a real listener and feed answers manually via
            ``deliver_answer``).

            ChatSession (and any future top-level entry that owns its own
            listener wiring) opts in via this flag so a missing listener
            (= headless deploy, test without TUI mount, A2A peer offline)
            fails closed instead of waiting on an unresolvable future.
            issue #254 Phase 1.
        """
        self._on_announce = on_announce
        self._active: dict[str, UserIntervention] = {}
        # Preserves FIFO emission order; gives head-of-queue semantics.
        self._order: deque[str] = deque()
        # issue #254 Phase 1: listener-presence guard against forever-await.
        self._enforce_listener_presence = enforce_listener_presence
        self._listeners: set[str] = set()
        # issue #268 Phase 1: stalled queue — iv whose origin channel
        # closed while the iv was unresolved. Other channels can
        # observe / discard / claim entries here via the ChatSession
        # API; the iv's future stays unresolved until a claim moves it
        # back to ``_active`` for a new origin channel or a discard
        # cancels it. ``_active`` and ``_stalled`` are mutually
        # exclusive — an iv is in one or the other, never both.
        self._stalled: dict[str, UserIntervention] = {}

    # ── Read helpers for tests / external observers ─────────────────────────

    def has_active(self, iv_id: str) -> bool:
        """Return True iff *iv_id* is currently in the active queue."""
        return iv_id in self._active

    def has_stalled(self, iv_id: str) -> bool:
        """Return True iff *iv_id* is currently in the stalled queue."""
        return iv_id in self._stalled

    def is_queued(self, iv_id: str) -> bool:
        """Return True iff *iv_id* is currently in the FIFO order deque.

        The order deque tracks the emission sequence for the active map;
        an entry stays in both ``_active`` and the order deque while
        active, and leaves both when ``mark_stalled`` / ``deliver_answer``
        / ``discard_stalled`` / ``claim_stalled`` runs.
        """
        return iv_id in self._order

    # ── Listener registration (issue #254 Phase 1) ───────────────────────────

    def register_listener(self, listener_id: str) -> None:
        """Mark *listener_id* as actively able to resolve interventions.

        A listener is any entity that has committed to eventually calling
        ``deliver_answer`` for queued interventions — in practice the
        attached TUI app, an A2A peer override, or a test fixture that
        will drive ``_maybe_answer_oldest_intervention`` manually. The
        registry uses the set's emptiness to decide whether ``dispatch``
        should short-circuit (when ``enforce_listener_presence=True``).
        Calling twice with the same id is idempotent.
        """
        self._listeners.add(listener_id)

    def unregister_listener(self, listener_id: str) -> None:
        """Remove *listener_id* from the active set. Idempotent."""
        self._listeners.discard(listener_id)

    def has_active_listener(self) -> bool:
        """Return True iff at least one listener is currently registered."""
        return bool(self._listeners)

    def listener_count(self) -> int:
        """Return the number of currently-registered listeners."""
        return len(self._listeners)

    def is_listener_enforcement_enabled(self) -> bool:
        """Return True iff the registry was constructed with enforce_listener_presence=True."""
        return self._enforce_listener_presence

    def clear(self) -> None:
        """Drop all active + stalled interventions (ADR-0038 Stage 1c-2 rewind).

        Cancels each pending iv future (their awaiters are already cancelled by
        the rewind's ``cancel_inflight``, so this just releases the handles) and
        clears the active / ordering / stalled collections. Listener
        registration (``_listeners``) is intentionally preserved — the attached
        channel survives a rewind. The rewind path re-populates via ``restore()``
        from the reconstructed snapshot, leaving no pre-rewind residue.
        """
        for iv in (*self._active.values(), *self._stalled.values()):
            if iv.future is not None and not iv.future.done():
                iv.future.cancel()
        self._active.clear()
        self._order.clear()
        self._stalled.clear()

    # ── Stalled queue operations (issue #268 Phase 1) ────────────────────────

    def mark_stalled(self, iv_id: str) -> bool:
        """Move *iv_id* from ``_active`` to ``_stalled``.

        Called by ``ChatSession.handle_intervention`` when the iv's
        ``origin_channel_id`` no longer maps to a registered listener
        (= origin channel closed). Returns True iff the iv was in
        ``_active`` and is now in ``_stalled``.

        The iv's future stays unresolved. The stalled iv can be
        observed (``list_stalled``), discarded (``discard_stalled``),
        or claimed (``claim_stalled``) by other channels.
        """
        iv = self._active.pop(iv_id, None)
        if iv is None:
            return False
        try:
            self._order.remove(iv_id)
        except ValueError:
            pass
        self._stalled[iv_id] = iv
        return True

    def list_stalled(self) -> list[UserIntervention]:
        """Return all stalled interventions (= origin channel closed).

        Read-only snapshot — caller iterates without holding the
        registry's internal collection. Used by
        ``ChatSession.list_stalled_interventions`` for the cross-channel
        observe surface.
        """
        return list(self._stalled.values())

    def stalled_count(self) -> int:
        """Return the number of stalled interventions."""
        return len(self._stalled)

    def get_stalled(self, iv_id: str) -> UserIntervention | None:
        """O(1) lookup of a stalled iv by id."""
        return self._stalled.get(iv_id)

    def discard_stalled(self, iv_id: str) -> bool:
        """Cancel a stalled iv — set its future to an empty answer +
        remove from the stalled queue.

        Returns True iff the iv was in ``_stalled`` and was discarded
        (= the future was either resolved with an empty answer or
        cancelled). Matches the existing cancellation contract
        (``InterventionAnswer(text="")``) so awaiters interpret the
        outcome as a refusal.
        """
        iv = self._stalled.pop(iv_id, None)
        if iv is None:
            return False
        if iv.future is not None and not iv.future.done():
            try:
                iv.future.set_result(InterventionAnswer(text=""))
            except Exception:  # noqa: BLE001 — best-effort
                # Future may be bound to a dead loop; cancel as fallback.
                try:
                    iv.future.cancel()
                except Exception:  # noqa: BLE001
                    pass
        return True

    def claim_stalled(self, iv_id: str, new_origin_channel_id: str) -> UserIntervention | None:
        """Reactivate a stalled iv with a new origin channel.

        Moves the iv from ``_stalled`` back to ``_active`` (= head of
        the active queue, ready for re-dispatch), updates its
        ``origin_channel_id`` to the caller's channel. Returns the iv
        on success so the caller can drive the re-dispatch (= call
        ``dispatch(iv)`` again to deliver to the new origin).

        Returns ``None`` when ``iv_id`` is not in ``_stalled``.
        """
        iv = self._stalled.pop(iv_id, None)
        if iv is None:
            return None
        iv.origin_channel_id = new_origin_channel_id
        # NB: we don't re-enqueue into _active here — let the caller
        # call dispatch() so the normal flow (= announce / await)
        # applies. Returning the iv signals "ready to dispatch again".
        return iv

    # ── Bus interface (called by ChatInterventionBus from skills) ────────────

    async def dispatch(self, iv: UserIntervention) -> InterventionAnswer:
        """Register *iv* in the queue, announce or signal queued status, then
        await the user's response.

        Always removes the entry on exit so a cancelled skill does not leave
        dangling queue entries.  On ``asyncio.CancelledError`` the future is
        cancelled and an empty ``InterventionAnswer`` is returned to the
        caller (same contract as the original ``_dispatch_intervention``).

        issue #254 Phase 1: when ``enforce_listener_presence=True`` was set
        at construction AND no listener is registered, return an empty
        ``InterventionAnswer`` immediately instead of enqueuing + awaiting
        an unresolvable future. The caller (= ``handle_limit_exceeded`` /
        permission gate) sees this as a refusal and falls through to abort,
        matching the existing cancellation contract.
        """
        if self._enforce_listener_presence and not self._listeners:
            # No listener will call deliver_answer → prompt would hang
            # forever. Match the cancellation contract: return empty answer
            # so the caller treats it as a refusal.
            return InterventionAnswer(text="")
        self._active[iv.id] = iv
        self._order.append(iv.id)
        try:
            if len(self._order) == 1:
                await self._on_announce(iv)
            # If there are already queued interventions the session-level
            # announce (or a "queued" status message) is the session's
            # responsibility in wave 2.  The registry itself only calls
            # on_announce for the head intervention.
            try:
                return await iv.future
            except asyncio.CancelledError:
                return InterventionAnswer(text="")
        finally:
            self._active.pop(iv.id, None)
            try:
                self._order.remove(iv.id)
            except ValueError:
                pass
            await self._maybe_announce_next()

    # ── Read-only queries (for slash commands, status display) ───────────────

    def list_active(self) -> list[UserIntervention]:
        """Return all queued interventions in FIFO order."""
        return [self._active[i] for i in self._order if i in self._active]

    def head(self) -> UserIntervention | None:
        """Return the oldest still-pending intervention, or None."""
        # Evict stale heads transparently before returning.
        while self._order:
            head_id = self._order[0]
            iv = self._active.get(head_id)
            if iv is None or iv.future.done():
                self._order.popleft()
                self._active.pop(head_id, None)
                continue
            return iv
        return None

    def get(self, iv_id: str) -> UserIntervention | None:
        """O(1) lookup by intervention id."""
        return self._active.get(iv_id)

    def is_empty(self) -> bool:
        return len(self._active) == 0

    def queued_count(self) -> int:
        return len(self._active)

    # ── Resolution (called by user input router) ─────────────────────────────

    async def deliver_answer(
        self,
        iv: UserIntervention,
        text: str,
        *,
        choice_id_override: str | None = None,
    ) -> bool:
        """Resolve *iv* with *text*.

        Returns
        -------
        True
            The intervention was consumed (future resolved).
        False
            Not consumed — either the future was already done, or choices were
            present but the user's text did not match any hotkey (re-prompt
            path; the caller may surface a hint to the user).

        ``choice_id_override``: when set (= peer-provided structured
        answer, e.g. A2A POST with explicit ``choice_id`` per PR #285
        Gap 4), bypass ``match_choice`` and resolve directly with the
        supplied choice id. Validates the id against ``iv.choices`` so
        a malformed peer payload still falls into the unrecognised-
        choice path. issue #292 (α): added so the peer-answer route
        can reach this single resolution path without losing the
        choice_id semantics PR #285 wired through.
        """
        if iv.future.done():
            return False
        if iv.choices:
            if choice_id_override is not None:
                # Peer supplied choice_id explicitly; verify it's valid.
                valid_ids = {c.id for c in iv.choices}
                if choice_id_override not in valid_ids:
                    return False
                answer = InterventionAnswer(
                    text=text, choice_id=choice_id_override,
                )
            else:
                choice = match_choice(text, iv.choices)
                if choice is None:
                    # Unrecognised hotkey — do NOT resolve; let the caller handle
                    # the re-prompt hint.  Return False so the session knows not
                    # to suppress a fresh router turn.
                    return False
                answer = InterventionAnswer(text=text, choice_id=choice.id)
        else:
            answer = InterventionAnswer(text=text)

        iv.future.set_result(answer)
        self._active.pop(iv.id, None)
        try:
            self._order.remove(iv.id)
        except ValueError:
            pass
        return True

    async def maybe_answer_head(self, text: str) -> bool:
        """If any intervention is pending, deliver *text* to the head and
        return True.  Stale (already-resolved) entries are evicted first."""
        head = self.head()
        if head is None:
            return False
        return await self.deliver_answer(head, text)

    # ── Cancellation paths ───────────────────────────────────────────────────

    def cancel(self, iv_id: str) -> bool:
        """Cancel a specific intervention by id.

        Returns True when the intervention existed and its future was
        cancelled; False when the id was unknown or the future already done.
        """
        iv = self._active.pop(iv_id, None)
        if iv is None:
            return False
        try:
            self._order.remove(iv_id)
        except ValueError:
            pass
        if not iv.future.done():
            iv.future.cancel()
            return True
        return False

    def drop_for_run(self, run_id: str | None) -> list[str]:
        """Cancel all pending interventions tagged with *run_id*.

        Returns the list of intervention ids that were dropped.
        """
        if not run_id:
            return []
        victims = [
            iv_id
            for iv_id, iv in self._active.items()
            if iv.run_id == run_id
        ]
        for iv_id in victims:
            iv = self._active.pop(iv_id, None)
            try:
                self._order.remove(iv_id)
            except ValueError:
                pass
            if iv is not None and not iv.future.done():
                iv.future.cancel()
        return victims

    # ── Restore (PR-intervention-link L5) ────────────────────────────────────

    def restore(
        self,
        interventions: list[UserIntervention],
        *,
        watcher: Callable[[UserIntervention], Awaitable[None]] | None = None,
    ) -> list[asyncio.Task]:
        """Re-enqueue interventions recovered from a crash snapshot.

        Each restored intervention is added to the queue and a watcher
        coroutine is spawned that awaits its future until the user resolves
        it. The default watcher is a no-op consumer of the answer (it
        merely keeps the dispatch finally clause alive for cleanup); the
        session can supply a different watcher when L6 lands skill-resume
        answer routing.

        Returns the spawned watcher tasks (so the caller can keep references
        alive and avoid task GC warnings). FIFO order matches the input list.

        Synchronous (not async) so callers from sync context — like
        ``ChatSession.restore_state`` — don't have to wrap in
        ``asyncio.ensure_future`` (which would add a task layer that delays
        when the children become visible in the queue).
        """
        tasks: list[asyncio.Task] = []
        for iv in interventions:
            t = asyncio.ensure_future(self._restore_one(iv, watcher))
            tasks.append(t)
        return tasks

    async def _restore_one(
        self,
        iv: UserIntervention,
        watcher: Callable[[UserIntervention], Awaitable[None]] | None,
    ) -> None:
        """Background task per restored intervention.

        Hands the iv to ``dispatch`` (which announces, awaits, cleans up).
        On answer, optionally calls the watcher with the resolved iv.
        """
        try:
            answer = await self.dispatch(iv)
        except asyncio.CancelledError:
            return
        if watcher is not None:
            try:
                await watcher(iv)
            except Exception:  # noqa: BLE001 — watcher errors must not poison restore
                import logging
                logging.getLogger(__name__).warning(
                    "intervention restore watcher for %s raised", iv.id,
                    exc_info=True,
                )
        # answer is intentionally unused at this layer; L6 wires it through
        # to the resuming skill via the watcher.
        del answer

    # ── Slash-command id-prefix lookup ───────────────────────────────────────

    def resolve_id_prefix(self, prefix: str) -> tuple[str | None, list[str]]:
        """Return ``(unique_id, candidates)`` for *prefix*.

        * If exactly one intervention id starts-with or ends-with *prefix* →
          ``(that_id, [that_id])``.
        * If zero or >1 matches → ``(None, candidates)``.
        * Empty *prefix* → ``(None, [])``.
        """
        prefix = prefix.strip()
        if not prefix:
            return None, []
        candidates = [
            iv_id
            for iv_id in self._active
            if iv_id.startswith(prefix) or iv_id.endswith(prefix)
        ]
        return (candidates[0] if len(candidates) == 1 else None), candidates

    # ── Internal helpers ─────────────────────────────────────────────────────

    async def _maybe_announce_next(self) -> None:
        """Announce the new head intervention (if any) after the previous one
        was resolved or cancelled."""
        if not self._order:
            return
        head_id = self._order[0]
        iv = self._active.get(head_id)
        if iv is None or iv.future.done():
            return
        await self._on_announce(iv)


__all__ = ["InterventionRegistry"]
