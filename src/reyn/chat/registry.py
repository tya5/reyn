"""AgentRegistry — owner of all ChatSession instances in a `reyn chat` process.

PR10 introduces multiple agents (= multiple ChatSession instances) sharing
one process. The registry handles persistence (`.reyn/agents/<name>/`),
lifecycle (lazy load, background `session.run()` task, attach/detach), and
attached-agent routing for the REPL.

Lifecycle invariants (PR10):
- A `default` agent always exists; created on registry init if absent.
- Agents are loaded lazily — `start_attached()` is the first time we
  spin up `session.run()` for the named agent.
- After `attach(B)`, agent A's `session.run()` keeps running in the
  background (skills can keep progressing); only the REPL's display
  pointer moves to B.

The registry deliberately knows nothing about prompt_toolkit, renderers,
or the inbox/outbox queue mechanics — those live in `repl.py`. Registry's
contract is:
- `attached` returns the currently-attached ChatSession (or None)
- `attach(name)` makes that session the attached one and returns it
- `running_tasks_for_agents()` lets the REPL `await` shutdown drain
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .profile import AgentProfile, PROFILE_FILENAME


DEFAULT_AGENT_NAME = "default"

# Lowercase ASCII + digit + underscore + hyphen, 1-32 chars. Mirrors usual
# directory-name-safety rules and keeps the on-disk layout uncluttered.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _validate_agent_name(name: str) -> None:
    if not _AGENT_NAME_RE.match(name):
        raise ValueError(
            f"invalid agent name {name!r}: must be 1-32 chars of "
            "[a-z0-9_-] starting with [a-z0-9]"
        )


class AgentRegistry:
    """In-process map of agent_name -> ChatSession with persistence wired in.

    Owns the **REPL-facing outbox**: a single queue that consumers (e.g.
    `repl._output_loop`) read regardless of which agent is attached. A
    per-agent forwarder task pumps the agent's own `outbox` into this queue
    only while that agent is the attached one — detached agents drop
    transient outbox items, durable kinds (agent / skill_done) still
    persist to history via the agent's `_append_history` (handled at the
    ChatSession layer, not here).
    """

    def __init__(
        self,
        project_root: Path,
        *,
        session_factory: Callable[[AgentProfile], "object"],
    ) -> None:
        """
        session_factory: returns a configured ChatSession given an AgentProfile.
            The factory captures CLI-derived defaults (model, resolver, permissions,
            limits, mcp config, …) — registry doesn't need to know them.
        """
        self._dir = project_root / ".reyn" / "agents"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._factory = session_factory
        self._agents: dict[str, "object"] = {}            # name -> ChatSession
        self._tasks: dict[str, asyncio.Task] = {}         # name -> session.run() task
        self._forward_tasks: dict[str, asyncio.Task] = {} # name -> outbox forwarder
        self._attached: str | None = None
        # Single queue the REPL drains; registry routes each attached agent's
        # outbox into here.
        self.repl_outbox: asyncio.Queue = asyncio.Queue()
        # Ensure default exists so `reyn chat` (no name) works out of the box.
        if not (self._dir / DEFAULT_AGENT_NAME / PROFILE_FILENAME).is_file():
            AgentProfile.new(DEFAULT_AGENT_NAME, role="").save(
                self._dir / DEFAULT_AGENT_NAME
            )

    # ── persistence ──────────────────────────────────────────────────────────

    def list_names(self) -> list[str]:
        """All agent names found on disk (sorted)."""
        out = []
        for entry in self._dir.iterdir():
            if entry.is_dir() and (entry / PROFILE_FILENAME).is_file():
                out.append(entry.name)
        return sorted(out)

    def load_profile(self, name: str) -> AgentProfile:
        return AgentProfile.load(self._dir / name)

    def exists(self, name: str) -> bool:
        return (self._dir / name / PROFILE_FILENAME).is_file()

    def create(self, name: str, *, role: str = "") -> AgentProfile:
        _validate_agent_name(name)
        if self.exists(name):
            raise FileExistsError(f"agent {name!r} already exists")
        profile = AgentProfile.new(name, role=role)
        profile.save(self._dir / name)
        return profile

    def remove(self, name: str) -> None:
        if name == DEFAULT_AGENT_NAME:
            raise ValueError("cannot remove the default agent")
        if name == self._attached:
            raise ValueError(f"cannot remove attached agent {name!r}")
        target = self._dir / name
        if not target.is_dir():
            raise FileNotFoundError(target)
        # Cancel any cached tasks / drop session before deleting on-disk state.
        for task_dict in (self._tasks, self._forward_tasks):
            task = task_dict.pop(name, None)
            if task and not task.done():
                task.cancel()
        self._agents.pop(name, None)
        # Recursive rm — agents/<name>/ is reyn-managed, no surprises expected.
        import shutil
        shutil.rmtree(target)

    def last_activity_at(self, name: str) -> datetime | None:
        """Last mtime among history.jsonl / events.jsonl, or None if absent."""
        agent_dir = self._dir / name
        candidates: list[float] = []
        for fname in ("history.jsonl", "events.jsonl"):
            p = agent_dir / fname
            if p.is_file():
                candidates.append(p.stat().st_mtime)
        if not candidates:
            return None
        return datetime.fromtimestamp(max(candidates), tz=timezone.utc)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def get_or_load(self, name: str) -> "object":
        """Return the ChatSession for `name`, instantiating from profile if new."""
        if name in self._agents:
            return self._agents[name]
        if not self.exists(name):
            raise FileNotFoundError(
                f"agent {name!r} not found; run `reyn agent new {name}` to create it"
            )
        profile = self.load_profile(name)
        session = self._factory(profile)
        self._agents[name] = session
        return session

    async def ensure_running(self, name: str) -> "object":
        """Load + start session.run() + forwarder for `name` without
        changing the user-attached pointer. Used for agent-to-agent
        messaging (PR11): when A sends to B, B's task must be live to
        consume the inbox put, but the user's display stays on whoever
        they were attached to.

        The forwarder is still started so that, should the user later
        attach to B, B's pre-existing outbox messages route correctly.
        """
        session = self.get_or_load(name)
        if name not in self._tasks or self._tasks[name].done():
            self._tasks[name] = asyncio.create_task(session.run())
        if name not in self._forward_tasks or self._forward_tasks[name].done():
            self._forward_tasks[name] = asyncio.create_task(self._forwarder(name))
        return session

    async def attach(self, name: str) -> "object":
        """Switch the attached agent to `name`. Loads + starts session.run()
        and the outbox forwarder for the new agent if not already running.
        Old agent stays in `self._tasks` (background)."""
        new_session = self.get_or_load(name)
        old_name = self._attached
        if old_name and old_name != name:
            old_session = self._agents.get(old_name)
            if old_session is not None:
                # Mark detached BEFORE switching so transient outbox emissions
                # from the old session start dropping at the source
                # (`ChatSession._put_outbox` filters status/trace).
                old_session.is_attached = False

        new_session.is_attached = True
        # Boot session.run() + forwarder on first attach. Keep them alive
        # across detach/re-attach cycles — shutdown drains via `running_tasks()`.
        if name not in self._tasks or self._tasks[name].done():
            self._tasks[name] = asyncio.create_task(new_session.run())
        if name not in self._forward_tasks or self._forward_tasks[name].done():
            self._forward_tasks[name] = asyncio.create_task(
                self._forwarder(name)
            )
        self._attached = name

        # Re-announce any pending interventions for the user. While detached,
        # `_announce_intervention` already put the original message on the
        # session outbox, but the forwarder dropped it (detached). On attach
        # we replay each pending iv so the user sees what's waiting.
        for iv in list(new_session._active_interventions.values()):
            if not iv.future.done():
                await new_session._announce_intervention(iv)
        return new_session

    async def _forwarder(self, name: str) -> None:
        """Pump one agent's outbox into the registry-level repl_outbox.

        Runs continuously per agent. Only forwards when this agent is the
        attached one; otherwise drops the message (transient kinds were
        already dropped at source, durable narration is in history). Special
        kind `__attach_request__` is consumed here as a control signal.
        """
        agent = self._agents[name]
        while True:
            msg = await agent.outbox.get()
            if msg.kind == "__end__":
                # Session shut down — propagate to REPL only if we're the
                # attached one (otherwise REPL would terminate spuriously
                # on a detached agent's shutdown).
                if name == self._attached:
                    await self.repl_outbox.put(msg)
                return
            if msg.kind == "__attach_request__":
                # User typed `:attach <other>` while this agent was attached.
                if msg.text and self.exists(msg.text):
                    await self.attach(msg.text)
                continue
            if name == self._attached:
                await self.repl_outbox.put(msg)
            # else: drop — agent is detached, transient kinds were already
            # dropped at source, durable narration is in history.jsonl

    def detach(self) -> None:
        """Mark the attached agent as detached without stopping its task."""
        if self._attached is None:
            return
        session = self._agents.get(self._attached)
        if session is not None:
            session.is_attached = False
        self._attached = None

    @property
    def attached_name(self) -> str | None:
        return self._attached

    def attached_session(self) -> "object | None":
        if self._attached is None:
            return None
        return self._agents.get(self._attached)

    def running_tasks(self) -> list[asyncio.Task]:
        """All non-completed tasks (session.run + forwarders) for shutdown drain."""
        out: list[asyncio.Task] = []
        for table in (self._tasks, self._forward_tasks):
            out.extend(t for t in table.values() if not t.done())
        return out

    async def shutdown(self) -> None:
        """Best-effort: stop all loaded sessions, then await tasks."""
        for name, agent in list(self._agents.items()):
            try:
                await agent.shutdown()
            except Exception:
                pass
        # Cancel forwarders so they don't block on a queue that won't refill
        for t in self._forward_tasks.values():
            if not t.done():
                t.cancel()
        if self.running_tasks():
            await asyncio.gather(*self.running_tasks(), return_exceptions=True)

    def loaded_names(self) -> list[str]:
        return list(self._agents.keys())

    def iter_other_agents(self, self_name: str) -> list[dict]:
        """List `{name, role}` for every agent except `self_name`.

        Used by ChatSession._invoke_router to populate `available_agents`
        in chat_routing_request. `role` is the first non-empty line of
        each agent's profile.role; empty when the agent has no role.
        """
        out: list[dict] = []
        for name in self.list_names():
            if name == self_name:
                continue
            try:
                profile = self.load_profile(name)
            except Exception:
                continue
            role_lines = (profile.role or "").strip().splitlines()
            role_excerpt = role_lines[0].strip() if role_lines else ""
            out.append({"name": name, "role": role_excerpt})
        return out


def _drain_queue(q: asyncio.Queue) -> None:
    """Best-effort drop of all currently-queued items. Non-blocking."""
    try:
        while True:
            q.get_nowait()
    except asyncio.QueueEmpty:
        pass


__all__ = ["AgentRegistry", "DEFAULT_AGENT_NAME"]
