"""Ambient turn identity (``chain_id``) for the LLM cost path (#3339).

Per-call token/cost figures are produced deep inside the single
``litellm.acompletion`` chokepoint (``reyn.llm.llm.recorded_acompletion``),
which receives no turn identity — while the turn key itself (``chain_id``) is
minted per user submission at the top of the session and carried only by the
``turn_started`` / ``turn_completed`` lifecycle events. Without a shared
carrier the two paths never meet, so a per-call cost can never be grouped back
into the turn that caused it.

The carrier is a ContextVar, mirroring the ``reyn.core.events.events`` ambient
EventLog (#1669) for the same reason: threading a parameter through every
intermediate call site would be churn AND incomplete (compaction / judge /
dogfood callers have no turn to thread). ContextVars copy into child asyncio
tasks at spawn, so a set at the turn seam propagates to every LLM call that
turn makes, including tool-loop iterations and compaction triggered inside it.

**Unset is a first-class state, never "the most recent turn".** An LLM call
made with no turn in scope reads ``None`` here, and the cost path files it
under no turn bucket at all. Attributing such a call to the last-seen turn
would be a fabricated number.

Which calls actually see which turn (enumerated against the single production
binding site, ``Session._run_router_loop``):

- **Every router turn, of every kind** — user / hook / pipeline_result /
  agent_request / agent_response — funnels through that seam, so its LLM
  calls (each tool-loop iteration, plus the in-turn pre-frame compaction)
  are billed to that turn.
- **A sub-agent's turn is billed to the SUB-AGENT's own turn, not the
  parent's.** The inheritance is real — a child task copies the parent's
  context at spawn, so it starts out carrying the parent's chain_id — but the
  sub-agent's work arrives as an ``agent_request`` turn, which re-enters this
  seam and REBINDS to its own chain_id. Parent and child turns are therefore
  billed separately. (An LLM call a sub-agent path made *without* re-entering
  the seam would silently inherit the parent's turn — it would raise nothing
  and report ``None`` nowhere. As of #3339 no such path exists: ``spawn_agent``
  only creates the agent, and a spawned session's LLM work runs through its
  own run loop.)
- **Genuinely turnless**, and recorded under no turn: the ``/compact`` slash
  command (a client-side command, dispatched without ever entering the inbox
  since #3595 S5, that calls ``Session._compact_now_for_op`` directly — so its
  compaction call has no turn), and the dev/dogfood surfaces
  (``reyn.dev.dogfood``), which pass ``recorder=None`` and are not recorded
  at all.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

_active_turn_chain_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "reyn_active_turn_chain_id", default=None,
)


def get_active_turn_chain_id() -> str | None:
    """The ``chain_id`` of the turn in scope for the current task, or ``None``.

    ``None`` means "this LLM call belongs to no turn" — the cost path MUST
    treat it as unattributable rather than folding it into any turn bucket.
    """
    return _active_turn_chain_id.get()


@contextmanager
def active_turn(chain_id: str | None) -> Iterator[None]:
    """Bind *chain_id* as the ambient turn identity for the enclosed block.

    Resets to the previous value on exit (including on exception / cancel), so
    a turn's identity can never leak into whatever the task does next.
    """
    token = _active_turn_chain_id.set(chain_id)
    try:
        yield
    finally:
        _active_turn_chain_id.reset(token)
