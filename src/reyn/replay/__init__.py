"""src/reyn/replay — debug-purpose time travel engine.

Public API
----------

    from reyn.replay import ReplayEngine, Checkpoint, StepFrame, DiffFrame, compare

ReplayEngine
    Walk and seek over a recorded session (WAL + LLM trace dump).
    No LLM calls — deterministic, read-only.

    engine = ReplayEngine("/tmp/b14_run.jsonl")

    # Full walk (step granularity)
    for frame in engine.walk():
        print(frame.checkpoint)

    # Zoom out to phase level
    for frame in engine.walk(scope="phase"):
        print(frame.checkpoint, len(frame.events))

    # Jump to a specific step
    cp = Checkpoint.parse("run_xyz:copy_to_work:3")
    frame = engine.seek(cp)
    print(frame.llm_payload, frame.state_snapshot)

    # List all checkpoints at skill_run granularity
    checkpoints = engine.list_checkpoints(scope="skill_run")

compare
    Diff two recorded sessions step-by-step.

    for diff in compare("/tmp/pre_fix.jsonl", "/tmp/post_fix.jsonl", scope="phase"):
        if diff.has_diff:
            print(diff.events_diff, diff.llm_diff)

TUI reusability (phase 3+)
--------------------------

The engine output is render-agnostic dataclasses.  A Textual widget can import
the same module and bind ``StepFrame`` fields directly to reactive attributes::

    # Example sketch (not implemented — phase 3+ separate PR):
    #
    #   from reyn.replay import ReplayEngine, StepFrame
    #   from textual.reactive import reactive
    #
    #   class StepInspector(Widget):
    #       current_frame: reactive[StepFrame | None] = reactive(None)
    #
    #       def on_mount(self) -> None:
    #           engine = ReplayEngine(self.app.trace_path)
    #           self.frames = list(engine.walk())
    #           if self.frames:
    #               self.current_frame = self.frames[0]
    #
    #       def watch_current_frame(self, frame: StepFrame | None) -> None:
    #           if frame is None:
    #               return
    #           self.query_one("#events").update(render_events(frame.events))
    #           self.query_one("#state").update(render_state(frame.state_snapshot))
    #
    # The DiffFrame dataclass maps naturally to a two-column side-by-side panel:
    # left column = diff.before, right column = diff.after, highlighted by
    # diff.events_diff / diff.state_diff / diff.llm_diff.
"""
from reyn.replay.compare import compare
from reyn.replay.engine import ReplayEngine
from reyn.replay.model import Checkpoint, DiffFrame, StepFrame

__all__ = [
    "ReplayEngine",
    "Checkpoint",
    "StepFrame",
    "DiffFrame",
    "compare",
]
