"""FP-0043 Stage 4b-3a: cron-transport session routing (registry-only, unit-testable).

Maps a fired cron job to its OWN conversation Session of the target agent via the
routing-key primitive (registry.resolve_session). Kept free of any web / chainlit
import so the mapping + run-binding is unit-tested directly with a registry; the
web-server cron runner (``_inbox_pusher``) is the thin glue that supplies the
registry + delivers the envelope.

Behaviour change note (FP-0043 S4b-3a, owner-approved): a message-based cron job's
delivery moves from the agent's shared "main" session to a ``cron:<job_name>``
mapping — each job is its own conversation, PERSISTENT per job (the stable job name
resumes the same Session across fires, so the conversation accumulates a history of
prior runs = "what changed since last run"). Skill-based cron jobs stay headless
(``SkillRuntime.run``); standalone ``reyn cron run`` (no registry) is unchanged.
"""
from __future__ import annotations

CRON_TRANSPORT = "cron"


def cron_session_id(job_name: str) -> str:
    """The logical session-id (routing-key) for a cron job: ``cron:<job_name>``."""
    return f"{CRON_TRANSPORT}:{job_name}"


def resolve_cron_session(registry, agent_name: str, job_name: str):
    """Resolve (get-or-spawn) the persistent ``cron:<job_name>`` Session of
    ``agent_name`` and boot its run-loop so the scheduled turn is processed.

    Steps (pure registry + session ops):
      1. ``resolve_session(agent, "cron", job_name)`` — get-or-spawn by routing-key
         (persistent: the same job resumes the same Session across fires).
      2. ``ensure_session_running`` — boot the run-loop WITHOUT a forwarder (cron is
         unattended; output handling is the S4b-3b notify layer, not the REPL sink).

    Idempotent. Returns the resolved Session."""
    session = registry.resolve_session(agent_name, CRON_TRANSPORT, job_name)
    registry.ensure_session_running(agent_name, cron_session_id(job_name))
    return session
