"""Plan-step context var (= issue #214 split from #180 #2).

When a plan step's sub-``RouterLoop`` calls ``invoke_skill``, the spawned
skill's OSRuntime / EventLog needs to know which plan step it belongs to
so the TUI's SkillActivityRow can render ``"plan N/M"`` detail. Threading
that information through every intermediate layer (RouterLoop → tool
dispatcher → SkillRunner → SkillRuntime.run) would touch many call sites for a
single signal.

A ``ContextVar`` is the right tool: planner sets it before each step's
sub-loop runs, the spawn site reads it at ``agent.run`` construction
time, and ``asyncio.Task`` snapshots the current context at creation so
the var propagates through any sub-tasks the runner spawns.

Public surface is the ``set_plan_step()`` context manager and the
``current_plan_step()`` helper — direct access to the ContextVar is
discouraged so test sites can be audited.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Iterator

# {"n_done": int, "n_total": int, "step_id": str}. None = no plan scope.
_plan_step_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "reyn_plan_step",
    default=None,
)


def current_plan_step() -> dict | None:
    """Return the plan_step dict for the current context, or None."""
    return _plan_step_var.get()


@contextmanager
def set_plan_step(
    *, n_done: int, n_total: int, step_id: str,
) -> Iterator[dict]:
    """Set the plan_step for the lifetime of the ``with`` block.

    ``n_done`` is 1-based (= the step number the user sees, including
    the currently-executing one). Callers reset via the context exit;
    no explicit unset needed.
    """
    payload = {
        "n_done": int(n_done),
        "n_total": int(n_total),
        "step_id": str(step_id),
    }
    token = _plan_step_var.set(payload)
    try:
        yield payload
    finally:
        _plan_step_var.reset(token)


__all__ = ["current_plan_step", "set_plan_step"]
