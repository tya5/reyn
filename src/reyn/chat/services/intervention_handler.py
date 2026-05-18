"""InterventionHandler — user-facing ask_user flow routing.

Extracted from ChatSession (FP-0019 Wave 2 part 1).  Owns the
ask_user dispatch path, intervention announcement to outbox, and
answer delivery coordination.

Depends on FP-0019 Wave 1b SkillRunner (commit 9ae66fa) for skill-
spawn coordination.  Depends on InterventionRegistry (pre-FP-0019
extraction, snapshot_journal).

Design constraints (same pattern as other Wave 1/1b services):
- Injected deps at construction (typed + Callable callbacks).
- No direct reference to ChatSession.
- All state mutations go through injected event_log (P6).
- No skill-specific strings (P7).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.chat.outbox import OutboxMessage
from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
    match_choice,
)

if TYPE_CHECKING:
    from reyn.chat.services.intervention_registry import InterventionRegistry
    from reyn.chat.services.snapshot_journal import SnapshotJournal
    from reyn.events.events import EventLog

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iv_meta(iv: UserIntervention) -> dict:
    """Standard ``meta`` payload for OutboxMessage announcing an intervention.

    Mirrors the module-level helper in session.py (kept in sync).
    Includes structured choice data so TUI renderers can build chip buttons
    without re-parsing the formatted text string.

    Issue #163 — adds ``prompt`` and ``detail`` as structured fields so
    the TUI widget can render visual hierarchy (= amber-bold prompt + dim
    detail) instead of one bold amber blob.  ``OutboxMessage.text`` stays
    a concatenated string for backward-compat (CLI Panel renderer / log
    fallback consume it unchanged).
    """
    out: dict = {
        "intervention_id": iv.id,
        "intervention_kind": iv.kind,
        "prompt": iv.prompt,
    }
    if iv.detail:
        out["detail"] = iv.detail
    if iv.run_id:
        out["run_id"] = iv.run_id
        out["run_id_short"] = iv.run_id[-4:] if iv.run_id else ""
    if iv.skill_name:
        out["skill_name"] = iv.skill_name
    if iv.choices:
        out["choices"] = [
            {"id": c.id, "label": c.label, "hotkey": c.hotkey}
            for c in iv.choices
        ]
    if iv.suggestions:
        out["suggestions"] = list(iv.suggestions)
    return out


class InterventionHandler:
    """Routes user-input answers to pending ask_user interventions.

    Extracted from ChatSession (FP-0019 Wave 2 part 1).

    Parameters
    ----------
    intervention_registry:
        :class:`~reyn.chat.services.intervention_registry.InterventionRegistry`
        owning the active intervention queue.
    journal:
        :class:`~reyn.chat.services.snapshot_journal.SnapshotJournal` for WAL
        persistence (``intervention_dispatched`` / ``intervention_resolved``
        events — PR-intervention-link L3).
    event_log:
        Session-scoped :class:`~reyn.events.events.EventLog`.  All audit
        events (``user_answered_intervention``) are emitted here (P6).
    put_outbox:
        Async callable ``(OutboxMessage) -> None`` — forwards intervention
        announcements and status hints to the session outbox.
    append_history:
        Sync callable ``(role, text, ts, meta) -> None`` — appends a
        conversational history entry for answered interventions.  The
        callable receives the same positional kwargs as ChatSession's
        internal ``_append_history`` helper, except it is simplified to
        only the fields InterventionHandler needs.
    """

    def __init__(
        self,
        *,
        intervention_registry: "InterventionRegistry",
        journal: "SnapshotJournal",
        event_log: "EventLog",
        put_outbox: "Callable[[OutboxMessage], Awaitable[None]]",
        append_history: "Callable[[str, str, str, dict], None]",
    ) -> None:
        self._registry = intervention_registry
        self._journal = journal
        self._events = event_log
        self._put_outbox = put_outbox
        self._append_history = append_history

    # ── Public API (mirrors former session._<name> methods) ──────────────────

    async def maybe_answer(self, text: str) -> bool:
        """If any intervention is pending, deliver ``text`` to the oldest and
        return True.  Stale heads are evicted by the registry on ``head()``.

        Corresponds to session._maybe_answer_oldest_intervention.
        """
        head = self._registry.head()
        if head is None:
            return False
        return await self.deliver_answer_to(head, text)

    async def deliver_answer_to(self, iv: UserIntervention, text: str) -> bool:
        """Resolve ``iv`` with ``text``, append a user-history entry, emit the
        ``user_answered_intervention`` event.

        Wraps ``InterventionRegistry.deliver_answer`` with the session-level
        side effects (history + audit event + unknown-choice hint).  Returns
        True when the user input was consumed (answer set OR unrecognized
        choice hint emitted — both suppress a fresh router turn).

        Corresponds to session._deliver_answer_to.
        """
        if iv.future.done():
            return False
        resolved = await self._registry.deliver_answer(iv, text)
        if not resolved and iv.choices:
            # No-match path: surface hint, but consume the input so the
            # router doesn't run on a stray hotkey-attempt.
            hint = " / ".join(c.label for c in iv.choices)
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"unknown choice; expected one of: {hint}",
                meta=_iv_meta(iv),
            ))
            return True
        if not resolved:
            return False
        # Successfully resolved: append history + emit audit event.
        choice = match_choice(text, iv.choices) if iv.choices else None
        self._append_history(
            "user",
            text,
            _now_iso(),
            {
                "answered_skill": iv.skill_name or "",
                "answered_run_id": iv.run_id or "",
                "intervention_id": iv.id,
                "intervention_kind": iv.kind,
            },
        )
        self._events.emit(
            "user_answered_intervention",
            intervention_id=iv.id,
            kind=iv.kind,
            run_id=iv.run_id,
            skill=iv.skill_name,
            choice_id=choice.id if choice else None,
            answer_text=text if not iv.choices else "",
        )
        # Signal the TUI to remove the intervention widget (handles the case
        # where the user answered via text input rather than clicking a chip
        # button — the chip path calls InterventionWidget._submit which calls
        # self.remove() itself; the text-input path skips that code path).
        await self._put_outbox(OutboxMessage(
            kind="intervention_resolved",
            text="",
            meta={"iv_id": iv.id, "run_id": iv.run_id or ""},
        ))
        return True

    async def announce(self, iv: UserIntervention) -> None:
        """Format and publish an intervention to the outbox for the renderer.

        Skill / run_id provenance lives in ``meta`` — the renderer prepends a
        ``[skill#abcd]`` tag, so we don't repeat it in ``text``.

        Corresponds to session._announce_intervention.
        """
        lines: list[str] = []
        if iv.kind == "ask_user":
            lines.append(f"Question: {iv.prompt}")
        else:
            lines.append(iv.prompt)
        if iv.detail:
            lines.append(f"  {iv.detail}")
        if iv.suggestions:
            lines.append(f"  options: {' / '.join(iv.suggestions)}")
        if iv.choices:
            labels = " / ".join(c.label for c in iv.choices)
            lines.append(f"  {labels}")
        await self._put_outbox(OutboxMessage(
            kind="intervention",
            text="\n".join(lines),
            meta=_iv_meta(iv),
        ))

    async def dispatch(self, iv: UserIntervention) -> InterventionAnswer:
        """Register an intervention via the registry.  Emits a "queued" status
        when the registry already has pending entries — the registry itself
        only auto-announces the head intervention.

        Wraps ``InterventionRegistry.dispatch`` with the session-level
        "awaiting answer (N queued)" UX hint and the WAL persistence step
        (PR-intervention-link L3) so a crash mid-await leaves the dispatch
        on disk for resume to re-enqueue.

        Corresponds to session._dispatch_intervention.
        """
        # Persist BEFORE awaiting so a crash mid-await leaves the WAL
        # with the dispatch event.  UserIntervention.to_dict excludes the
        # volatile future field.
        await self._journal.record_intervention_dispatched(
            intervention_id=iv.id, iv_dict=iv.to_dict(),
        )
        # Pre-emit the queued-status hint when this iv won't be the head.
        if not self._registry.is_empty():
            queued = self._registry.queued_count()
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"awaiting answer ({queued} queued)",
                meta=_iv_meta(iv),
            ))
        try:
            return await self._registry.dispatch(iv)
        finally:
            # Resolve event covers all exit paths (answered, cancelled,
            # task abort).  Idempotent in the journal so duplicate cleanup
            # via _drop_interventions_for_run is safe.
            await self._journal.record_intervention_resolved(
                intervention_id=iv.id,
            )


__all__ = ["InterventionHandler"]
