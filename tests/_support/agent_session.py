"""Shared test helper: construct ``Session`` through its ``Agent`` identity
object (single source of truth), instead of the pre-#3133-Priority-0 pattern
of 259 test call sites constructing ``Session`` with flat identity kwargs
(``agent_name`` / ``agent_role`` / ``model`` / ``permission_resolver`` /
``workspace_base_dir`` / ``workspace_state_dir`` / ``sandbox_config`` /
``sandbox_backend`` / ``environment_backend``) and leaving ``agent=None``.

Production construction (``scoped_session_factory.py``) always builds an
``Agent`` explicitly and passes it alongside the same flat identity kwargs
(see ``build_scoped_chat_session`` — the identity params still flow through
``**base`` for Session's direct/test fallback path; pruning that duplication
is #3133 Priority-0 step-2, a signature change out of scope here). This
helper mirrors that exact production shape: it builds the ``Agent`` from the
identity-owned kwargs *and* forwards the same kwargs to ``Session`` flat, so
``self._agent`` (Agent SSoT) and the local ``agent_name`` variable Session
still reads directly in a couple of spots (``_build_retrieval_bundle``,
``MediaStore`` wiring) stay equal by construction rather than by
coincidence.

``Session.__init__`` itself is unchanged by this helper (step-1 is
test-only) — it still accepts either ``agent=`` or the flat identity kwargs.
This helper simply makes every migrated test call site exercise the same
"build an Agent, pass it in" path production code uses.
"""
from __future__ import annotations

from typing import Any

from reyn.runtime.agent import Agent
from reyn.runtime.session import Session

# Identity fields owned by Agent (see reyn.runtime.agent.Agent). Extracted
# here (by their Session-kwarg name, which differs from the Agent field name
# only for role/agent_role) so the helper can build the Agent instance from
# whichever of these a call site happens to pass, while leaving them in
# ``kwargs`` too — Session forwards them onward exactly as it did pre-helper.
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
    already pass — ``agent_name`` / ``agent_role`` / ``model`` / the other
    Agent-owned fields, plus anything else Session's constructor takes
    (``state_log``, ``budget_tracker``, ``safety``, ...). ``role`` is the
    #3133-architect-authored alias for ``agent_role`` (both are accepted so a
    call site can migrate ``Session(...)`` -> ``make_session(...)``
    byte-for-byte, keyword-for-keyword, with no per-site kwarg rename
    required).

    ``agent_name`` defaults to ``"test-agent"`` when the caller supplies
    neither (Session's own ``agent_name`` param has no default, so every
    prior direct-construction call site necessarily passed one already —
    this default only covers the degenerate case of calling the helper with
    no identity kwargs at all).
    """
    if role is not None:
        kwargs.setdefault("agent_role", role)
    kwargs.setdefault("agent_name", "test-agent")

    agent_field_kwargs = {
        agent_field: kwargs[kwarg_name]
        for kwarg_name, agent_field in _AGENT_FIELD_FROM_KWARG.items()
        if kwarg_name in kwargs
    }
    agent = Agent(**agent_field_kwargs)
    return Session(agent=agent, **kwargs)
