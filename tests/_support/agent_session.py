"""Shared test helper: construct ``Session`` through its ``Agent`` identity
object (single source of truth) — the only construction path since
#3133 Priority-0 step-2 made ``agent: Agent`` a required ``Session`` param
and removed the 9 duplicate flat identity kwargs (``agent_name`` /
``agent_role`` / ``model`` / ``permission_resolver`` / ``workspace_base_dir``
/ ``workspace_state_dir`` / ``sandbox_config`` / ``sandbox_backend`` /
``environment_backend``). The #3133 P0-follow-up folded a 10th identity
field, ``agent_id``, into ``Agent`` the same way (closing the FP-0016
Component E gap where ``Agent`` lacked the field Session's fallback
comment already named ``agent.id``).

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
from reyn.runtime.services.recovery import build_recovery, default_snapshot_path
from reyn.runtime.session import Session
from reyn.runtime.session_params import ReactivityConfig
from tests._support.session import TEST_MODEL_RESOLVER

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
    # #3133 P0-follow-up: agent_id folded into Agent (identity SSoT) — no
    # longer a separate Session param.
    "agent_id": "agent_id",
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

    ``agent_name`` has no default — every call site must pass it explicitly.
    (#3413: a prior ``kwargs.setdefault("agent_name", "test-agent")`` here
    was verified dead — an AST enumeration of all 282 call sites across 196
    test files found 0 that omitted ``agent_name``, so the default was never
    exercised. Removing it costs nothing today and permanently forecloses
    the hazard the issue raised: a test that deliberately passes a specific
    ``agent_name`` to exercise identity-dependent behaviour could have that
    value silently absorbed by a default if the pass-through were ever
    dropped by a future edit. ``Agent.agent_name`` itself has no default, so
    omitting it now raises ``TypeError`` immediately instead of falling back
    to a fixed literal.)
    """
    if role is not None:
        kwargs.setdefault("agent_role", role)

    agent_field_kwargs = {
        agent_field: kwargs.pop(kwarg_name)
        for kwarg_name, agent_field in _AGENT_FIELD_FROM_KWARG.items()
        if kwarg_name in kwargs
    }
    agent = Agent(**agent_field_kwargs)
    # Session no longer builds its own recovery pair (generation_store ->
    # journal) — build it here from the same inputs the pre-refactor
    # Session.__init__ read internally (recovery-bundle-out-of-Session).
    # ``snapshot_path`` / ``state_log`` / ``session_id`` are PEEKED, not
    # popped: Session's own signature still accepts them (it keeps needing
    # ``self._snapshot_path`` / ``self._state_log`` for its own logic), so
    # they must still flow through ``**kwargs`` below unchanged.
    # #3705: pass workspace_state_dir through when the caller supplied one
    # (via agent_field_kwargs above) — default_snapshot_path's own root=
    # param (added by #3705) is silently unreachable through this helper
    # otherwise: the ~16/223 call sites that DO pass workspace_state_dir
    # would still land at Path.cwd()/".reyn" for their snapshot despite
    # having given an explicit root. root=None (the ~207 call sites that
    # never set workspace_state_dir) keeps the exact prior cwd-relative
    # fallback — unchanged for them.
    snapshot_path = kwargs.get("snapshot_path") or default_snapshot_path(
        agent.agent_name, root=agent.workspace_state_dir,
    )
    generation_store, journal = build_recovery(
        agent.agent_name,
        snapshot_path,
        kwargs.get("state_log"),
        kwargs.get("session_id", "main"),
    )
    # #4349: Session's own default (``resolver or ModelResolver({})``) is
    # genuinely empty now — reyn ships no built-in model catalog to fall
    # back to. A caller here that doesn't care about model resolution
    # still needs "light"/"standard"/"strong" to resolve to SOMETHING, so
    # default to the shared synthetic test resolver unless the caller
    # passed its own (via ``**kwargs``, unchanged for the ~16 call sites
    # that already configure one).
    kwargs.setdefault("resolver", TEST_MODEL_RESOLVER)
    kwargs.setdefault("reactivity", ReactivityConfig())
    return Session(
        agent=agent, generation_store=generation_store, journal=journal, **kwargs,
    )
