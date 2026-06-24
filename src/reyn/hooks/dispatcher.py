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

import json
import logging
from typing import Any, Awaitable, Callable

from reyn.hooks.registry import HookRegistry
from reyn.hooks.render import ResolvedPush, render_push
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
        consent_bus: Any = None,
        interactive: bool = False,
    ) -> None:
        self._registry = registry
        self._put_inbox = put_inbox
        self._stage_next_turn_context = stage_next_turn_context
        self._run_shell = run_shell
        self._sandbox_config = sandbox_config
        self._sandbox_backend = sandbox_backend
        # #2095: the session RequestBus + interactivity flag, forwarded to the
        # shell-hook consent gate so a not-yet-allowlisted command's prompt
        # surfaces on the interactive surface (TUI Pending tab) instead of the
        # stdin prompt. None / non-interactive → the runner's fail-closed path.
        self._consent_bus = consent_bus
        self._interactive = interactive

    def replace_registry(self, registry: HookRegistry) -> None:
        """Swap the live hook registry (#2073 S2b config hot-reload). ``dispatch()``
        reads ``self._registry`` fresh on every lifecycle point, so a single swap
        here propagates to every holder of this dispatcher instance — no re-threading
        through the kernel/router seams. Used by the Session's hooks reapply seam to
        install ``startup ∪ re-read-runtime`` hooks at the turn boundary."""
        self._registry = registry

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
        """Dispatch a single hook by scheme: template_push (C/E) / shell_exec (F)
        / shell_push (run + parse stdout → the same C/E path as template_push)."""
        if hook.template_push is not None:
            resolved = render_push(hook.template_push, template_vars)
            await self._push_resolved(resolved, hook, point)
        elif hook.shell_exec is not None:
            # shell_exec — an external side-effect. Output IGNORED; never raises
            # (the runner logs + returns). Backend: the injected instance, else
            # run_shell_hook resolves get_default_backend(sandbox_config).
            await self._run_shell(
                hook.shell_exec,
                template_vars,
                sandbox_backend=self._sandbox_backend,
                sandbox_config=self._sandbox_config,
                consent_bus=self._consent_bus,
                interactive=self._interactive,
            )
        elif hook.shell_push is not None:
            # shell_push (#2069) — a shell command whose STDOUT is a JSON
            # push-directive. Captured (capture_stdout, vs shell_exec's ignored
            # output), parsed fail-safe into a ResolvedPush, then dispatched via
            # the SAME C/E path as template_push. The ONLY difference from
            # template_push is the SOURCE of the ResolvedPush: stdout JSON here vs
            # Jinja2 render there. A run-failure (→ stdout None) or a parse-failure
            # (→ _parse_shell_push None) skips the push (fail-safe).
            stdout = await self._run_shell(
                hook.shell_push,
                template_vars,
                sandbox_backend=self._sandbox_backend,
                sandbox_config=self._sandbox_config,
                capture_stdout=True,
                consent_bus=self._consent_bus,
                interactive=self._interactive,
            )
            resolved = _parse_shell_push(stdout)
            if resolved is not None:
                await self._push_resolved(resolved, hook, point)

    async def _push_resolved(self, resolved, hook: HookDef, point: str) -> None:
        """Dispatch a resolved push directive via C/E (#1800 slice 5b/6) — shared
        by ``template_push`` (Jinja2 render) and ``shell_push`` (stdout JSON), so
        the only difference between the two is where ``resolved`` comes from."""
        if not resolved.push_when:
            return  # conditional push guard (or a render/parse failure — fail-safe)
        # Attribution name (#1800 slice 6): the hook's operator label when set,
        # else the lifecycle point (slice-5b default) — the ``[hook:<name>]``
        # system-role prefix (shared E + C renderer).
        payload = {"name": hook.name or point, "text": resolved.message}
        if resolved.wake:
            # E — a turn trigger (self-continuation): _put_inbox wake=True →
            # _drain_to_wake treats it as the trigger → _handle_hook_message.
            await self._put_inbox(HOOK_INBOX_KIND, {**payload, "wake": True})
        else:
            # C — a passive ride-along: stage directly into next-turn context (the
            # 4b API), NOT via the inbox (a wake=false-only inbox push never drains
            # alone — Decision A).
            await self._stage_next_turn_context(HOOK_STAGE_KIND, payload)


def _parse_shell_push(stdout: str | None) -> ResolvedPush | None:
    """Parse a ``shell_push`` command's captured stdout (a JSON push-directive,
    #2069) into a ``ResolvedPush``, or ``None`` to skip the push.

    Contract: stdout is a single JSON object
    ``{"push_when": bool, "wake": bool, "message": str, "session"?: str}``.
    The first three are required; ``session`` is optional.

    **Fail-safe** (never raises): empty stdout, invalid JSON, a non-object, a
    missing or wrong-typed required field, or a non-string ``session`` all log a
    WARNING and return ``None`` so the dispatcher skips the push and the run
    proceeds — symmetric with ``render_push``'s ``push_when=False`` safety net.

    ``session`` is parsed and carried on the ``ResolvedPush`` for uniformity with
    ``template_push`` (forward-compat). Cross-session routing is **not wired** in
    this slice — the dispatcher pushes to the current session and ignores
    ``resolved.session``, so a ``session`` value is a no-op today (tracked
    follow-up), exactly as for ``template_push``.
    """
    if not stdout or not stdout.strip():
        return None
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        _log.warning(
            "shell_push stdout is not valid JSON — push skipped. error=%s: %s",
            type(exc).__name__, exc,
        )
        return None
    if not isinstance(obj, dict):
        _log.warning(
            "shell_push directive must be a JSON object, got %s — push skipped.",
            type(obj).__name__,
        )
        return None

    message = obj.get("message")
    wake = obj.get("wake")
    push_when = obj.get("push_when")
    session = obj.get("session")

    # Required-field + type checks (bool first — bool is an int subclass, so the
    # isinstance(..., bool) guard correctly rejects an integer 1/0).
    if not isinstance(message, str) or not message.strip():
        _log.warning("shell_push directive 'message' must be a non-empty string — push skipped.")
        return None
    if not isinstance(wake, bool):
        _log.warning("shell_push directive 'wake' must be a JSON bool — push skipped.")
        return None
    if not isinstance(push_when, bool):
        _log.warning("shell_push directive 'push_when' must be a JSON bool — push skipped.")
        return None
    if session is not None and not isinstance(session, str):
        _log.warning("shell_push directive 'session' must be a string or null — push skipped.")
        return None
    session = session if (session and session.strip()) else None

    return ResolvedPush(message=message, wake=wake, push_when=push_when, session=session)


__all__ = ["HookDispatcher", "HOOK_INBOX_KIND", "HOOK_STAGE_KIND"]
