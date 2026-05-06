"""InterventionRegistry — owns active-intervention queue (extracted from ChatSession wave 1C).

Not WAL-persisted: active interventions are volatile. PR21 crash-recovery for
in-flight interventions is tracked in residuals as future work.

Announce callback is injected at construction so the registry has no direct
dependency on ChatSession. Wave 2 will wire:
    registry = InterventionRegistry(on_announce=self._announce_intervention)
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

    Not WAL-persisted (PR21 out of scope; future work in residuals).

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
    ) -> None:
        self._on_announce = on_announce
        self._active: dict[str, UserIntervention] = {}
        # Preserves FIFO emission order; gives head-of-queue semantics.
        self._order: deque[str] = deque()

    # ── Bus interface (called by ChatInterventionBus from skills) ────────────

    async def dispatch(self, iv: UserIntervention) -> InterventionAnswer:
        """Register *iv* in the queue, announce or signal queued status, then
        await the user's response.

        Always removes the entry on exit so a cancelled skill does not leave
        dangling queue entries.  On ``asyncio.CancelledError`` the future is
        cancelled and an empty ``InterventionAnswer`` is returned to the
        caller (same contract as the original ``_dispatch_intervention``).
        """
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

    async def deliver_answer(self, iv: UserIntervention, text: str) -> bool:
        """Resolve *iv* with *text*.

        Returns
        -------
        True
            The intervention was consumed (future resolved).
        False
            Not consumed — either the future was already done, or choices were
            present but the user's text did not match any hotkey (re-prompt
            path; the caller may surface a hint to the user).
        """
        if iv.future.done():
            return False
        if iv.choices:
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
