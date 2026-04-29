from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from .models import Skill
from .runtime import OSRuntime, RunResult
from .model_resolver import ModelResolver
from .permissions import PermissionResolver
from reyn.reporters.persister import EventPersister


class Agent:
    def __init__(
        self,
        model: str,
        state_dir: str = ".reyn",
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        user_input_fn: Callable[[str, list[str]], str] | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
        max_phase_visits: int = 25,
        mcp_servers: dict | None = None,
    ) -> None:
        self.model = model
        self.state_dir = state_dir
        self.strict = strict
        self._subscribers = list(subscribers or [])
        self._user_input_fn = user_input_fn
        self._shell_allowed = shell_allowed
        self._max_phase_visits = max_phase_visits
        self._resolver = resolver or ModelResolver({})
        self._permission_resolver = permission_resolver
        self._mcp_servers = mcp_servers
        self._runtime: OSRuntime | None = None
        self.run_id: str | None = None
        self.events_path: Path | None = None

    def run(self, skill: Skill, initial_input: dict, output_language: str = "ja") -> RunResult:
        self.run_id = self._make_run_id(skill.name)
        self.events_path = Path(self.state_dir) / "runs" / f"{self.run_id}.jsonl"
        persister = EventPersister(self.events_path)

        self._runtime = OSRuntime(
            skill, self.model, self.state_dir,
            strict=self.strict,
            subscribers=[persister] + self._subscribers,
            user_input_fn=self._user_input_fn,
            run_id=self.run_id,
            shell_allowed=self._shell_allowed,
            resolver=self._resolver,
            permission_resolver=self._permission_resolver,
            max_phase_visits=self._max_phase_visits,
            mcp_servers=self._mcp_servers,
        )
        return self._runtime.run(initial_input, output_language=output_language)

    @property
    def phase_artifacts(self) -> list[dict]:
        """Return all artifacts stored during the run, excluding the initial input."""
        if self._runtime is None:
            return []
        return [
            a for a in self._runtime.workspace.artifacts
            if a["phase"] != "_input" and not a["phase"].endswith("_preprocessed")
        ]

    def get_events(self) -> list:
        return self._runtime.events.all() if self._runtime else []

    def get_events_json(self) -> list[dict]:
        return self._runtime.events.to_json() if self._runtime else []

    @staticmethod
    def _make_run_id(skill_name: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_name = skill_name.replace(" ", "_")[:40]
        return f"{ts}_{safe_name}"
