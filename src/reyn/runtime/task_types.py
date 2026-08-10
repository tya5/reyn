"""current_task / inbox-item attributes — proposal 0067 P0 (types and state only).

**No behaviour change.** This module exists to give the concepts proposal
0067's "Types and substrate" section names — ``requester``, ``reply_to``,
``kind``, ``collect``, ``on_settle``, ``schema``, ``ttl_seconds``, ``wake`` —
a real, importable, typed home before anything reads or writes them. Nothing
in this PR wires :data:`Session.current_task` into dispatch, attribution, or
delivery; every existing code path (``_last_sender`` / ``_last_reply_to`` /
the untyped ``(kind, payload)`` inbox tuple) is untouched. P1 extracts the
``InboxArbiter`` and is the first consumer.

``Requester`` is #2130's ``(agent, sid)`` addressing primitive, typed —
``session.py``'s own attribution code today carries the same pair as a free-
form ``sender`` string label (e.g. ``"user:tui"``, ``"a2a:<peer_agent>"``);
this type does not replace that label (changing what's rendered to the LLM
would be a behaviour change), it gives P1 a real type to migrate onto.

Field-by-field source, all from proposal 0067 (docs/deep-dives/proposals/
0067-task-model-and-arbiter.md):

- ``requester`` — "typed wrapper over (agent_name, session_id) — #2130's
  primitive" (§ Types and substrate).
- ``reply_to`` — "``TransportRef | None`` — volatile; None after crash"
  (same section); :class:`reyn.runtime.transport.TransportRef` already
  exists and is reused here, not redefined.
- ``kind`` / ``collect`` / ``on_settle`` / ``schema`` / ``ttl_seconds`` —
  the ``run_prompt`` issuing signature (§ Issuing): ``kind`` is the task
  class (`prompt` / `pipeline` / `exec` per §12's retraction "3 kind +
  resource generation"; NOT the same axis as ``ToolDefinition.dispatch_kind``
  or the inbox tuple's ``TurnOrigin``, both pre-existing, unrelated
  classifications this PR does not touch), ``collect`` is `"attached" |
  "async"`, ``on_settle`` is `"deliver" | "<pipeline name>" | "drop"`
  (deliberately ``str``, not a closed enum — a pipeline name is
  registry-defined, not a fixed set).
- ``wake`` — "a task's settle always wakes its issuer (ADR-0040 D5)";
  defaults to ``True`` for that reason, distinct from ``send_to_session``'s
  selectable ``wake`` (§ Delivery, message vs task — messages, not tasks,
  are where "don't wake" applies).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reyn.runtime.transport import TransportRef

TaskKind = Literal["prompt", "pipeline", "exec"]
CollectMode = Literal["attached", "async"]


@dataclass(frozen=True)
class Requester:
    """#2130's ``(agent, session_id)`` addressing primitive, typed.

    ``sid`` is namespaced per agent (``registry.py:591``), so the pair is
    the address — not either half alone (proposal 0067 § Issuing, citing
    ``session.py:3038``'s "#2130 first-class (agent, sid) routing" comment).
    """
    agent_name: str
    session_id: str


@dataclass
class CurrentTask:
    """The present-tense task state a session's inbox arbiter (P1) will
    read and write. Every field is optional/defaulted because nothing
    constructs a non-default instance yet in this PR — the type exists so
    P1 has one to populate, not because P0 populates it.

    Mutable (not frozen): a task's state changes over its own lifetime
    (settling, e.g.) once P1 wires this in; P0 does not mutate it."""
    requester: "Requester | None" = None
    reply_to: "TransportRef | None" = None
    kind: "TaskKind | None" = None
    collect: "CollectMode | None" = None
    on_settle: "str | None" = None
    schema: "str | None" = None
    ttl_seconds: "int | None" = None
    wake: bool = True
