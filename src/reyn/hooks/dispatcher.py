"""reyn.hooks.dispatcher — the awaited HookDispatcher (#1800 slice 5b).

The integration core of the agent-lifecycle-hook system. Unlike a P6 EventLog
subscriber (sync-inline, cannot ``await``), the dispatcher is a **first-class
``await``ed dispatch** invoked at the session/turn lifecycle points: it can
``await`` the inbox push (E), the next-turn staging (C), and the shell run (F).

Per-hook isolation: a hook that raises is logged and skipped; its siblings and
the lifecycle point itself proceed. ``dispatch()`` never propagates an exception
out — a misbehaving hook can never break the run-loop.

No-hooks equivalence (the critical property): an empty registry makes
``dispatch()`` a no-op (the ``hooks_for`` loop body never runs), so the run-loop
is byte-identical to a hooks-free build.

The three Session seams the dispatcher needs are injected as bound callables
(DI), so the dispatcher is decoupled from ``Session`` and unit-testable against a
real Session's methods (no mocks):

- ``put_inbox(kind, payload)``           — E (wake=true): a turn trigger.
- ``stage_next_turn_context(kind, payload)`` — C (wake=false): a passive ride-along.
- ``run_shell(command, event_context, **sandbox)`` — F: an external side-effect.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import render_push
from reyn.hooks.schema import HookDef
from reyn.hooks.shell_runner import run_shell_hook

_log = logging.getLogger(__name__)

# The inbox kind for an E (wake=true) hook trigger. Routed by the run-loop to
# ``Session._handle_hook_message`` (system-role ``[hook:name]`` + one turn).
HOOK_INBOX_KIND = "hook"

# The kind stored on a staged C (wake=false) ride-along entry; the staged-context
# consumer reads ``payload["name"]`` for the ``[hook:name]`` attribution.
HOOK_STAGE_KIND = "hook"

PutInbox = Callable[[str, dict], Awaitable[Any]]
StageContext = Callable[[str, dict], Awaitable[Any]]
RunShell = Callable[..., Awaitable[Any]]


class HookDispatcher:
    """Awaited dispatch of lifecycle hooks (#1800 slice 5b)."""

    def __init__(
        self,
        registry: HookRegistry,
        *,
        put_inbox: PutInbox,
        stage_next_turn_context: StageContext,
        run_shell: RunShell = run_shell_hook,
        sandbox_config: Any = None,
        sandbox_backend: Any = None,
    ) -> None:
        self._registry = registry
        self._put_inbox = put_inbox
        self._stage_next_turn_context = stage_next_turn_context
        self._run_shell = run_shell
        self._sandbox_config = sandbox_config
        self._sandbox_backend = sandbox_backend

    async def dispatch(self, point: str, template_vars: dict) -> None:
        """Run every hook registered for ``point`` (registration order).

        Per-hook ``try/except``: a raising hook is logged + skipped; siblings and
        the lifecycle point proceed. Never propagates out of ``dispatch()``.
        Empty registry → the loop body never runs → byte-identical no-op.
        """
        for hook in self._registry.hooks_for(point):
            try:
                await self._dispatch_one(hook, point, template_vars)
            except Exception as exc:  # noqa: BLE001 — per-hook isolation boundary
                _log.warning(
                    "Hook at point %r raised — skipped (siblings proceed). "
                    "hook=%r error=%s: %s",
                    point, hook, type(exc).__name__, exc,
                )

    async def _dispatch_one(self, hook: HookDef, point: str, template_vars: dict) -> None:
        """Dispatch a single hook by type (push C/E vs shell F)."""
        if hook.push is not None:
            resolved = render_push(hook.push, template_vars)
            if not resolved.push_when:
                return  # conditional push guard (or a render failure — fail-safe)
            # Attribution name (#1800 slice 6): the hook's operator label when
            # set, else the lifecycle point (slice-5b default). Consumed as the
            # ``[hook:<name>]`` system-role prefix (shared E + C renderer).
            payload = {"name": hook.name or point, "text": resolved.message}
            if resolved.wake:
                # E — a turn trigger (self-continuation). Routed to
                # _handle_hook_message, which renders the system-role
                # [hook:name] message + runs one turn. wake=True so
                # _drain_to_wake treats it as the trigger.
                await self._put_inbox(HOOK_INBOX_KIND, {**payload, "wake": True})
            else:
                # C — a passive ride-along: stage directly into next-turn context
                # (the 4b staging API), NOT via the inbox (a wake=false-only inbox
                # push would never drain alone — Decision A). Rides along with the
                # next turn as a system-role [hook:name] message.
                await self._stage_next_turn_context(HOOK_STAGE_KIND, payload)
        elif hook.shell is not None:
            # F — an external side-effect. Output ignored; never raises (the
            # runner logs + returns). Backend: the injected instance, else
            # run_shell_hook resolves get_default_backend(sandbox_config).
            await self._run_shell(
                hook.shell,
                template_vars,
                sandbox_backend=self._sandbox_backend,
                sandbox_config=self._sandbox_config,
            )


__all__ = ["HookDispatcher", "HOOK_INBOX_KIND", "HOOK_STAGE_KIND"]
