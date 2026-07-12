"""reyn.hooks.event — the typed ``HookEvent`` (Hook-Event Redesign Phase 1,
proposal ``docs/deep-dives/proposals/0059-hook-event-redesign.md`` §1).

reyn has THREE distinct things named "event" (CLAUDE.md's 3-event rule):
audit-event (P6, ``.reyn/events``), WAL-event (crash-recovery,
``.reyn/state/wal.jsonl``), and hook-event (lifecycle + external reactivity
trigger). This module is the hook-event type ONLY — it does not touch, wrap,
or replace the other two. **Naming is deliberate** (proposal §1
review-pass): the type is ``HookEvent`` (never bare ``Event``) and lives in
``reyn.hooks`` (never a module named ``events``) precisely so it cannot
collide with ``reyn.core.events`` (P6 audit) at the identifier level.

Before this module, a hook dispatch carried an ad-hoc ``template_vars: dict``
with no central type — the shape of "what fields does ``turn_end`` carry?"
lived only in docstrings and call-site literals (``hooks/dispatcher.py::
dispatch(point, template_vars)``). ``HookEvent`` gives that payload a type
without changing dispatch's external shape: ``HookDispatcher.dispatch(point,
template_vars)`` keeps its existing signature (byte-identical for every
existing call site); internally the dispatcher wraps the same dict into a
``HookEvent`` for its own bookkeeping (see ``reyn.hooks.dispatcher``).

Phase 1 does NOT add an ``emit_hook_event`` op (LLM-authored hook-events are
Phase 5, proposal §8) — this module is the type + registry foundation only.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HookEvent:
    """A single typed hook-event dispatch.

    Fields
    ------
    kind:
        The namespaced hook-event kind (see ``reyn.hooks.schema_registry``),
        e.g. ``"builtin:lifecycle:turn_end"`` or
        ``"builtin:external:cron_fired"``. Derived from the pre-existing bare
        dispatch-point string (``"turn_end"``, ``"cron_fired"``, ...) via
        ``schema_registry.canonical_kind``.
    payload:
        The event's field dict — the SAME dict every call site already
        assembles (was called ``template_vars``); values are unchanged, only
        now schema-checked at construction (``schema_registry.
        build_hook_payload``) for every builtin producer.
    source:
        The origin tag. Every Phase-1 point ships from reyn's own
        lifecycle/ingress code, so this is always ``"builtin"`` in this
        phase; ``mcp:<server_id>`` / ``webhook:<provider>`` /
        ``llm:<session_id>`` sources are a later phase (proposal §1/§6/§8).
    chain_id:
        Reused from reyn's existing causality field (``turn_start``/
        ``turn_end`` template_vars, ``runtime/session.py``) — NOT a new
        field (proposal §1 reconcile: no causality field is invented here).
    id:
        A fresh identifier per dispatch (uuid4 hex) — informational, not
        used for de-duplication or ordering.
    emitted_at:
        Wall-clock dispatch time (informational only; no ordering guarantee
        is derived from it — reyn's Sync dispatch is already
        registration-ordered, per-hook-isolated; see ``HookDispatcher``).
    """

    kind: str
    payload: dict
    source: str = "builtin"
    chain_id: "str | None" = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    emitted_at: float = field(default_factory=time.time)


__all__ = ["HookEvent"]
