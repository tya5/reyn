from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from .models import App
from .runtime import OSRuntime, RunResult
from .model_resolver import ModelResolver
from .permissions import PermissionResolver
from reyn.reporters.persister import EventPersister


class Agent:
    def __init__(
        self,
        model: str,
        workspace_dir: str = "./workspace",
        strict: bool = False,
        subscribers: list[Callable] | None = None,
        user_input_fn: Callable[[str, list[str]], str] | None = None,
        extra_read_roots: list[str] | None = None,
        shell_allowed: bool = False,
        resolver: ModelResolver | None = None,
        permission_resolver: PermissionResolver | None = None,
    ) -> None:
        self.model = model
        self.workspace_dir = workspace_dir
        self.strict = strict
        self._subscribers = list(subscribers or [])
        self._user_input_fn = user_input_fn
        self._extra_read_roots = extra_read_roots or []
        self._shell_allowed = shell_allowed
        self._resolver = resolver or ModelResolver({})
        self._permission_resolver = permission_resolver
        self._runtime: OSRuntime | None = None
        self.run_id: str | None = None
        self.events_path: Path | None = None

    def run(self, app: App, initial_input: dict, output_language: str = "ja") -> RunResult:
        self.run_id = self._make_run_id(app.name)
        self.events_path = Path(self.workspace_dir) / "runs" / f"{self.run_id}.jsonl"
        persister = EventPersister(self.events_path)

        self._runtime = OSRuntime(
            app, self.model, self.workspace_dir,
            strict=self.strict,
            subscribers=[persister] + self._subscribers,
            user_input_fn=self._user_input_fn,
            run_id=self.run_id,
            extra_read_roots=self._extra_read_roots,
            shell_allowed=self._shell_allowed,
            resolver=self._resolver,
            permission_resolver=self._permission_resolver,
        )
        return self._runtime.run(initial_input, output_language=output_language)

    def get_events(self) -> list:
        return self._runtime.events.all() if self._runtime else []

    def get_events_json(self) -> list[dict]:
        return self._runtime.events.to_json() if self._runtime else []

    @staticmethod
    def _make_run_id(app_name: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_name = app_name.replace(" ", "_")[:40]
        return f"{ts}_{safe_name}"
