"""AgentProfile — per-agent metadata persisted to .reyn/agents/<name>/profile.yaml.

PR10 introduced the file with the minimal schema (`name`, `role`,
`created_at`). PR37 adds `allowed_mcp`: an optional allowlist of MCP server names this
agent may access, layered on top of the project-wide `permissions.mcp`
config. Semantics:
- absent / null  → no restriction (inherits project config)
- `"all"`        → same as null but explicit in YAML for audit clarity
- `[a, b]`       → intersect with project allow-list (per-agent narrowing)

The `role` text is injected into the LLM's system prompt by
`llm._system_prompt` so each agent gets a distinct persona without
changing the OS layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from reyn.security.permissions.capability_profile import CapabilityProfile

PROFILE_FILENAME = "profile.yaml"


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str = ""
    created_at: str = ""
    # PR37: optional MCP server allowlist. None = no per-agent restriction
    # (inherits project config). "all" in YAML normalizes to None here.
    # list[str] = intersect with project allow-list.
    allowed_mcp: list[str] | None = None

    @classmethod
    def new(cls, name: str, role: str = "") -> "AgentProfile":
        return cls(
            name=name,
            role=role,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @classmethod
    def load(cls, agent_dir: Path) -> "AgentProfile":
        """Load profile.yaml from `agent_dir`. Raises FileNotFoundError if missing."""
        path = agent_dir / PROFILE_FILENAME
        if not path.is_file():
            raise FileNotFoundError(path)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        # PR37: parse allowed_mcp — "all" sentinel normalizes to None.
        raw_allowed_mcp = data.get("allowed_mcp", None)
        if raw_allowed_mcp is None or raw_allowed_mcp == "all":
            allowed_mcp: list[str] | None = None
        else:
            allowed_mcp = [str(s) for s in raw_allowed_mcp]
        return cls(
            name=str(data.get("name", agent_dir.name)),
            role=str(data.get("role", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
            allowed_mcp=allowed_mcp,
        )

    def save(self, agent_dir: Path) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / PROFILE_FILENAME
        # Hand-roll the dict so absent allowed_mcp (None) doesn't appear
        # in the yaml as `null` — keep the on-disk shape minimal.
        payload: dict = {
            "name": self.name,
            "role": self.role,
            "created_at": self.created_at,
        }
        if self.allowed_mcp is not None:
            payload["allowed_mcp"] = list(self.allowed_mcp)
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    def default_profile(self) -> "CapabilityProfile":
        """The agent's default capability spec (#2074 S4a) — the canonical unified
        representation of this agent's per-agent baseline narrowing on the MCP axis.

        The profile.yaml user key ``allowed_mcp`` maps onto the unified spec's
        ``mcp_allow`` axis (the INTERNAL representation). ``None`` passes through
        as ``None`` (= ⊤, unrestricted). #2074 S4b repoints the per-agent ∩ layer
        to read this spec object so one primitive feeds the MCP binding adapter."""
        from reyn.security.permissions.capability_profile import CapabilityProfile

        return CapabilityProfile(
            name=self.name,
            mcp_allow=tuple(self.allowed_mcp) if self.allowed_mcp is not None else None,
        )


__all__ = ["AgentProfile", "PROFILE_FILENAME"]
