"""HotReloader — IN-set config hot-reload at the turn boundary (#2073).

The OUT-set (``reyn.yaml``: security / permission / sandbox / budget / the loop
valve / state-coupled runtime) is loaded ONCE at startup and **never reloaded** —
the HotReloader reads ONLY the IN-set (the runtime-mutable ``.reyn/*.yaml``
registries, via :func:`reyn.config.loader.load_hot_reload_config`). The file-split
IS the write-gate boundary (owner-confirmed): a reload — and the LLM-op that
triggers one — can never touch the OUT-set, because the loader never opens it.

Timing-B (owner-confirmed): a trigger **schedules** a reload
(:meth:`request_reload`); it **applies** at the turn boundary (finish-reason=stop —
the #1800 ``turn_end`` safe-point). 1 turn = 1 config snapshot, never mid-turn; the
next turn runs under the new config.

On apply: re-read the IN-set → reapply each registered component seam → emit the
``config_reloaded`` P6 event → clear pending. **#2073 S1** establishes the
orchestration + the safety boundary + the event + the operator trigger; the
per-component reapply **seams are wired in S2** (this stage runs with none, so a
reload is a re-read + event only).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from reyn.config.loader import load_hot_reload_config

if TYPE_CHECKING:
    from pathlib import Path

_log = logging.getLogger(__name__)

# A component reapply seam: ``(name, async fn(in_set) -> changed: bool)``. #2073 S2
# wires the per-component seams (mcp / cron / hooks / per-agent capability / …).
ReapplySeam = tuple[str, Callable[[dict], Awaitable[bool]]]


class HotReloader:
    """Schedules + applies an IN-set config reload at the turn boundary (#2073)."""

    def __init__(
        self,
        *,
        project_root: "Path | None",
        events: "object | None",
        seams: "list[ReapplySeam] | None" = None,
    ) -> None:
        self._project_root = project_root
        self._events = events
        self._seams: list[ReapplySeam] = list(seams or [])
        self._pending = False
        self._pending_source: "str | None" = None

    @property
    def pending(self) -> bool:
        """True iff a reload is scheduled and not yet applied."""
        return self._pending

    def register_seam(self, name: str, fn: "Callable[[dict], Awaitable[bool]]") -> None:
        """Register a per-component reapply seam (#2073 S2). ``fn(in_set)`` returns
        whether it changed anything; it must not raise (the applier isolates it)."""
        self._seams.append((name, fn))

    def request_reload(self, *, source: str) -> None:
        """Schedule a reload at the next turn boundary (operator command / LLM-op).

        Idempotent within a turn: repeated requests collapse into one apply (1 turn
        = 1 config snapshot). ``source`` is recorded on the ``config_reloaded`` event
        for the audit trail (e.g. ``"operator"`` / ``"llm_op"``)."""
        self._pending = True
        self._pending_source = source
        _log.info(
            "hot-reload scheduled (source=%s) — applies at the next turn boundary", source,
        )

    async def apply_pending(self) -> "dict | None":
        """At the turn boundary: if a reload is pending, re-read the IN-set, reapply
        each component seam, emit ``config_reloaded`` (P6), and clear pending.

        Returns a summary ``{"source", "applied", "failed"}`` when a reload was
        applied, or ``None`` when nothing was pending (the zero-overhead no-op — a
        session that never reloads is byte-identical to a build without this).

        Never raises out: a seam failure is logged + recorded under ``failed`` and
        the turn loop proceeds (a misbehaving reload can never break the run-loop).
        """
        if not self._pending:
            return None
        source = self._pending_source
        self._pending = False
        self._pending_source = None

        # Safety boundary: re-read ONLY the IN-set (.reyn/*.yaml). The OUT-set
        # (reyn.yaml) is never opened here, so a reload cannot touch it.
        in_set = load_hot_reload_config(self._project_root)

        applied: list[str] = []
        failed: list[str] = []
        for name, fn in self._seams:
            try:
                if await fn(in_set):
                    applied.append(name)
            except Exception as exc:  # noqa: BLE001 — a seam never breaks the loop
                _log.warning("hot-reload seam %r failed: %s", name, exc)
                failed.append(name)

        if self._events is not None:
            # P6 audit (review-focus b): every config change is an evented,
            # replay-capable state change.
            self._events.emit(
                "config_reloaded",
                source=source or "unknown",
                components=applied,
                failed=failed,
            )
        return {"source": source, "applied": applied, "failed": failed}


__all__ = ["HotReloader", "ReapplySeam"]
