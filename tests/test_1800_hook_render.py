"""Tests for #1800 slice B — Jinja2 template rendering for hook push directives.

Coverage plan
-------------
Tier 1 (contract): ``render_push`` public API shape + ``ResolvedPush`` fields.
  - Happy path: render with real context → expected ``ResolvedPush`` using
    NON-default values for every field (non-default bool passthrough, str
    template for wake, session with template, push_when evaluated).
  - Bool truthiness: all eight string tokens (true/1/yes/on, false/0/no/off)
    + bool passthrough for ``wake`` (True / False directly).
  - push_when=false template ⇒ resolved push_when is False.
  - Sandbox proof: a malicious template that attempts to escape the sandbox
    is blocked / rendered safe — proves ``SandboxedEnvironment`` is in effect.
  - Undefined-variable behaviour per the declared policy:
      * ``message`` with StrictUndefined → error caught, push_when=False fallback.
      * ``wake`` / ``push_when`` with silent Undefined → empty string → False.
  - session rendering: static passthrough, template rendering, empty → None.
  - Render-error safety net: ``render_push`` returns ``push_when=False`` and
    does not raise.
"""
from __future__ import annotations

import pytest

from reyn.hooks.render import ResolvedPush, render_push
from reyn.hooks.schema import PushBlock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _push(
    *,
    message: str = "hello {{ name }}",
    wake: bool | str = False,
    push_when: str = "true",
    session: str | None = None,
) -> PushBlock:
    """Build a PushBlock with non-default wake (False) for isolation."""
    return PushBlock(message=message, wake=wake, push_when=push_when, session=session)


# ===========================================================================
# Tier 1 — Contract: render_push public API + ResolvedPush shape
# ===========================================================================


def test_render_push_full_non_default_context() -> None:
    """Tier 1: render_push with real context → ResolvedPush with all NON-default values.

    All four fields use non-default values to catch any unwired field:
    - message: rendered from template (non-empty, non-trivial)
    - wake: False (non-default bool passthrough, not the default True)
    - push_when: True (rendered from a conditional template evaluating to true)
    - session: a static non-None session id
    """
    push = _push(
        message="event={{ event.name }} skill={{ skill }}",
        wake=False,
        push_when="{{ ctx.should_push }}",
        session="ses-abc-123",
    )
    ctx = {"event": {"name": "skill_end"}, "skill": "my_skill", "ctx": {"should_push": "yes"}}
    result = render_push(push, ctx)

    assert isinstance(result, ResolvedPush)
    assert result.message == "event=skill_end skill=my_skill"
    assert result.wake is False
    assert result.push_when is True
    assert result.session == "ses-abc-123"


def test_render_push_wake_true_non_default() -> None:
    """Tier 1: wake=True (non-default) passes through as bool without rendering."""
    push = _push(message="msg", wake=True, push_when="true")
    result = render_push(push, {})

    assert result.wake is True
    assert result.push_when is True


# ===========================================================================
# Tier 1 — Truthiness: string tokens + bool passthrough
# ===========================================================================


@pytest.mark.parametrize("token", ["true", "True", "TRUE", "1", "yes", "YES", "on", "ON"])
def test_truthy_strings_resolve_to_true(token: str) -> None:
    """Tier 1: all documented truthy string tokens render wake to True."""
    push = _push(message="m", wake=token, push_when="true")
    result = render_push(push, {})
    assert result.wake is True


@pytest.mark.parametrize("token", ["false", "False", "FALSE", "0", "no", "NO", "off", "OFF", ""])
def test_falsy_strings_resolve_to_false(token: str) -> None:
    """Tier 1: all documented falsy string tokens render wake to False."""
    push = _push(message="m", wake=token, push_when="true")
    result = render_push(push, {})
    assert result.wake is False


def test_bool_true_passthrough() -> None:
    """Tier 1: wake=True (Python bool) bypasses string rendering."""
    push = _push(message="m", wake=True, push_when="true")
    result = render_push(push, {})
    assert result.wake is True


def test_bool_false_passthrough() -> None:
    """Tier 1: wake=False (Python bool) bypasses string rendering."""
    push = _push(message="m", wake=False, push_when="true")
    result = render_push(push, {})
    assert result.wake is False


# ===========================================================================
# Tier 1 — push_when=false stops the push
# ===========================================================================


def test_push_when_false_template() -> None:
    """Tier 1: push_when template rendering to 'false' ⇒ push_when=False."""
    push = _push(message="m", wake=False, push_when="{{ skip }}")
    result = render_push(push, {"skip": "false"})
    assert result.push_when is False


def test_push_when_static_false() -> None:
    """Tier 1: push_when='false' (static string) ⇒ push_when=False."""
    push = _push(message="m", wake=False, push_when="false")
    result = render_push(push, {})
    assert result.push_when is False


def test_push_when_conditional_evaluates_to_true() -> None:
    """Tier 1: push_when Jinja2 conditional ⇒ True when condition holds."""
    push = _push(
        message="m",
        wake=False,
        push_when="{% if count > 0 %}true{% else %}false{% endif %}",
    )
    result = render_push(push, {"count": 3})
    assert result.push_when is True


def test_push_when_conditional_evaluates_to_false() -> None:
    """Tier 1: push_when Jinja2 conditional ⇒ False when condition fails."""
    push = _push(
        message="m",
        wake=False,
        push_when="{% if count > 0 %}true{% else %}false{% endif %}",
    )
    result = render_push(push, {"count": 0})
    assert result.push_when is False


# ===========================================================================
# Tier 1 — Sandbox proof: SandboxedEnvironment is in effect
# ===========================================================================


def test_sandbox_blocks_class_escape() -> None:
    """Tier 1: sandbox proof — {{ ().__class__.__bases__ }} is blocked/safe.

    SandboxedEnvironment raises SecurityError (or renders safely without
    leaking real Python internals).  The critical property: ``render_push``
    must not raise — it must activate the safety net and return
    push_when=False.  The malicious template must NOT produce the literal
    string representation of Python's type hierarchy.
    """
    push = _push(
        message="{{ ().__class__.__bases__ }}",
        wake=False,
        push_when="true",
    )
    # render_push must not raise regardless of sandbox outcome
    result = render_push(push, {})
    # Safety net fires: the push is skipped
    assert result.push_when is False
    assert result.message == ""


def test_sandbox_blocks_globals_escape() -> None:
    """Tier 1: sandbox proof — {{ cycler.__init__.__globals__ }} is blocked/safe.

    cycler is a Jinja2 built-in; __globals__ traversal is the classic
    sandbox-escape vector.  SandboxedEnvironment blocks attribute access to
    dunder names.  render_push must not raise and must return push_when=False.
    """
    push = _push(
        message="{{ cycler.__init__.__globals__ }}",
        wake=False,
        push_when="true",
    )
    result = render_push(push, {})
    assert result.push_when is False
    assert result.message == ""


def test_sandbox_allows_normal_attribute_access() -> None:
    """Tier 1: sandbox allows safe attribute access on context objects."""

    class _Evt:
        name = "turn_end"

    push = _push(message="{{ event.name }}", wake=False, push_when="true")
    result = render_push(push, {"event": _Evt()})
    assert result.message == "turn_end"
    assert result.push_when is True


# ===========================================================================
# Tier 1 — Undefined-variable policy
# ===========================================================================


def test_message_undefined_var_triggers_safety_net() -> None:
    """Tier 1: message with StrictUndefined — undefined var triggers error safety net.

    Policy: message uses StrictUndefined; a typo'd variable should not silently
    produce a blank message.  The render error is caught; push_when=False is
    returned so the push is skipped.
    """
    push = _push(message="{{ no_such_var }}", wake=False, push_when="true")
    result = render_push(push, {})
    # Safety net: push is skipped, not raised
    assert result.push_when is False
    assert result.message == ""


def test_wake_undefined_var_resolves_to_false() -> None:
    """Tier 1: wake with undefined var → silent empty string → False (fail-safe).

    Policy: wake uses silent Undefined; undefined → empty string → False (don't
    wake on an undefined condition — the safer direction).
    """
    push = _push(message="m", wake="{{ no_such_wake }}", push_when="true")
    result = render_push(push, {})
    assert result.wake is False


def test_push_when_undefined_var_resolves_to_false() -> None:
    """Tier 1: push_when with undefined var → silent empty string → False (fail-safe).

    Policy: push_when uses silent Undefined; undefined → empty string → False
    (don't push on an undefined condition — the safer direction).
    """
    push = _push(message="m", wake=False, push_when="{{ no_such_condition }}")
    result = render_push(push, {})
    assert result.push_when is False


# ===========================================================================
# Tier 1 — session field rendering
# ===========================================================================


def test_session_static_passthrough() -> None:
    """Tier 1: session without '{{' is used as-is (no rendering)."""
    push = _push(message="m", wake=False, push_when="true", session="static-ses-007")
    result = render_push(push, {})
    assert result.session == "static-ses-007"


def test_session_template_rendered() -> None:
    """Tier 1: session containing '{{' is rendered against context."""
    push = _push(message="m", wake=False, push_when="true", session="ses-{{ sid }}")
    result = render_push(push, {"sid": "xyz"})
    assert result.session == "ses-xyz"


def test_session_none_resolves_to_none() -> None:
    """Tier 1: session=None ⇒ resolved session=None (use current session)."""
    push = _push(message="m", wake=False, push_when="true", session=None)
    result = render_push(push, {})
    assert result.session is None


def test_session_template_undefined_var_resolves_to_none() -> None:
    """Tier 1: session template with undefined var → empty string → None (fail-safe)."""
    push = _push(message="m", wake=False, push_when="true", session="{{ no_such_sid }}")
    result = render_push(push, {})
    assert result.session is None


# ===========================================================================
# Tier 1 — Render-error safety net: render_push never raises
# ===========================================================================


def test_render_error_never_raises() -> None:
    """Tier 1: render_push does not propagate exceptions; returns push_when=False."""
    # Unrecognised boolean string → _str_to_bool raises ValueError
    push = _push(message="m", wake="not-a-bool-value", push_when="true")
    # Must not raise
    result = render_push(push, {})
    assert result.push_when is False


def test_render_error_returns_safe_defaults() -> None:
    """Tier 1: render failure returns the documented safe-default ResolvedPush fields."""
    push = _push(message="{{ undefined_strict }}", wake=False, push_when="true")
    result = render_push(push, {})
    assert result.push_when is False
    assert result.wake is False
    assert result.message == ""
    assert result.session is None
