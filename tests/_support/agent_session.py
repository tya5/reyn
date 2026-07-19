"""Shared test helper: construct ``Session`` through its ``Agent`` identity
object (single source of truth) — the only construction path since
#3133 Priority-0 step-2 made ``agent: Agent`` a required ``Session`` param
and removed the 9 duplicate flat identity kwargs (``agent_name`` /
``agent_role`` / ``model`` / ``permission_resolver`` / ``workspace_base_dir``
/ ``workspace_state_dir`` / ``sandbox_config`` / ``sandbox_backend`` /
``environment_backend``).

Production construction (``scoped_session_factory.py``) builds an ``Agent``
from the identity-owned inputs and passes it as ``agent=`` alone (no
duplicate flat forwarding — step-2 also stopped the factory's ``**base``
double-pass). This helper mirrors that exact production shape: it pops the
identity kwargs out of ``kwargs``, builds the ``Agent`` from them, and calls
``Session(agent=agent, **kwargs)`` with the identity kwargs no longer present
— so ``self._agent`` (Agent SSoT) and the ``self.agent_name`` property
Session still reads directly in a couple of spots (``_build_retrieval_bundle``,
``MediaStore`` wiring) are equal by construction, not by coincidence.
"""
from __future__ import annotations

from typing import Any

from reyn.runtime.agent import Agent
from reyn.runtime.session import Session

# Identity fields owned by Agent (see reyn.runtime.agent.Agent). Extracted
# here (by their pre-step-2 Session-kwarg name, which differs from the Agent
# field name only for role/agent_role) so the helper can build the Agent
# instance from whichever of these a call site happens to pass. These keys
# are POPPED out of ``kwargs`` before the ``Session(...)`` call — step-2
# removed them from Session's signature, so forwarding them flat would now
# raise ``TypeError``.
_AGENT_FIELD_FROM_KWARG = {
    "agent_name": "agent_name",
    "agent_role": "role",
    "model": "model",
    "permission_resolver": "permission_resolver",
    "workspace_base_dir": "workspace_base_dir",
    "workspace_state_dir": "workspace_state_dir",
    "sandbox_config": "sandbox_config",
    "sandbox_backend": "sandbox_backend",
    "environment_backend": "environment_backend",
}


def make_session(*, role: str | None = None, **kwargs: Any) -> Session:
    """Build a ``Session`` via an explicit ``Agent`` (identity SSoT).

    Accepts every kwarg the pre-migration flat ``Session(...)`` call sites
    already passed — ``agent_name`` / ``agent_role`` / ``model`` / the other
    Agent-owned fields, plus anything else Session's constructor takes
    (``state_log``, ``budget_tracker``, ``safety``, ...). ``role`` is the
    #3133-architect-authored alias for ``agent_role`` (both are accepted so a
    call site can migrate ``Session(...)`` -> ``make_session(...)``
    byte-for-byte, keyword-for-keyword, with no per-site kwarg rename
    required).

    ``agent_name`` defaults to ``"test-agent"`` when the caller supplies
    neither (Session's own ``agent`` param has no default, so every prior
    direct-construction call site necessarily passed an identity already —
    this default only covers the degenerate case of calling the helper with
    no identity kwargs at all).
    """
    if role is not None:
        kwargs.setdefault("agent_role", role)
    kwargs.setdefault("agent_name", "test-agent")

    agent_field_kwargs = {
        agent_field: kwargs.pop(kwarg_name)
        for kwarg_name, agent_field in _AGENT_FIELD_FROM_KWARG.items()
        if kwarg_name in kwargs
    }
    agent = Agent(**agent_field_kwargs)
    return Session(agent=agent, **kwargs)
