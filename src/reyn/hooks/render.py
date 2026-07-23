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

import json
import logging
from dataclasses import dataclass

from jinja2 import TemplateError
from jinja2.sandbox import SandboxedEnvironment

from reyn.hooks.schema import PushBlock
from reyn.security.template_env import make_sandboxed_env

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
    env_strict = make_sandboxed_env(undefined="strict")
    env_silent = make_sandboxed_env(undefined="lenient")

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


# ---------------------------------------------------------------------------
# pipeline_launch input rendering (#2608 H3)
# ---------------------------------------------------------------------------


def render_pipeline_input(input_template: "dict | str | None", context: dict) -> "dict | None":
    """Render a hook's ``pipeline_launch.input_template`` against ``context``
    into the ``input: dict`` a launched Pipeline receives (#2608 H3).

    ``input_template`` shapes
    --------------------------
    - ``None``: no input — the launched pipeline gets ``input=None``.
    - ``dict``: every STRING leaf (recursively, through nested dicts/lists) is
      rendered as a Jinja2 template against ``context``; non-string leaves
      (int/float/bool/None) pass through unchanged. The dict's STRUCTURE
      (keys, nesting) is never templated — only leaf strings are.
    - ``str``: the whole string is rendered as ONE Jinja2 template, and the
      rendered text is parsed as JSON — the result must be a JSON object
      (mirrors ``exec_capture``'s stdout-is-a-JSON-object contract). Use this
      form to build the input from a single templated JSON blob.

    Rendering uses a ``SandboxedEnvironment`` with silent ``Undefined`` (an
    undefined variable renders as the empty string) — an event payload
    missing a referenced field yields an empty string in that slot rather
    than crashing; this mirrors the ``wake``/``push_when``/``session`` fields
    in ``render_push``, not ``message`` (which is intentionally strict).

    Raises on a genuine failure (bad Jinja2 syntax, a ``str`` template that
    does not render to valid JSON or a JSON object, or an unsupported
    ``input_template`` type). Unlike ``render_push``, this function does NOT
    swallow errors itself — a silently-empty pipeline input is a worse
    failure mode than a loud skip, so the caller (the hook dispatcher) is
    responsible for catching and skipping the launch.
    """
    if input_template is None:
        return None
    env = make_sandboxed_env(undefined="lenient")
    if isinstance(input_template, str):
        rendered = env.from_string(input_template).render(context)
        obj = json.loads(rendered)
        if not isinstance(obj, dict):
            raise ValueError(
                f"pipeline_launch.input_template rendered to a JSON "
                f"{type(obj).__name__}, expected a JSON object: {rendered!r}"
            )
        return obj
    if isinstance(input_template, dict):
        return _render_template_leaves(input_template, env, context)
    raise TypeError(
        f"pipeline_launch.input_template must be a dict, string, or null, "
        f"got {type(input_template).__name__!r}."
    )


def _render_template_leaves(value: object, env: SandboxedEnvironment, context: dict) -> object:
    """Recursively render every STRING leaf of ``value`` (through nested dicts
    and lists) as a Jinja2 template; non-string leaves pass through unchanged."""
    if isinstance(value, str):
        return env.from_string(value).render(context)
    if isinstance(value, dict):
        return {k: _render_template_leaves(v, env, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template_leaves(v, env, context) for v in value]
    return value
