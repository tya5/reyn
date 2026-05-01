"""AgentProfile — per-agent metadata persisted to .reyn/agents/<name>/profile.yaml.

PR10 keeps the schema minimal (`name`, `role`, `created_at`). Per-agent
overrides for model / output_language / allowed_skills / permissions are
deferred to subsequent PRs (PR11+).

The `role` text is injected into the LLM's system prompt by `llm._system_prompt`
so each agent gets a distinct persona without changing the OS layer.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml


PROFILE_FILENAME = "profile.yaml"


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str = ""
    created_at: str = ""

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
        return cls(
            name=str(data.get("name", agent_dir.name)),
            role=str(data.get("role", "") or ""),
            created_at=str(data.get("created_at", "") or ""),
        )

    def save(self, agent_dir: Path) -> None:
        agent_dir.mkdir(parents=True, exist_ok=True)
        path = agent_dir / PROFILE_FILENAME
        path.write_text(
            yaml.safe_dump(asdict(self), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )


__all__ = ["AgentProfile", "PROFILE_FILENAME"]
