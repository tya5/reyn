"""Agent — the per-agent IDENTITY value object (FP-0043 Stage 2).

Extracted from ``Session``, which historically fused two concerns: the
**identity** (who the agent is — name, profile role, permissions, workspace
root, exec/FS backends) and the **conversation** (history, inbox, the running
``session.run()`` task). This object owns the identity cluster so that — in a
later stage — N conversation Sessions can SHARE one ``Agent`` (identity is
agent-scoped; conversation is session-scoped). Stage 2 is a pure, byte-identical
extraction: one ``Session`` still holds exactly one ``Agent``, and every
former ``Session`` identity field reads through it via a delegating property
— no observable behaviour changes.

Assembled at the construction chokepoint (``build_scoped_chat_session``), which
already gathers every identity input from the frontend + the ``AgentRegistry``
profile. ``AgentRegistry`` / ``AgentProfile`` / ``AgentSnapshot`` are unchanged
(the snapshot is already conversation-shaped, keyed by ``agent_name`` — identity
extraction stays orthogonal to it).

Scope note (Stage 2, byte-identical): the agent holds ``name`` + ``role`` (the
two identity fields that flow into a Session today), NOT the full
``AgentProfile`` object — threading the profile's allowlists into the session is
NEW wiring deferred to a later stage (when permissions become explicitly
agent-scoped). Conceptually the agent owns the profile; the wiring waits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reyn.config.infra import _default_agent_id

if TYPE_CHECKING:
    from reyn.security.permissions.permissions import PermissionResolver


@dataclass(frozen=True)
class Agent:
    """The agent's identity cluster (FP-0043 Stage 2). Frozen — identity is
    immutable for a session's lifetime (no Session identity field is
    reassigned post-construction; verified)."""

    # Identity proper.
    agent_name: str
    role: str = ""
    model: str = "standard"
    # ``reyn.yaml`` ``agent.id`` — the audit-event / X-Reyn-Agent-Id identifier
    # (#3133 P0-follow-up: folded in from Session's former standalone
    # ``agent_id`` param + ``_default_agent_id()`` fallback; identity SSoT now
    # owns its own default instead of Session constructing it on Agent's
    # behalf). Immutable — never reassigned post-construction (verified).
    agent_id: str = field(default_factory=_default_agent_id)

    # Authority + scoping.
    permission_resolver: "PermissionResolver | None" = None

    # Workspace-identity root. ``workspace_base_dir`` = the OpContext FS root
    # (container repo when env-backend routes into a container; None → host cwd);
    # ``workspace_state_dir`` = the host-side OS state dir (survives container
    # death). ``workspace_dir`` is DERIVED (see property) — the agent's home under
    # ``.reyn/agents/<name>``.
    workspace_base_dir: "Path | None" = None
    workspace_state_dir: "Path | None" = None

    # Exec + FS seams (agent-level-uniform backends, #1200 — "agent-level"
    # there names the SCOPE the backend stays uniform ACROSS (one agent's
    # own chat/planner/phase run-modes), not "narrowed differently per
    # agent"). ``sandbox_config`` is the exec-tool gating config;
    # ``sandbox_backend`` is the SandboxBackend INSTANCE;
    # ``environment_backend`` is the EnvironmentBackend INSTANCE.
    #
    # #5352 (lens 5, security — reading (A) ruled by architect,
    # issuecomment-5450347844): this field ITSELF is STILL the SAME object
    # for every agent in a process — SessionFactoryConfig.from_config threads
    # the ONE process-wide ``ReynConfig.sandbox`` (loaded once from
    # reyn.yaml/reyn.local.yaml) to every agent's factory construction,
    # unmodified; nothing narrows THIS field per agent name. #5352 answered
    # the open question this disclosure originally left standing ("who sets a
    # per-agent narrowing, operator or spawning model") — BOTH: an agent's own
    # ``profile.yaml`` may declare a ``sandbox:`` narrowing (operator-authored,
    # ``AgentProfile.sandbox``), and a spawning model's ``spawn_session`` tool
    # composes it per a same-agent / cross-agent-declared / cross-agent-
    # undeclared priority table (``RouterHostAdapter.spawn_session`` +
    # ``AgentRegistry.resolved_sandbox_for``). Neither reaches THIS field —
    # both compose on TOP of it, one layer up, at
    # ``Session._sandbox_config`` (the actual per-session/per-agent-effective
    # read every consumer already uses instead of ``Agent.sandbox_config``
    # directly — see that property's own docstring,
    # ``docs/reference/runtime/session-construction.md``). So the
    # consequence named below is now narrower than it reads: it still holds
    # for an agent that declares nothing and was never spawned with an
    # override (the pre-#5352 default), not for the system as a whole.
    sandbox_config: Any = None
    sandbox_backend: Any = None
    environment_backend: Any = None

    @property
    def workspace_dir(self) -> Path:
        """The agent's home directory (``<state-root>/agents/<name>``).

        #3705: previously always ``Path(".reyn") / "agents" / self.agent_name``
        — a LITERAL relative path, resolved against whatever the PROCESS cwd
        happened to be at write time, regardless of ``workspace_state_dir``
        having already been explicitly supplied on this exact object. That
        silent override is why the owner's live `.reyn/agents/` accumulated
        68 directories of test-fixture agents: every caller that built an
        ``Agent``/``Session`` with an isolated ``workspace_state_dir`` (or
        was simply standing in the wrong directory) still had this property
        quietly resolve into the ambient cwd instead.

        Now anchored on ``workspace_state_dir`` when the caller supplied one
        (``env_backend.py`` / ``registry_bootstrap.py`` both set it to
        ``project_root / ".reyn"`` already — this property just has to
        respect it instead of re-deriving its own ``.reyn`` from scratch).
        Falls back to ``Path.cwd() / ".reyn"`` ONLY when no caller supplied
        ``workspace_state_dir`` at all — this preserves the exact prior
        default for every caller that never set it (not a behavior change
        for them), while making an explicitly-supplied root finally take
        effect for the callers that do (which is the actual #3705 fix).
        """
        base = (
            self.workspace_state_dir
            if self.workspace_state_dir is not None
            else Path.cwd() / ".reyn"
        )
        return base / "agents" / self.agent_name
