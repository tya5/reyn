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


def validate_in_set(in_set: "dict") -> "str | None":
    """Validate-before-apply (#2073 S2): a structural check of the re-read IN-set.

    Returns a reason string when the IN-set is malformed (→ the HotReloader REJECTS
    the whole reload: no seam runs, the live config is unchanged = rollback), or
    ``None`` when valid. Permissive — an absent component is a no-op — but rejects a
    malformed shape so a half-written ``.reyn/*.yaml`` can never half-apply. The
    component checks grow with the IN-set (hooks in S2b)."""
    if not isinstance(in_set, dict):
        return f"IN-set must be a mapping, got {type(in_set).__name__}"
    cron = in_set.get("cron")
    if cron is not None:
        if not isinstance(cron, dict):
            return "cron section must be a mapping"
        jobs = cron.get("jobs")
        if jobs is not None and not isinstance(jobs, list):
            return "cron.jobs must be a list"
        for j in jobs or []:
            if not isinstance(j, dict) or not j.get("name") or not j.get("schedule"):
                return "each cron job needs a name + schedule"
    mcp = in_set.get("mcp")
    if mcp is not None and not isinstance(mcp, dict):
        return "mcp section must be a mapping"
    hooks = in_set.get("hooks")
    if hooks is not None:
        # #2073 S2b: validate the runtime hooks shape via the real loader so a
        # malformed .reyn/hooks.yaml rejects the whole reload (atomic) rather than
        # raising inside the reapply seam.
        from reyn.hooks import HookConfigError, load_hooks
        try:
            load_hooks(hooks)
        except HookConfigError as exc:
            return f"hooks: {exc}"
    return None


class HotReloader:
    """Schedules + applies an IN-set config reload at the turn boundary (#2073)."""

    def __init__(
        self,
        *,
        project_root: "Path | None",
        events: "object | None",
        seams: "list[ReapplySeam] | None" = None,
        validate: "Callable[[dict], str | None] | None" = None,
    ) -> None:
        self._project_root = project_root
        self._events = events
        self._seams: list[ReapplySeam] = list(seams or [])
        # #2073 S2: validate-before-apply. ``validate(in_set) -> reason | None`` — a
        # non-None reason REJECTS the whole reload (no seam runs, live config
        # unchanged = rollback), so a malformed IN-set can never half-apply. Defaults
        # to the built-in structural :func:`validate_in_set`.
        self._validate: "Callable[[dict], str | None]" = validate or validate_in_set
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

    def set_validate(self, fn: "Callable[[dict], str | None]") -> None:
        """Set the validate-before-apply hook (#2073 S2): ``fn(in_set) -> reason |
        None``; a non-None reason rejects the reload atomically (no seam runs)."""
        self._validate = fn

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

        # Validate-before-apply (atomicity): a malformed IN-set is REJECTED whole —
        # no seam runs, the live config is unchanged (rollback). config_reloaded is
        # NOT emitted (no state change occurred), only a warning.
        if self._validate is not None:
            reason = self._validate(in_set)
            if reason:
                _log.warning(
                    "hot-reload REJECTED (invalid IN-set): %s — live config unchanged",
                    reason,
                )
                return {"source": source, "rejected": reason, "applied": [], "failed": []}

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


# ── process-wide active HotReloader (#2073 S3) ──────────────────────────────
# The LLM-op hooks-write tool reaches the reloader to ``request_reload`` after
# writing .reyn/hooks.yaml. Mirrors ``set_active_scheduler`` / ``get_active_scheduler``
# (cron). NOTE (multi-session caveat, same as cron's single-scheduler): this is the
# last-registered session's reloader; a per-session route (via ToolContext) is a
# noted beauty-follow-up, out of S3 scope.
_active_hot_reloader: "HotReloader | None" = None


def set_active_hot_reloader(reloader: "HotReloader | None") -> None:
    """Register / unregister the process-wide active HotReloader (#2073 S3)."""
    global _active_hot_reloader
    _active_hot_reloader = reloader


def get_active_hot_reloader() -> "HotReloader | None":
    """Return the active HotReloader, or None when unset."""
    return _active_hot_reloader


__all__ = [
    "HotReloader",
    "ReapplySeam",
    "validate_in_set",
    "set_active_hot_reloader",
    "get_active_hot_reloader",
]
