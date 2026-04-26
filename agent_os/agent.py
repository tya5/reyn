from .models import App
from .runtime import OSRuntime, RunResult


class Agent:
    def __init__(self, model: str, workspace_dir: str = "./workspace") -> None:
        self.model = model
        self.workspace_dir = workspace_dir
        self._runtime: OSRuntime | None = None

    def run(self, app: App, initial_input: dict, output_language: str = "ja") -> RunResult:
        self._runtime = OSRuntime(app, self.model, self.workspace_dir)
        return self._runtime.run(initial_input, output_language=output_language)

    def get_events(self) -> list:
        return self._runtime.events.all() if self._runtime else []

    def get_events_json(self) -> list[dict]:
        return self._runtime.events.to_json() if self._runtime else []
