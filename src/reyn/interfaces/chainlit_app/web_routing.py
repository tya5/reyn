"""FP-0043 Stage 4b-2: web-transport session routing (chainlit-free, unit-testable).

Maps a chainlit per-browser thread to its OWN conversation Session of an agent via
the routing-key primitive (registry.resolve_session). Kept free of any ``chainlit``
import so the mapping + run-binding logic is unit-tested without the optional web
dependency; ``app.py`` is the thin chainlit glue that supplies the thread id and
drains the resolved session's ``.outbox``.

Behaviour change note (FP-0043 S4b-2, owner-approved): web inbound moves from the
single shared/"main" session to a ``web:<thread>`` mapping — each browser thread is
its own conversation (stateful + isolated). This is intentionally NOT byte-identical
for web; REPL/TUI/CLI/a2a are unchanged. A prior "main" web conversation is not
carried into a new ``web:<thread>``; "main" stays reachable via the REPL/CLI or an
explicit-join.
"""
from __future__ import annotations

WEB_TRANSPORT = "web"
_FALLBACK_NATIVE_ID = "default"


def web_native_id(thread_id: "str | None") -> str:
    """Native-id for the routing-key from a chainlit per-websocket thread id.

    The thread id is normally always present (``cl.context.session.id``); a missing
    / blank id falls back to a stable ``"default"`` so it maps to ``web:default`` —
    kept INSIDE the web namespace, never silently merged into the REPL's "main"."""
    tid = (thread_id or "").strip()
    return tid or _FALLBACK_NATIVE_ID


def web_session_id(thread_id: "str | None") -> str:
    """The logical session-id (routing-key) for a web thread: ``web:<native>``."""
    return f"{WEB_TRANSPORT}:{web_native_id(thread_id)}"


def resolve_web_session(registry, agent_name: str, thread_id: "str | None"):
    """Resolve (get-or-spawn) the ``web:<thread>`` Session for one browser thread.

    Steps (pure registry + session ops, chainlit-free):
      1. ``resolve_session(agent, "web", native)`` — get-or-spawn by routing-key.
      2. ``ensure_session_running`` — boot the run-loop WITHOUT a forwarder, so the
         browser can drain ``session.outbox`` directly (the forwarder would race it).
      3. ``is_attached = True`` — the browser thread is live, so the session's output
         is not source-filtered (each web session is independently "attached"; the
         registry's single ``_attached`` focus slot stays for the REPL/TUI).

    Idempotent: safe to call on every inbound message (resolve = get-or-spawn,
    ensure-running = no-op if live). Returns the resolved Session."""
    native = web_native_id(thread_id)
    session = registry.resolve_session(agent_name, WEB_TRANSPORT, native)
    registry.ensure_session_running(agent_name, f"{WEB_TRANSPORT}:{native}")
    session.is_attached = True
    return session
