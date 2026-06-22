"""reyn.hooks.render — Jinja2 template rendering for hook push directives (#1800 slice B).

Entry point: ``render_push(push, context)`` — renders a ``PushBlock``'s
template fields against a runtime context dict and returns a ``ResolvedPush``
frozen dataclass that the runtime consumes directly.

Jinja2 environment
------------------
All rendering uses ``jinja2.sandbox.SandboxedEnvironment`` (never a plain
``Environment``).  The sandbox blocks attribute-traversal escapes (e.g.
``{{ ().__class__.__bases__ }}`` or ``{{ cycler.__init__.__globals__ }}``)
that a plain environment would allow.  Templates render against untrusted
operator-supplied config, so the sandbox is **non-negotiable**.

Truthiness policy (``wake`` / ``push_when``)
--------------------------------------------
When a template renders to a string, it is converted to bool by explicit
case-insensitive look-up:

    True  ← "true", "1", "yes", "on"
    False ← "false", "0", "no", "off", "" (empty string)

Any other rendered string is a **render error** (see below).  A plain
Python bool in the source config bypasses rendering and is used as-is.

Fail-safe undefined-variable policy
------------------------------------
``jinja2.StrictUndefined`` would raise on any undefined variable, but for
bool fields (``wake``, ``push_when``) this would crash the agent — the
wrong failure mode.  The chosen policy:

- ``message``: **``StrictUndefined``** — a blank message due to a typo'd
  variable name is misleading; raise loudly and let the error safety-net
  skip the push entirely (``push_when=False``).
- ``wake`` / ``push_when`` / ``session``: **``Undefined``** (silent, renders
  as empty string) — an empty string maps to ``False`` via the truthiness
  table (fail-safe: don't wake / don't push on an undefined condition).
  For ``session``, empty → ``None`` (fall back to the current session).

Render-error safety net
-----------------------
A render error (Jinja2 ``TemplateError`` or an unrecognised boolean string)
must **not** crash the agent.  On any exception in ``render_push``:

- ``push_when`` is set to ``False`` (skip the push entirely — the safest
  default; a broken condition should not trigger an unintended turn).
- The error is logged at WARNING level with the push block repr so the
  operator can diagnose it.
- A ``ResolvedPush`` with ``push_when=False`` (and the other fields at
  their safest defaults) is returned.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from jinja2 import StrictUndefined, TemplateError, Undefined
from jinja2.sandbox import SandboxedEnvironment

from reyn.hooks.schema import PushBlock

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Truthiness map
# ---------------------------------------------------------------------------

_TRUTHY: frozenset[str] = frozenset({"true", "1", "yes", "on"})
_FALSY: frozenset[str] = frozenset({"false", "0", "no", "off", ""})


def _str_to_bool(value: str, field: str) -> bool:
    """Convert a rendered string to a Python bool via the truthiness table.

    Raises ``ValueError`` for unrecognised values so the caller can wrap
    them in the render-error safety net.
    """
    lowered = value.strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ValueError(
        f"Hook push field {field!r} rendered to an unrecognised boolean "
        f"string {value!r}. Accepted (case-insensitive): "
        f"true/1/yes/on → True; false/0/no/off/'' → False."
    )


# ---------------------------------------------------------------------------
# ResolvedPush — the rendered push directive
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedPush:
    """A fully-rendered hook push directive, ready for the runtime to act on.

    Fields
    ------
    message:
        The rendered message string to inject into the session inbox.
    wake:
        ``True`` if the pushed message should trigger a new agent turn
        (use-case E — self-continuation); ``False`` if it should ride along
        with the next scheduled turn (use-case C — additive context).
    push_when:
        ``False`` → skip this push entirely (conditional guard).
        ``True``  → proceed with the push.
    session:
        Target session identifier, or ``None`` to use the current session.
    """

    message: str
    wake: bool
    push_when: bool
    session: str | None


# ---------------------------------------------------------------------------
# Jinja2 environment factories
# ---------------------------------------------------------------------------


def _make_env_strict() -> SandboxedEnvironment:
    """SandboxedEnvironment with StrictUndefined — for ``message``."""
    return SandboxedEnvironment(undefined=StrictUndefined)


def _make_env_silent() -> SandboxedEnvironment:
    """SandboxedEnvironment with silent Undefined — for bool / session fields."""
    return SandboxedEnvironment(undefined=Undefined)


# ---------------------------------------------------------------------------
# Core renderer
# ---------------------------------------------------------------------------


def render_push(push: PushBlock, context: dict) -> ResolvedPush:
    """Render a ``PushBlock`` against ``context`` and return a ``ResolvedPush``.

    Parameters
    ----------
    push:
        The raw ``PushBlock`` from the hook config (templates stored as
        strings).
    context:
        Runtime context dict — typically event + session metadata supplied
        by the hook dispatcher.

    Returns
    -------
    ResolvedPush
        The fully-resolved push directive.  On any render failure the
        returned object has ``push_when=False`` so the runtime skips the
        push rather than crashing.
    """
    try:
        return _render_push_inner(push, context)
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "Hook push render failed — push will be skipped. "
            "push=%r error=%s: %s",
            push,
            type(exc).__name__,
            exc,
        )
        # Fail-safe: return a ResolvedPush that causes the push to be skipped.
        return ResolvedPush(message="", wake=False, push_when=False, session=None)


def _render_push_inner(push: PushBlock, context: dict) -> ResolvedPush:
    """Inner renderer — may raise; callers should use ``render_push`` instead."""
    env_strict = _make_env_strict()
    env_silent = _make_env_silent()

    # ── message (StrictUndefined — a blank message due to a typo is misleading) ──
    message = env_strict.from_string(push.message).render(context)

    # ── wake ──────────────────────────────────────────────────────────────────
    if isinstance(push.wake, bool):
        wake = push.wake
    else:
        rendered_wake = env_silent.from_string(push.wake).render(context)
        wake = _str_to_bool(rendered_wake, "wake")

    # ── push_when ─────────────────────────────────────────────────────────────
    rendered_pw = env_silent.from_string(push.push_when).render(context)
    push_when = _str_to_bool(rendered_pw, "push_when")

    # ── session (render only if the template contains '{{', else static) ──────
    session: str | None
    if push.session is None:
        session = None
    elif "{{" in push.session:
        rendered_session = env_silent.from_string(push.session).render(context)
        session = rendered_session if rendered_session.strip() else None
    else:
        session = push.session if push.session.strip() else None

    return ResolvedPush(
        message=message,
        wake=wake,
        push_when=push_when,
        session=session,
    )
