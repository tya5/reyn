from __future__ import annotations

import contextvars
import logging
from datetime import date
from pathlib import Path
from typing import Callable

from reyn.schemas.models import Event

logger = logging.getLogger(__name__)


# #1669: session-scoped ambient EventLog for the single LLM acompletion chokepoint.
# ``recorded_acompletion`` (reyn.llm.llm) is the one place ALL LLM calls funnel
# through (#1190 AST-guarded), but it receives no events sink (only budget /
# recorder). Threading one through its 9 call sites would be churn AND incomplete
# (judge / compaction / dogfood callers lack a sink). Instead the chat session /
# kernel runtime sets this ContextVar to its EventLog at creation; the chokepoint
# reads it and emits ``llm_request``. ContextVars copy into child asyncio tasks at
# spawn, so a set-before-the-run-loop propagates to every in-session LLM call.
# None (tests / dogfood / CLI, no active session) → the chokepoint skips the emit,
# mirroring the ``recorder=None`` graceful path.
_llm_request_event_log: contextvars.ContextVar["EventLog | None"] = contextvars.ContextVar(
    "reyn_llm_request_event_log", default=None,
)


def set_llm_request_event_log(log: "EventLog | None") -> contextvars.Token:
    """Set the ambient EventLog the LLM chokepoint emits ``llm_request`` to (#1669).

    Returns the token so a caller MAY reset to the prior value for a nested scope;
    the session / runtime set-at-creation sites do not reset (last-set-wins is the
    intended session-scoped lifetime — the active top-level run owns the sink)."""
    return _llm_request_event_log.set(log)


def get_llm_request_event_log() -> "EventLog | None":
    """Read the ambient EventLog for the LLM chokepoint (#1669); None when unset."""
    return _llm_request_event_log.get()


class EventLog:
    def __init__(
        self,
        subscribers: list[Callable[[Event], None]] | None = None,
        *,
        agent_id: str | None = None,
        run_id: str | None = None,
        plan_step: dict | None = None,
    ) -> None:
        self._events: list[Event] = []
        self._subscribers: list[Callable[[Event], None]] = list(subscribers or [])
        # FP-0016 Component E: agent_id is auto-injected into every event
        # payload when set. None preserves prior behaviour for callers
        # (= tests + emit_cli_event) that don't have a session identity.
        self._agent_id = agent_id
        # Issue #134: run_id is auto-injected into every event payload
        # when set, mirroring the agent_id pattern. The skill run that
        # emits the event is recorded so that subscribers (= forwarder /
        # TUI) can distinguish events from a parent skill versus a
        # sub-skill spawned via the ``run_skill`` op (which currently
        # inherits the parent's subscriber list).
        self._run_id = run_id
        # Issue #214 (= #180 #2 split): plan_step is auto-injected when
        # a skill OSRuntime is constructed within the scope of a plan
        # step. Subscribers (= ChatEventForwarder) read ``plan_step`` on
        # the first ``phase_started`` to render "plan N/M" detail on the
        # SkillActivityRow, so the user can correlate a spawned skill
        # row with the originating step. Same caller-wins convention as
        # agent_id / run_id. Shape: {"n_done": int, "n_total": int,
        # "step_id": str}. None = top-level (not inside a plan step).
        self._plan_step = plan_step

    @property
    def subscribers(self) -> list[Callable[[Event], None]]:
        return self._subscribers

    @property
    def agent_id(self) -> str | None:
        """The agent_id this EventLog stamps onto emitted events (FP-0016 E).

        Public read-only view of the constructor-injected agent_id so
        downstream consumers (= kernel executors building OpContext) can
        pick it up without a separate threading parameter.
        """
        return self._agent_id

    @property
    def run_id(self) -> str | None:
        """The run_id this EventLog stamps onto emitted events (issue #134)."""
        return self._run_id

    @property
    def plan_step(self) -> dict | None:
        """The plan_step this EventLog stamps onto emitted events (issue #214)."""
        return self._plan_step

    def add_subscriber(self, fn: Callable[[Event], None]) -> None:
        self._subscribers.append(fn)

    def remove_subscriber(self, fn: Callable[[Event], None]) -> bool:
        """Detach a previously added subscriber.

        Returns True iff the subscriber was found and removed. Used by
        scoped consumers that subscribe for the duration of one call
        (= e.g. issue #271 M1 MCP progress bridge: subscribe in
        ``_call_tool``, unsubscribe in ``finally``) so the subscriber
        list doesn't grow unboundedly across many calls.
        """
        try:
            self._subscribers.remove(fn)
            return True
        except ValueError:
            return False

    def emit(self, type: str, **data) -> Event:
        # FP-0016 Component E: stamp the session's agent_id onto every
        # event payload so the P6 audit trail can answer "which agent
        # did this?" without correlating across multiple logs.  Caller-
        # provided ``agent_id`` wins (= delegation flows may preserve
        # the upstream origin's identity).
        if self._agent_id and "agent_id" not in data:
            data = {**data, "agent_id": self._agent_id}
        # Issue #134: stamp run_id with the same caller-wins convention
        # as agent_id. Lets subscribers route events to the correct
        # skill row when a child skill spawned via ``run_skill`` shares
        # the parent's subscriber list.
        if self._run_id and "run_id" not in data:
            data = {**data, "run_id": self._run_id}
        # Issue #214: stamp plan_step (= {n_done, n_total, step_id}) so
        # ChatEventForwarder can render "plan N/M" detail on the
        # SkillActivityRow of any skill spawned inside a plan step.
        # Caller-wins matches the run_id / agent_id pattern — a skill
        # explicitly emitting plan_step in data is preserved.
        if self._plan_step and "plan_step" not in data:
            data = {**data, "plan_step": self._plan_step}
        event = Event(type=type, data=data)
        self._events.append(event)
        for sub in self._subscribers:
            sub(event)
        return event

    def all(self) -> list[Event]:
        return list(self._events)

    def to_json(self) -> list[dict]:
        return [e.model_dump(mode="json") for e in self._events]


def _find_reyn_dir(start: Path) -> Path | None:
    """Walk up from *start* until finding a directory containing `.reyn/`, or return None."""
    current = start.resolve()
    while True:
        candidate = current / ".reyn"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def emit_cli_event(kind: str, **payload) -> None:
    """Emit a one-off P6 event from a CLI context (no active session).

    Routes to ``.reyn/events/direct/cli/<YYYY-MM-DD>.jsonl``. Locates the
    ``.reyn/`` dir by walking up from ``Path.cwd()``. If no ``.reyn/``
    directory is found, logs a warning and returns silently — the caller's
    operation is the primary action; audit-emit failure must not propagate.

    The file is appended to (P6 append-only contract). Dir creation is
    idempotent (``mkdir(parents=True, exist_ok=True)``).
    """
    from reyn.events.event_store import EventStore

    reyn_dir = _find_reyn_dir(Path.cwd())
    if reyn_dir is None:
        logger.warning(
            "emit_cli_event: no .reyn/ directory found from %s; "
            "skipping P6 audit emit for event %r",
            Path.cwd(),
            kind,
        )
        return

    cli_dir = reyn_dir / "events" / "direct" / "cli"
    today = date.today().isoformat()  # YYYY-MM-DD
    # Use a date-named suffix so each day's CLI events land in one predictable file.
    # max_bytes=0 / max_age_seconds=0 disables rotation — the suffix IS the date.
    store = EventStore(cli_dir, max_bytes=0, max_age_seconds=0, suffix=f"_{today}")
    event_log = EventLog(subscribers=[store])
    event_log.emit(kind, **payload)
