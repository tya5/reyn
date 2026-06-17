"""src/reyn/replay/model.py — dataclasses for the replay engine.

These dataclasses are the structured unit of exchange between the replay
engine and its consumers (CLI, future TUI, tests).  They are render-agnostic:
a CLI presenter formats them as text; a Textual widget would bind them to
reactive state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Checkpoint:
    """Addressing for a specific step inside a recorded session.

    Shell-friendly serialisation: ``<run_id>:<phase>:<step_idx>``
    (e.g. ``run_xyz:copy_to_work:3``).
    """

    run_id: str
    phase: str
    step_idx: int

    # ── serialisation ────────────────────────────────────────────────────────

    def __str__(self) -> str:
        return f"{self.run_id}:{self.phase}:{self.step_idx}"

    @classmethod
    def parse(cls, s: str) -> "Checkpoint":
        """Parse ``run_id:phase:step_idx`` string.

        Raises ``ValueError`` if the format is wrong.
        """
        parts = s.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"Checkpoint must be '<run_id>:<phase>:<step_idx>', got: {s!r}"
            )
        run_id, phase, step_idx_s = parts
        if not run_id or not phase:
            raise ValueError(f"run_id and phase must be non-empty in: {s!r}")
        try:
            step_idx = int(step_idx_s)
        except ValueError:
            raise ValueError(
                f"step_idx must be an integer in checkpoint {s!r}, "
                f"got: {step_idx_s!r}"
            )
        return cls(run_id=run_id, phase=phase, step_idx=step_idx)


@dataclass
class StepFrame:
    """All data recorded for a single step within a phase.

    ``checkpoint``     — address of this step (run_id / phase / step_idx)
    ``events``         — list of WAL event dicts whose scope covers this step
                         (typically step_started + step_completed/step_failed
                          plus any intervening audit events)
    ``state_snapshot`` — synthesised key/value snapshot at end of this step
                         (populated from WAL events; partial if WAL is sparse)
    ``llm_payload``    — LLM request dict from the LLM trace dump, or None if
                         no LLM call occurred in this step
    ``llm_result``     — LLM response dict from the LLM trace dump, or None
    """

    checkpoint: Checkpoint
    events: list[dict] = field(default_factory=list)
    state_snapshot: dict[str, Any] = field(default_factory=dict)
    llm_payload: dict | None = None
    llm_result: dict | None = None


@dataclass
class DiffFrame:
    """Diff result for a single step (or phase / skill_run when aggregated).

    ``before`` / ``after`` — StepFrames from two trace sources.  Either may be
    None when the step exists in one trace but not the other.

    ``events_diff``   — structured diff of the events lists
    ``state_diff``    — structured diff of state_snapshot dicts
    ``llm_diff``      — structured diff of LLM payloads and results
    """

    before: StepFrame | None
    after: StepFrame | None
    events_diff: dict[str, Any] = field(default_factory=dict)
    state_diff: dict[str, Any] = field(default_factory=dict)
    llm_diff: dict[str, Any] = field(default_factory=dict)

    @property
    def has_diff(self) -> bool:
        """Return True if any of the three diff dicts is non-empty."""
        return bool(self.events_diff or self.state_diff or self.llm_diff)
