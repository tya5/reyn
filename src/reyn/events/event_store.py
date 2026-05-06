"""EventStore — file-backed audit log with rotation.

Used by both chat sessions (long-lived, rotated by size+age+date) and
skill runs (1 run = 1 file, no rotation). Same API for both — the
difference is the rotation policy passed at construction.

Files live under `<dir>/<YYYY-MM>/<YYYY-MM-DDTHHMMSS>[<suffix>].jsonl`.
filename start-time prefix means lexical sort = chronological order.

Rotation creates a NEW file (no rename). The previous file is left in
place and remains readable. This sidesteps mid-rotation crash hazards
that rename-based schemes have.

Per P7: this is OS-level generic infrastructure — it never references
specific event types or skill / chat domain strings.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from reyn.schemas.models import Event


class EventStore:
    def __init__(
        self,
        dir_path: Path,
        *,
        max_bytes: int = 0,
        max_age_seconds: int = 0,
        suffix: str = "",
    ) -> None:
        """
        dir_path: e.g. `events/agents/researcher/chat`
                  or  `events/agents/researcher/skill_runs`
        max_bytes:       0 disables size-based rotation (skill_run mode)
        max_age_seconds: 0 disables age-based rotation
                         (date-boundary rotation also gated on this)
        suffix:          "" for chat, e.g. "_skill_router" for a run
        """
        self._dir = Path(dir_path)
        self._max_bytes = int(max_bytes)
        self._max_age_seconds = int(max_age_seconds)
        self._suffix = suffix
        self._active: Path | None = None
        self._active_started_at: datetime | None = None

    # ── public API ──────────────────────────────────────────────────────

    def __call__(self, event: Event) -> None:
        """Subscriber-callable form so EventStore can be plugged into EventLog."""
        self.write(event)

    def write(self, event: Event) -> None:
        if self._active is None or self._should_rotate():
            self._open_new_file(now=datetime.now())
        line = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
        with self._active.open("a", encoding="utf-8") as f:  # type: ignore[union-attr]
            f.write(line + "\n")

    def iter_all(self) -> Iterator[Event]:
        """Yield every event in this store in chronological order.

        Walks `<dir>/<YYYY-MM>/*.jsonl` in lexical order — since filenames
        are start-time prefixed, lexical order is chronological. Bad lines
        are skipped silently (mid-write crash leaves the last line partial).
        """
        for path in self.iter_files():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        yield Event.model_validate(raw)
                    except Exception:
                        continue

    def iter_files(self) -> list[Path]:
        """Return all .jsonl files in this store, chronological order."""
        if not self._dir.is_dir():
            return []
        out: list[Path] = []
        for month_dir in sorted(self._dir.iterdir()):
            if not month_dir.is_dir():
                continue
            for f in sorted(month_dir.glob("*.jsonl")):
                out.append(f)
        return out

    @property
    def active_path(self) -> Path | None:
        return self._active

    def open(self) -> Path:
        """Eagerly create the active file and return its path.

        Useful for callers that print the destination before any event is
        actually written (e.g. `reyn run` shows `events saved → ...`).
        """
        if self._active is None:
            self._open_new_file(now=datetime.now())
        return self._active  # type: ignore[return-value]

    # ── internals ───────────────────────────────────────────────────────

    def _should_rotate(self) -> bool:
        if self._active is None or self._active_started_at is None:
            return False
        if self._max_bytes <= 0 and self._max_age_seconds <= 0:
            return False
        if self._max_bytes > 0:
            try:
                if self._active.stat().st_size >= self._max_bytes:
                    return True
            except OSError:
                pass
        now = datetime.now()
        if self._max_age_seconds > 0:
            elapsed = (now - self._active_started_at).total_seconds()
            if elapsed >= self._max_age_seconds:
                return True
            # Date boundary: rotation also fires when the local date rolls
            # over, so a "daily" file naturally aligns with calendar days.
            if now.date() != self._active_started_at.date():
                return True
        return False

    def _open_new_file(self, now: datetime) -> None:
        month_dir = self._dir / now.strftime("%Y-%m")
        month_dir.mkdir(parents=True, exist_ok=True)
        ts = now.strftime("%Y-%m-%dT%H%M%S")
        candidate = month_dir / f"{ts}{self._suffix}.jsonl"
        self._active = self._unique(candidate)
        self._active.touch()
        self._active_started_at = now

    @staticmethod
    def _unique(path: Path) -> Path:
        """If `path` already exists, append `_1`, `_2`, ... before `.jsonl`.

        We use `_N` (not `-N`) so collisions sort AFTER the base file
        lexically: `.` (0x2E) < `_` (0x5F). With `-N` (0x2D) the collision
        files would sort BEFORE the base, breaking chronological iter_all.
        """
        if not path.exists():
            return path
        stem = path.stem  # "<ts><suffix>"
        for n in range(1, 10000):
            candidate = path.with_name(f"{stem}_{n}.jsonl")
            if not candidate.exists():
                return candidate
        # Implausible — bail out with the original to avoid infinite loop
        return path
