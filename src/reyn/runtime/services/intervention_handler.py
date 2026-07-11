"""InterventionHandler — user-facing ask_user flow routing.

Extracted from Session (FP-0019 Wave 2 part 1).  Owns the
ask_user dispatch path, intervention announcement to outbox, and
answer delivery coordination.

Depends on InterventionRegistry (pre-FP-0019 extraction,
snapshot_journal).

Design constraints (same pattern as other Wave 1/1b services):
- Injected deps at construction (typed + Callable callbacks).
- No direct reference to Session.
- All state mutations go through injected event_log (P6).
- No domain-specific strings (P7).
"""
from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from reyn.runtime.outbox import OutboxMessage
from reyn.user_intervention import (
    InterventionAnswer,
    UserIntervention,
    match_choice,
)

if TYPE_CHECKING:
    from reyn.core.events.events import EventLog
    from reyn.runtime.services.intervention_registry import InterventionRegistry
    from reyn.runtime.services.snapshot_journal import SnapshotJournal

logger = logging.getLogger(__name__)


# Issue #261 / #254 Phase 4 follow-up — source-agent threading.
#
# When ``Session.handle_intervention`` takes the ``parent_delegate``
# branch, it sets this var to the name of the agent that decided to
# delegate (= the *upstream* / source agent, i.e. the recipient who
# couldn't answer locally and forwarded to its parent). The downstream
# ``user_channel.deliver`` path then reads the var inside ``_iv_meta``
# and stamps ``meta["source_agent"]`` on the outbox message.
#
# Multi-hop chains (A → B → C): each ``parent_delegate`` overwrites the
# var with the immediate delegator, so when C eventually reaches
# ``user_channel``, ``meta["source_agent"]`` reflects B (= the direct
# parent of C in the chain). This matches the "immediate parent only"
# semantics noted in issue #261's "Out of scope" — multi-hop
# breadcrumbs would be a separate feature.
#
# Default value is ``None`` (= no delegation chain). When ``None``, the
# meta builder omits the ``source_agent`` key entirely, preserving the
# Phase 2 outbox-meta-shape commitment for non-delegated paths
# (``test_outbox_intervention_meta_shape_is_stable`` still passes).
source_agent_var: ContextVar[str | None] = ContextVar(
    "source_agent", default=None,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _neutralize_terminal(value: str) -> str:
    """Strip ESC / control sequences from an LLM-derived intervention leaf before
    it reaches any inline terminal renderer (#2770). Intervention content — the
    prompt / detail / suggestions / choice labels — is untrusted (ask_user args
    come straight from a model tool-call; permission prompts interpolate a
    model-controlled path), and no downstream renderer neutralizes it. This
    applies the SAME terminal neutralizer present's leaf seam uses
    (``core/present/guard``, FP-0054), so a control/ESC sequence cannot drive the
    terminal. Kept as a module helper so EVERY inline path that echoes
    intervention content — the ``announce`` scrollback AND the unknown-choice
    status hint (``deliver_answer_to``) — neutralizes identically: one seam, no
    drift, no un-guarded echo path."""
    from reyn.core.present.guard import get_neutralizer

    return get_neutralizer("terminal").neutralize(value)[0]


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
    if iv.actor:
        out["actor"] = iv.actor
    if iv.choices:
        out["choices"] = [
            {"id": c.id, "label": c.label, "hotkey": c.hotkey}
            for c in iv.choices
        ]
    if iv.suggestions:
        out["suggestions"] = list(iv.suggestions)
    # Issue #261 — source_agent stamping for the parent_delegate branch.
    # See ``source_agent_var`` module docstring above for the chain
    # semantics. Omitted when the var is at its default (``None``) so
    # the meta shape stays identical to the non-delegated path.
    src = source_agent_var.get()
    if src:
        out["source_agent"] = src
    return out


class InterventionHandler:
    """Routes user-input answers to pending ask_user interventions.

    Extracted from Session (FP-0019 Wave 2 part 1).

    Parameters
    ----------
    intervention_registry:
        :class:`~reyn.runtime.services.intervention_registry.InterventionRegistry`
        owning the active intervention queue.
    journal:
        :class:`~reyn.runtime.services.snapshot_journal.SnapshotJournal` for WAL
        persistence (``intervention_dispatched`` / ``intervention_resolved``
        events — PR-intervention-link L3).
    event_log:
        Session-scoped :class:`~reyn.core.events.events.EventLog`.  All audit
        events (``user_answered_intervention``) are emitted here (P6).
    put_outbox:
        Async callable ``(OutboxMessage) -> None`` — forwards intervention
        announcements and status hints to the session outbox.
    append_history:
        Sync callable ``(role, text, ts, meta) -> None`` — appends a
        conversational history entry for answered interventions.  The
        callable receives the same positional kwargs as Session's
        internal ``_append_history`` helper, except it is simplified to
        only the fields InterventionHandler needs.
    threat_scan:
        Optional ``ThreatScanConfig`` (FP-0050 / #1862, EP7).  When an
        answer is delivered from an *external* peer (A2A POST / webhook,
        ``external_source=True``), the **history-bound copy** of the
        answer text is structurally fenced via
        :func:`~reyn.security.content_guard.fence_if_enabled` before it
        reaches conversation context.  The future-resolved / buffered /
        choice-matched answer and the audit event stay **raw** — only the
        context sink is fenced, so the A2A round-trip (buffer + choice-id)
        is unaffected.  ``None`` disables fencing entirely.
    """

    def __init__(
        self,
        *,
        intervention_registry: "InterventionRegistry",
        journal: "SnapshotJournal",
        event_log: "EventLog",
        put_outbox: "Callable[[OutboxMessage], Awaitable[None]]",
        append_history: "Callable[[str, str, str, dict], None]",
        threat_scan: "Any | None" = None,
    ) -> None:
        self._registry = intervention_registry
        self._journal = journal
        self._events = event_log
        self._put_outbox = put_outbox
        self._append_history = append_history
        self._threat_scan = threat_scan

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

    async def deliver_answer_to(
        self,
        iv: UserIntervention,
        text: str,
        *,
        choice_id_override: str | None = None,
        external_source: bool = False,
        attribution: "dict | None" = None,
    ) -> bool:
        """Resolve ``iv`` with ``text``, append a user-history entry, emit the
        ``user_answered_intervention`` event.

        Wraps ``InterventionRegistry.deliver_answer`` with the session-level
        side effects (history + audit event + unknown-choice hint).  Returns
        True when the user input was consumed (answer set OR unrecognized
        choice hint emitted — both suppress a fresh router turn).

        ``external_source`` (FP-0050 / #1862, EP7): True iff the answer came
        from an untrusted peer (A2A POST / webhook — set only by
        ``Session.answer_pending_intervention``). When True and a
        ``threat_scan`` config is present, **only** the history-bound copy
        of ``text`` is structurally fenced; the future resolution, buffered
        answer, choice match, and audit event all stay raw so the A2A
        round-trip (buffer + choice-id matching) is unchanged. Local UI
        callers (TUI / slash / chainlit) leave the default ``False`` →
        unfenced.

        Corresponds to session._deliver_answer_to.
        """
        if iv.future.done():
            return False
        resolved = await self._registry.deliver_answer(
            iv, text, choice_id_override=choice_id_override,
        )
        if not resolved and iv.choices:
            # No-match path: surface hint, but consume the input so the
            # router doesn't run on a stray hotkey-attempt. The choice labels are
            # LLM-derived (ask_user options) and this status message reaches the
            # SAME inline terminal surface as announce with no renderer-side
            # guard — neutralize each label here too (#2770, GAP-1), or a control/
            # ESC sequence in an option leaks through the invalid-choice path.
            hint = " / ".join(_neutralize_terminal(c.label) for c in iv.choices)
            await self._put_outbox(OutboxMessage(
                kind="status",
                text=f"unknown choice; expected one of: {hint}",
                meta=_iv_meta(iv),
            ))
            return True
        if not resolved:
            return False
        # Successfully resolved: append history + emit audit event.
        # issue #292 (α): peer-provided choice_id_override is the
        # authoritative choice for audit; otherwise match_choice on
        # text gives the same result as the resolved registry entry.
        if choice_id_override is not None:
            choice = next(
                (c for c in iv.choices if c.id == choice_id_override),
                None,
            )
        else:
            choice = match_choice(text, iv.choices) if iv.choices else None
        # FP-0050 / #1862 (EP7): fence ONLY the history-bound copy of an
        # external peer answer — the context sink. The raw `text` above
        # already drove future resolution / buffer / choice match; the
        # audit event below keeps raw text (P6 audit truth). Fencing here
        # is the single point that touches LLM context, so the A2A
        # round-trip (buffer + choice-id) is unaffected.
        history_text = text
        if external_source and self._threat_scan is not None:
            from reyn.security.content_guard import fence_if_enabled
            history_text = fence_if_enabled(text, self._threat_scan)
        meta = {
            "answered_actor": iv.actor or "",
            "answered_run_id": iv.run_id or "",
            "intervention_id": iv.id,
            "intervention_kind": iv.kind,
        }
        if external_source:
            # #1827 S4: seam-agnostic untrusted-source taint marker. While this
            # external (A2A / webhook peer) answer is live in context, the agent
            # is capability-narrowed (context-auto) — defense-in-depth with the
            # fence above. The key is the convention in
            # reyn.security.permissions.capability_profile.UNTRUSTED_META_KEY.
            meta["external_source"] = True
        self._append_history("user", history_text, _now_iso(), meta)
        # ADR-0039 P3: fold in wire attribution (auth_user_id + connection id) so
        # a remote grant is attributable to WHO granted (the identity) and WHICH
        # terminal (the connection). Local UI callers pass None → shape unchanged.
        attrib = dict(attribution) if attribution else {}
        self._events.emit(
            "user_answered_intervention",
            intervention_id=iv.id,
            kind=iv.kind,
            run_id=iv.run_id,
            actor=iv.actor,
            choice_id=choice.id if choice else None,
            answer_text=text if not iv.choices else "",
            **attrib,
        )
        return True

    async def announce(self, iv: UserIntervention) -> None:
        """Format and publish an intervention to the outbox for the renderer.

        Actor / run_id provenance lives in ``meta`` — the renderer prepends a
        ``[actor#abcd]`` tag via ``meta["actor"]`` + ``meta["run_id_short"]``,
        so we don't repeat it in ``text``.

        Rendering discipline (#2770, display-layer unification with ``present``):
        intervention content is LLM-derived / untrusted (ask_user ``prompt`` /
        ``suggestions`` come straight from a model tool-call; permission prompts
        interpolate a model-controlled ``path``). Every leaf here is routed
        through the SAME terminal neutralizer ``present``'s leaf seam uses
        (``core/present/guard.get_neutralizer("terminal")`` — ESC/control strip,
        FP-0054) BEFORE it reaches any renderer, closing a terminal-injection
        surface. This is intervention's binding-seam analog of present's
        ``resolve_bindings`` (neutralize at the source, render with the shared
        pure primitive). ``meta["nodes"]`` is a ``present``-shaped render model
        the inline CUI draws through the same ``render_presentation_nodes``
        primitive as ``present`` (owner: "rendering consistency"); ``text`` stays
        a plain neutralized string so every OTHER renderer that reads it (the
        ``--cui`` ConsoleChatRenderer, the Rich Panel path, log fallbacks) is
        equally guarded. The two-way-pause flow (registry / future) is untouched.

        Corresponds to session._announce_intervention.
        """
        _clean = _neutralize_terminal

        # Plain-text fallback (msg.text) — neutralized so the non-inline renderers
        # that consume it raw are guarded too, not only the nodes path.
        lines: list[str] = []
        prompt_line = f"Question: {_clean(iv.prompt)}" if iv.kind == "ask_user" else _clean(iv.prompt)
        lines.append(prompt_line)
        if iv.detail:
            lines.append(f"  {_clean(iv.detail)}")
        if iv.suggestions:
            lines.append(f"  options: {' / '.join(_clean(s) for s in iv.suggestions)}")
        if iv.choices:
            lines.append(f"  {' / '.join(_clean(c.label) for c in iv.choices)}")

        # present-shaped render model (neutralized leaves) for the inline CUI's
        # shared render_presentation_nodes path.
        nodes: list[dict] = [{"component": "text", "text": prompt_line}]
        if iv.detail:
            nodes.append({"component": "text", "text": _clean(iv.detail)})
        if iv.suggestions:
            nodes.append({"component": "list", "items": [_clean(s) for s in iv.suggestions]})
        if iv.choices:
            nodes.append({"component": "list", "items": [_clean(c.label) for c in iv.choices]})

        await self._put_outbox(OutboxMessage(
            kind="intervention",
            text="\n".join(lines),
            meta={**_iv_meta(iv), "nodes": nodes},
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
