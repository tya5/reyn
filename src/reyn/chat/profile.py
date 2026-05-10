"""AgentProfile — per-agent metadata persisted to .reyn/agents/<name>/profile.yaml.

PR10 introduced the file with the minimal schema (`name`, `role`,
`created_at`). PR15 adds `allowed_skills`: an optional allowlist of
project / stdlib skill names this agent may invoke. stdlib `skill_router`
/ `chat_compactor` are always available — the allowlist only constrains
user-visible skills the router would otherwise hand off to. (FP-0011:
`skill_narrator` was removed; the router LLM narrates inline.)

Semantics for `allowed_skills`:
- absent / null  → no restriction (every project + stdlib skill, default)
- empty list `[]` → router runs (LLM-only replies) but no skill spawn
- `[a, b]`        → only those skill names

PR37 adds `allowed_mcp`: an optional allowlist of MCP server names this
agent may access, layered on top of the project-wide `permissions.mcp`
config. Semantics:
- absent / null  → no per-agent restriction (inherits project config)
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

import yaml

PROFILE_FILENAME = "profile.yaml"


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str = ""
    created_at: str = ""
    # PR15: optional skill allowlist. None = unrestricted (default), [] = no
    # skills at all, [...] = only those names. stdlib router/compactor are NOT
    # subject to this list. (FP-0011: skill_narrator removed.)
    allowed_skills: list[str] | None = None
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
        raw_allowed = data.get("allowed_skills", None)
        if raw_allowed is None:
            allowed: list[str] | None = None
        else:
            # Accept yaml empty mapping `[]` or list of strings; coerce to list[str].
            allowed = [str(s) for s in raw_allowed]
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
            allowed_skills=allowed,
            allowed_mcp=allowed_mcp,
        )

    def save(self, agent_dir: Path) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / PROFILE_FILENAME
        # Hand-roll the dict so absent allowed_skills (None) doesn't appear
        # in the yaml as `null` — keep the on-disk shape minimal.
        payload: dict = {
            "name": self.name,
            "role": self.role,
            "created_at": self.created_at,
        }
        if self.allowed_skills is not None:
            payload["allowed_skills"] = list(self.allowed_skills)
        if self.allowed_mcp is not None:
            payload["allowed_mcp"] = list(self.allowed_mcp)
        path.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


__all__ = ["AgentProfile", "PROFILE_FILENAME"]
