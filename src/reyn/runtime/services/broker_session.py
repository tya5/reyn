"""``broker_session_for`` — #5350: derive, never store, the broker
``session_id`` that corresponds to a reyn agent.

Architect ruling (#5350, quoted): "対応を「持つ主体」は居ません。対応は保
存するものではなく導出するものです。broker の変更は要りません" ("there is
no subject that HOLDS the correspondence — it is derived, not saved; broker
needs zero changes").

Both sides already carry the same fact — a **path**:

- reyn: an agent's own ``base_dir`` (``AgentProfile.base_dir`` /
  ``registry.ensure_running(name)``'s working directory).
- broker: a registered session's ``working_dir`` (``server.py`` stores it at
  registration and returns it from ``list_sessions``/``inbox_stats``).

So the correspondence is the JOIN of those two paths — no new ledger, no new
field, no declared identifier. A prior attempt at a declared identifier
(``AgentProfile.broker_identity``, #5084/#5085) was later removed from
reyn's own runtime entirely (#5091/#5095, subject line verbatim: "remove the
'broker' concept from reyn's own runtime") — architect's #5350 ruling is
this repo's SECOND time reaching the same conclusion, not a new opinion.
``broker`` is an operator-chosen MCP-server name under
``external_transports:`` (see ``reyn.config.infra``), never a concept
reyn's own core types carry — this module keeps it that way: it takes
whatever session listing its caller already fetched (however they fetched
it — an MCP ``list_sessions`` call, a test fixture) and returns a plain
``str | None``, no broker-shaped type anywhere in this module's own surface.

Two structural rules the design explicitly calls out (architect, #5350 §3):

1. **Normalize both sides** (``Path(...).resolve()``) before comparing —
   skipping this lets a symlink or a trailing slash silently produce a
   false "no match" even though the same directory is meant on both sides.
2. **Never assume 1:1.** Multiple reyn agents can legitimately share one
   ``base_dir`` (sub-agents in the same project) — the correspondence is
   asked one direction only ("this agent's session?"), never the reverse
   ("this session's agent?", which could have more than one right answer).
   This module does not build or expose an index; it re-derives on every
   call, matching "computed, never persisted" (see the ruling above).

In-repo call sites are 0 by DESIGN, not by omission (architect, #5457
review): ``reyn doctor`` cannot call this — its own charter (D-2, "doctor
never connects") forbids reaching out to broker to fetch a live session
listing. The real consumer is #5410's ``broker_drain.py`` hook, which lives
in an operator's own config repo (``reyn-self``), outside this repo
entirely. A future "0-consumer public surface" census (the #4866/#5442/
#5447 family) should read this paragraph before flagging this function —
the absence is structural, not a leftover.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence


def broker_session_for(
    agent_base_dir: "str | Path", sessions: "Sequence[Mapping[str, Any]]",
) -> "str | None":
    """Return the ``session_id`` of the broker session whose ``working_dir``
    resolves to the same path as ``agent_base_dir``, or ``None`` when no
    registered session matches.

    ``sessions`` is whatever the caller already fetched — the shape
    broker's own ``list_sessions``/``inbox_stats`` MCP tools return: an
    iterable of mappings each carrying at least ``session_id`` and
    ``working_dir`` (see ``server.py``'s own ``sessions[session_id] =
    SessionEntry(session_id=..., working_dir=...)``). This function makes
    no MCP call itself and holds no broker-shaped type — the caller (a
    hook, a future op) is the one that knows how to reach broker; this is
    the pure JOIN step once that data is in hand.

    ``None`` is a NORMAL answer, not a failure: "this agent has no live
    broker session right now" is a true, current fact, not an error to
    raise or paper over. A session entry missing ``working_dir`` (should
    not happen against a real broker, but this function does not trust its
    caller's data blindly) is skipped rather than raising.
    """
    target = Path(agent_base_dir).resolve()
    for session in sessions:
        working_dir = session.get("working_dir")
        if working_dir is None:
            continue
        if Path(working_dir).resolve() == target:
            return session.get("session_id")
    return None
