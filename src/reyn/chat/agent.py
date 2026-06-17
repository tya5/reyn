"""Agent — the per-agent IDENTITY value object (FP-0043 Stage 2).

Extracted from ``ChatSession``, which historically fused two concerns: the
**identity** (who the agent is — name, profile role, permissions, workspace
root, exec/FS backends) and the **conversation** (history, inbox, the running
``session.run()`` task). This object owns the identity cluster so that — in a
later stage — N conversation Sessions can SHARE one ``Agent`` (identity is
agent-scoped; conversation is session-scoped). Stage 2 is a pure, byte-identical
extraction: one ``ChatSession`` still holds exactly one ``Agent``, and every
former ``ChatSession`` identity field reads through it via a delegating property
— no observable behaviour changes.

Assembled at the construction chokepoint (``build_scoped_chat_session``), which
already gathers every identity input from the frontend + the ``AgentRegistry``
profile. ``AgentRegistry`` / ``AgentProfile`` / ``AgentSnapshot`` are unchanged
(the snapshot is already conversation-shaped, keyed by ``agent_name`` — identity
extraction stays orthogonal to it).

Scope note (Stage 2, byte-identical): the agent holds ``name`` + ``role`` (the
two identity fields that flow into a ChatSession today), NOT the full
``AgentProfile`` object — threading the profile's allowlists into the session is
NEW wiring deferred to a later stage (when permissions become explicitly
agent-scoped). Conceptually the agent owns the profile; the wiring waits.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from reyn.security.permissions.permissions import PermissionResolver


@dataclass(frozen=True)
class Agent:
    """The agent's identity cluster (FP-0043 Stage 2). Frozen — identity is
    immutable for a session's lifetime (no ChatSession identity field is
    reassigned post-construction; verified)."""

    # Identity proper.
    agent_name: str
    role: str = ""
    model: str = "standard"

    # Authority + scoping.
    permission_resolver: "PermissionResolver | None" = None

    # Workspace-identity root. ``workspace_base_dir`` = the OpContext FS root
    # (container repo when env-backend routes into a container; None → host cwd);
    # ``workspace_state_dir`` = the host-side OS state dir (survives container
    # death). ``workspace_dir`` is DERIVED (see property) — the agent's home under
    # ``.reyn/agents/<name>``.
    workspace_base_dir: "Path | None" = None
    workspace_state_dir: "Path | None" = None

    # Exec + FS seams (agent-level-uniform backends, #1200). ``sandbox_config`` is
    # the exec-tool gating config; ``sandbox_backend`` is the SandboxBackend
    # INSTANCE; ``environment_backend`` is the EnvironmentBackend INSTANCE.
    sandbox_config: Any = None
    sandbox_backend: Any = None
    environment_backend: Any = None

    @property
    def workspace_dir(self) -> Path:
        """The agent's home directory (``.reyn/agents/<name>``) — derived from
        the name, byte-identical to ChatSession's former
        ``Path(".reyn") / "agents" / self.agent_name``."""
        return Path(".reyn") / "agents" / self.agent_name
