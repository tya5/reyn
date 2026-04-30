"""Extraction journal and trigger logic for ChatSession.

A journal lives at `<chat_workspace>/extraction.json` and tracks how much of
the conversation has been processed by write_memory. Four triggers fire
extraction:

  shutdown : on clean exit, if any new turns since last extract (blocking)
  startup  : on resume, if the journal shows unprocessed turns (background)
  manual   : user typed `/remember` (background)
  periodic : at least N new turns AND at least T seconds since last extract
"""
from __future__ import annotations
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Defaults; ChatSession may override from reyn.yaml
TURN_THRESHOLD = 8
TIME_THRESHOLD = 600.0  # seconds


@dataclass
class ExtractionJournal:
    path: Path
    last_extracted_msg_count: int = 0
    last_extracted_ts: float = 0.0
    in_progress: bool = False

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.last_extracted_msg_count = int(data.get("last_extracted_msg_count", 0))
        self.last_extracted_ts = float(data.get("last_extracted_ts", 0.0))
        self.in_progress = bool(data.get("in_progress", False))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_extracted_msg_count": self.last_extracted_msg_count,
            "last_extracted_ts": self.last_extracted_ts,
            "in_progress": self.in_progress,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def mark_started(self) -> None:
        self.in_progress = True
        self.save()

    def mark_finished(self, msg_count: int, ts: float) -> None:
        self.last_extracted_msg_count = msg_count
        self.last_extracted_ts = ts
        self.in_progress = False
        self.save()

    def mark_aborted(self) -> None:
        self.in_progress = False
        self.save()


def should_extract(
    history_count: int,
    journal: ExtractionJournal,
    *,
    reason: str,
    now: float | None = None,
    turn_threshold: int = TURN_THRESHOLD,
    time_threshold: float = TIME_THRESHOLD,
) -> bool:
    """Return True if extraction should fire for the given trigger.

    `reason` ∈ {"shutdown", "startup", "manual", "periodic"}.

    The extraction is skipped (returns False) whenever the journal indicates
    a previous extraction is still in progress — to avoid concurrent writes.
    """
    if journal.in_progress:
        return False
    new_turns = history_count - journal.last_extracted_msg_count
    if new_turns <= 0:
        # Manual trigger fires anyway (user explicitly asked) so they get feedback
        return reason == "manual"
    if reason in ("shutdown", "startup", "manual"):
        return True
    if reason == "periodic":
        if now is None:
            now = time.time()
        elapsed = now - journal.last_extracted_ts
        return new_turns >= turn_threshold and elapsed >= time_threshold
    return False
