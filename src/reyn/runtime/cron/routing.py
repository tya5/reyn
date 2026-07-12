"""FP-0043 Stage 4b-3a: cron-transport session routing (registry-only, unit-testable).

Maps a fired cron job to its OWN conversation Session of the target agent via the
routing-key primitive (registry.resolve_session). Kept free of any web
import so the mapping + run-binding is unit-tested directly with a registry; the
web-server cron runner (``_inbox_pusher``) is the thin glue that supplies the
registry + delivers the envelope.

Behaviour change note (FP-0043 S4b-3a, owner-approved): a message-based cron job's
delivery moves from the agent's shared "main" session to a ``cron:<job_name>``
mapping — each job is its own conversation, PERSISTENT per job (the stable job name
resumes the same Session across fires, so the conversation accumulates a history of
prior runs = "what changed since last run"). Standalone ``reyn cron run`` (no
registry) is unchanged.

#2608 H5: :func:`dispatch_cron_fired` fires the ``cron_fired`` external-event
hook on the job's resolved session — the LAST source in the
external-event->hooks arc (after H1's MCP push, H4's fs-watcher). Called from
the same ingress coroutine as the job's own inbox delivery (see
``reyn.interfaces.web.server``'s cron runner), right after
:func:`resolve_cron_session`.
"""
from __future__ import annotations

from reyn.hooks.ingress import CronIngressAdapter

CRON_TRANSPORT = "cron"

# Hook-Event Redesign Phase 2 (proposal 0059 §6.4): the Cron Adapter is
# stateless (no bound queue/session — it resolves its target Session fresh
# at fire time), so one module-level instance is shared by every call.
_ADAPTER = CronIngressAdapter()


def cron_session_id(job_name: str) -> str:
    """The logical session-id (routing-key) for a cron job: ``cron:<job_name>``."""
    return _ADAPTER.session_id(job_name)


def resolve_cron_session(registry, agent_name: str, job_name: str):
    """Resolve (get-or-spawn) the persistent ``cron:<job_name>`` Session of
    ``agent_name`` and boot its run-loop so the scheduled turn is processed.

    Hook-Event Redesign Phase 2 (proposal 0059 §6.4): delegates to
    ``CronIngressAdapter.resolve_session`` — the out-of-process Session-resolve
    step of the unified Ingress Adapter interface, closed inside the adapter
    (Sync dispatch / a future Async Bus never see it). Byte-identical steps
    (get-or-spawn by routing-key, then boot the run-loop with no forwarder —
    cron is unattended).

    Idempotent. Returns the resolved Session."""
    return _ADAPTER.resolve_session(registry, agent_name, job_name)


def dispatch_cron_fired(session, job_name: str, to: str) -> None:
    """#2608 H5 / Hook-Event Phase 2 §6.4: fire the ``cron_fired`` external-event
    hook on ``session`` (the job's own resolved Session — pass the object
    :func:`resolve_cron_session` returned, so the hook fires on the SAME
    session the job's message was delivered to).

    Delegates to ``CronIngressAdapter``'s ``to_event`` (builds the typed
    ``HookEvent`` via Phase 1's ``build_hook_payload``, unchanged field-set)
    then ``deliver`` (``reyn.hooks.external_fire.fire_and_forget`` — a slow
    hook action must never stall the cron job's own inbox delivery).
    ``template_vars`` carry only operator-authored config metadata
    (``job_name``, the target agent name) — a cron job never carries
    end-user-supplied secrets the way an inbound webhook body can, so
    nothing is withheld here (contrast
    ``reyn.runtime.webhook_routing.dispatch_webhook_received``). ``job_name``
    is the matchable field (exact match — not a glob field, see
    ``reyn.hooks.matcher``), e.g. ``matcher: {job_name: "backup"}``.
    """
    event = _ADAPTER.to_event(job_name, to)
    _ADAPTER.deliver(event, session)
