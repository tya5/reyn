"""Client-side chat read-model — the ADR-0039 P3 seam that makes the inline CUI
transport-agnostic (local ≡ remote by construction, at the RENDERER layer).

The inline input driver (:func:`reyn.interfaces.inline.app.run_inline_input`)
renders a live status bar, an intervention region, and a task tree. P1/P2 already
unified the client's WRITE side behind
:class:`~reyn.interfaces.transport.client_transport.ClientTransport` and its READ
of the conversation/working-indicator stream behind the frame stream — but the
inline driver's *status-panel* reads still went straight to the local
``AgentRegistry`` / ``Session`` by duck-typing (fine in-process, impossible for a
remote client that holds no session). That was the last local-only coupling, and
the inline app's own docstring named it: *"a full client-side read-model is P3."*

This module is that read-model: one seam the inline driver reads ALL of its
status/region/task state from, with two implementations —

- :class:`RegistryReadModel` — LOCAL, byte-identical to the pre-P3 behavior:
  every accessor delegates to the exact ``_snapshot(registry, …)`` /
  ``registry.attached_session()`` calls the driver made inline before.
- :class:`RemoteReadModel` — REMOTE, backed by the P2
  :class:`~reyn.interfaces.transport.agui.state.RemoteStatusView` the
  ``AgUiTransport`` already populates from the server's ``STATE_*`` stream. It
  projects the wire status subset into the ``_snapshot`` dict shape the chips
  read (:func:`project_remote_snapshot`).

**Frame-sufficiency (what a remote client CAN show).** The server projects only
the MAIN-bar subset onto the wire (``state.py``'s ``project_status`` /
``_WIRE_KEYS``): ``model`` · ``attached_name`` · ``cost_agent`` / ``cost_total``
/ ``agent_tokens`` · ``ctx_used`` / ``ctx_window`` · ``task_count`` ·
``waiting_on``. Those chip VALUES render on remote. The dropdown EXPANSIONS
(cost/ctx detail tuples, the ``/model`` class picker, the agent/session tree, the
task tree, the ``…`` sub-bar toggle counts) and the interactive intervention /
rewind PICKERS are session-local and are NOT on the wire — the remote model
returns empty/``—`` for them (graceful degrade), never a fake value. Extending
any of those is an additive ``project_status`` key (a follow-up vocabulary
change), not a client rewrite.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reyn.interfaces.transport.client_transport import ClientTransport


class ChatReadModel(ABC):
    """The inline CUI's sole READ seam: status snapshot + region + tasks + history.

    Writes still ride the :class:`ClientTransport`; this is the read half the
    inline driver used to take off the registry directly. Abstract (not a bare
    Protocol) so a partial implementation fails at construction, not first use —
    the same completeness-by-construction discipline ``ClientTransport`` uses.
    """

    @abstractmethod
    def snapshot(self, task_cache: "dict | None" = None, config=None) -> "dict | None":
        """Return the status-bar snapshot dict (the ``_snapshot`` shape), or None
        when there is nothing to show (no attached session locally)."""

    @abstractmethod
    def intervention_head(self) -> "object | None":
        """The head closed-set intervention (with ``.id`` / ``.choices``) for the
        above-input region selector, or None. Remote returns None: a remote
        intervention rides the display prompt and is answered on the input line
        (via the transport), not through the local region picker."""

    @abstractmethod
    def pending_command_ui(self) -> "dict | None":
        """The pending command-UI request (the ``/rewind`` picker), or None.
        Command-UI is inline-app-local state, not on the wire → None for remote."""

    @abstractmethod
    def clear_pending_command_ui(self) -> None:
        """Consume the pending command-UI request (no-op when unsupported)."""

    @abstractmethod
    async def list_active_tasks(self) -> "list[dict]":
        """Active-task dicts (``Task.to_dict()``) for the local task-tree poll.
        Remote returns [] — the remote task chip shows the wire ``task_count``,
        with no tree."""

    @property
    @abstractmethod
    def has_command_ui_region(self) -> bool:
        """Whether this client hosts the interactive command-UI region. False for
        remote → the ``__rewind_list__`` frame renders as a text list instead of
        being swallowed for a picker that will never appear."""

    @property
    @abstractmethod
    def history_path(self) -> Path:
        """Filesystem path for the input-history file."""


class RegistryReadModel(ChatReadModel):
    """LOCAL read-model — delegates to the same registry/session accessors the
    inline driver called inline before P3 (behavior byte-identical)."""

    def __init__(self, registry) -> None:
        self._registry = registry

    def snapshot(self, task_cache=None, config=None):
        from reyn.interfaces.inline.app import _snapshot  # noqa: PLC0415 — avoid cycle
        return _snapshot(self._registry, task_cache, config)

    def _attached(self):
        return self._registry.attached_session()

    def intervention_head(self):
        s = self._attached()
        return s.interventions.head() if s is not None else None

    def pending_command_ui(self):
        s = self._attached()
        return s.pending_command_ui if s is not None else None

    def clear_pending_command_ui(self) -> None:
        s = self._attached()
        if s is not None:
            s.set_pending_command_ui(None)

    async def list_active_tasks(self):
        tasks = await self._registry.task_backend.list()
        dicts = [t.to_dict() for t in tasks]
        return [d for d in dicts if d["status"] not in ("done", "failed", "aborted")]

    @property
    def has_command_ui_region(self) -> bool:
        return True

    @property
    def history_path(self) -> Path:
        s = self._attached()
        if s is None:
            raise RuntimeError(
                "RegistryReadModel.history_path: no attached session "
                "(call registry.attach() before run_inline_input)"
            )
        return s.workspace_dir / ".input_history"


def project_remote_snapshot(values: "dict | None") -> dict:
    """Project a :class:`RemoteStatusView`'s wire values into the ``_snapshot``
    dict shape the inline chips read.

    The MAIN-bar keys are filled from the wire; every EXPANSION-only key gets a
    graceful empty/zero default so opening a dropdown on a remote client shows an
    empty panel rather than raising or fabricating a value. ``model`` falls back
    to ``—`` so a pre-``STATE_SNAPSHOT`` frame renders a placeholder, not None.
    """
    v = values or {}
    return {
        # -- MAIN bar (frame-available via STATE_*) --
        "model": v.get("model") or "—",
        "attached_name": v.get("attached_name"),
        "cost_agent": v.get("cost_agent", 0.0),
        "cost_total": v.get("cost_total", 0.0),
        "cost_usd": v.get("cost_agent", 0.0),
        "agent_tokens": v.get("agent_tokens", 0),
        "ctx_used": v.get("ctx_used", 0),
        "ctx_window": v.get("ctx_window", 0),
        "task_count": v.get("task_count", 0),
        # -- expansion-only keys (NOT on the wire) → graceful empty --
        "model_active_class": None,
        "model_classes": [],
        "agent_names": [],
        "session_tree": [],
        "usage": (0, 0, v.get("agent_tokens", 0)),
        "session_cached_tokens": 0,
        "ctx_recent_usage": (0, 0),
        "ctx_source": "remote",
        "ctx_compaction_status_fn": None,
        "task_tree": [],
        "cron_jobs": [],
        "mcp_servers": [],
        "hooks": [],
        "skills": [],
        "visibility_items": [],
        "hook_items": [],
        "pipelines": [],
    }


class RemoteReadModel(ChatReadModel):
    """REMOTE read-model — projects the transport's P2 ``RemoteStatusView`` (fed by
    the server's ``STATE_SNAPSHOT`` / ``STATE_DELTA``) into the status-bar shape.

    Region/task/command-UI are inline-app-local and not on the wire → empty. The
    input-history file lands under ``~/.reyn`` (there is no per-agent workspace on
    the client side of a remote attach)."""

    def __init__(self, transport: "ClientTransport") -> None:
        self._transport = transport

    def snapshot(self, task_cache=None, config=None):
        # ``transport.status`` is the RemoteStatusView the AgUiTransport updates as
        # it decodes STATE_* frames; read it live each render tick.
        values = getattr(getattr(self._transport, "status", None), "values", None)
        return project_remote_snapshot(values)

    def intervention_head(self):
        return None

    def pending_command_ui(self):
        return None

    def clear_pending_command_ui(self) -> None:
        return None

    async def list_active_tasks(self):
        return []

    @property
    def has_command_ui_region(self) -> bool:
        return False

    @property
    def history_path(self) -> Path:
        base = Path.home() / ".reyn"
        base.mkdir(parents=True, exist_ok=True)
        return base / "remote-input-history"


__all__ = [
    "ChatReadModel",
    "RegistryReadModel",
    "RemoteReadModel",
    "project_remote_snapshot",
]
