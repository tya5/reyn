"""#4497 Phase 1: resident-memory measurement — count + approximate bytes
per major in-process container, no threshold, no eviction.

Separated from #4387 at close (owner, 2026-08-13): #4387's byte-capping
landed (6 mechanisms bounded), but the *symptom* that motivated it — the
owner's real environment reaching ~6GB RSS — was never attributed to any
specific container. This module is the measurement surface that lets an
operator, next time RSS is high, answer "what is actually dominant" instead
of guessing.

**Deliberately NOT the same surface as `reyn media stats` /
`reyn storage stats`** (#4485/#4488) — those read `.reyn/` FILES from a
fresh CLI process, which can see another process's disk writes but cannot
see another process's live memory. The containers this module measures
(a live `Session`'s own attributes, plus process-global module-level
``WeakKeyDictionary`` registries) only exist inside the ONE process that's
actually using that memory — the only way to measure them is from *inside*
that process, which is why this ships as a slash command (`/resident` in
`reyn chat`) rather than a CLI subcommand. Naming it the same as the disk
surface was considered and rejected (lead-coder's own correction) — the
same name for two different measurement domains is exactly the "same name,
two meanings" defect class this repo has hit repeatedly.

**Approximate, on purpose.** `sys.getsizeof` is SHALLOW (a container's own
overhead + its direct elements' own header sizes, not a recursive walk of
everything each element references) — a precise, recursive accounting
would be expensive enough to distort the very memory picture it's trying
to measure. The issue's own framing: the goal is "which container is
DOMINANT", not a byte-accurate ledger — count + a cheap approximation is
enough to answer that.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.runtime.session import Session


@dataclass(frozen=True)
class ContainerStat:
    """One row of the resident-memory report: a container's item count and
    its shallow approximate byte footprint. Neither field claims precision
    — see the module docstring."""
    name: str
    count: int
    approx_bytes: int


def _approx_bytes(container: Any) -> int:
    """Shallow size estimate: the container's own overhead plus each
    direct element's own `sys.getsizeof` (dict: both key and value; other
    iterables: each item) — never recurses into what an element itself
    references. `None` (a container some Session states legitimately
    leave unset, e.g. `_router_loop_delegations` outside an active router
    turn) contributes 0, not an error."""
    if container is None:
        return 0
    total = sys.getsizeof(container)
    try:
        if hasattr(container, "items"):
            for k, v in container.items():
                total += sys.getsizeof(k) + sys.getsizeof(v)
        else:
            for item in container:
                total += sys.getsizeof(item)
    except TypeError:
        pass
    return total


def _stat(name: str, container: Any) -> ContainerStat:
    count = 0 if container is None else len(container)
    return ContainerStat(name=name, count=count, approx_bytes=_approx_bytes(container))


# The 9 session-lifetime containers #4497's own issue body enumerates as
# unmeasured (`history` is excluded — already byte-capped and tracked by
# #4468's own mechanism; included below anyway, via the same shallow
# measurer, so this report presents one consistent method across every
# row rather than mixing an internally-tracked figure for one row with
# estimates for the rest).
_SESSION_ATTRS = (
    "history",
    "_pending_user_images",
    "_safety_extensions",
    "_inflight_wal_tasks",
    "_buffered_intervention_answers",
    "_router_loop_delegations",
    "_router_loop_agent_replies",
    "_cancel_forward_targets",
    "_allowed_mcp",
)


def session_container_stats(session: "Session") -> "list[ContainerStat]":
    """Count + approximate bytes for each of #4497's 9 enumerated
    session-lifetime containers (plus `history`, see module note) on
    *session*. A container not present on this Session build (a future
    rename/removal) is silently skipped, not an error — this is a
    diagnostic surface, not a schema contract."""
    stats = []
    for attr in _SESSION_ATTRS:
        if hasattr(session, attr):
            stats.append(_stat(attr, getattr(session, attr)))
    return stats


def process_global_container_stats() -> "list[ContainerStat]":
    """Count + approximate bytes for the 3 process-global
    ``WeakKeyDictionary`` registries #4497's issue names — these are
    NOT per-session; they aggregate every session/loop this PROCESS has
    ever touched (bounded by garbage collection reclaiming a dead
    session/loop key, not by this module). Imported lazily so this
    module itself carries no import-time coupling to the 3 owning
    modules — a diagnostic surface should not widen anyone's import
    graph just by existing.
    """
    stats = []
    try:
        from reyn.hooks.external_fire import _session_bridges
        stats.append(_stat("_session_bridges", _session_bridges))
    except ImportError:
        pass
    try:
        from reyn.core.events.snapshot_generations import _REWIND_INDEXES
        stats.append(_stat("_REWIND_INDEXES", _REWIND_INDEXES))
    except ImportError:
        pass
    try:
        from reyn.core.op_runtime.path_locks import _LOCKS_BY_LOOP
        # #2782: the INNER dict[str, Lock] grows per distinct path a loop
        # has touched — the outer WeakKeyDictionary's own count (number of
        # loops) undersells this. Sum the inner dicts' lengths too, as a
        # second figure on the same row, not a second row (it's one
        # container's own two-level shape, not two containers).
        inner_paths = sum(len(v) for v in _LOCKS_BY_LOOP.values())
        base = _stat("_LOCKS_BY_LOOP", _LOCKS_BY_LOOP)
        stats.append(
            ContainerStat(
                name=f"{base.name} ({inner_paths} path-keys across "
                     f"{base.count} loop(s))",
                count=base.count,
                approx_bytes=base.approx_bytes,
            ),
        )
    except ImportError:
        pass
    return stats
