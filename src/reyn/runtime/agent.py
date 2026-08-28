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
    # issuecomment-5450347844): DESPITE living on a per-agent ``Agent``
    # instance, ``sandbox_config`` today is the SAME object for every
    # agent in a process — SessionFactoryConfig.from_config threads the
    # ONE process-wide ``ReynConfig.sandbox`` (loaded once from
    # reyn.yaml/reyn.local.yaml) to every agent's factory construction,
    # unmodified; no code path narrows it per agent name (repo-wide
    # census, 2026-08-28 — until #5352 answers whether per-agent
    # narrowing is built; that PR's own landing is what would make this
    # claim false, not a byte-count or a date). A field named on a
    # per-agent dataclass reads as a per-agent narrowing point —
    # there is no way to notice from here alone that it is not one.
    # Consequence: reyn.yaml's absolute-path fields (``allow_write_paths``
    # etc.) grant write access to every agent in the process equally,
    # including one whose own worktree differs from the path an operator
    # wrote for a DIFFERENT agent — harmless while every agent in a
    # deployment is equally trusted, live the moment they are not.
    # Deliberately NOT fixed by adding per-agent narrowing here — that is
    # a NEW capability #1200 never decided to build, and a security
    # capability's own promise ("this can be narrowed per agent") needs
    # its own owner-level decision on who sets it (operator vs. a
    # spawning model) before it exists at all — tracked as a SEPARATE
    # question in #5352, not resolved by this disclosure.
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
