"""
EventPersister — event subscriber that appends every event to a JSONL file.

One run → one file.  Written incrementally so partial runs are recoverable.
File format: newline-delimited JSON, one event per line.
"""
from __future__ import annotations
import json
from pathlib import Path
from .models import Event


class EventPersister:
    """
    Callable subscriber that persists events to a JSONL file.

    Parameters
    ----------
    path : Path
        Destination file.  Parent directories are created on construction.
        The file is opened in append mode so existing content is preserved
        (relevant if the same path is somehow reused).
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
