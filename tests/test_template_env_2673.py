"""Tier 1: shared sandboxed Jinja2 env factory (FP-0055 PR-0 / #2673).

``make_sandboxed_env`` is the single Jinja2 environment constructor for the
codebase (extracted from ``reyn.hooks.render``'s private helpers). These
tests are a contract check on the factory itself: it always returns a
``SandboxedEnvironment`` (never a plain ``Environment``), and the
``undefined`` policy switches between raising on an undefined variable
(``"strict"``) and rendering it as an empty string (``"lenient"``). Real
Jinja2 instances, no mocks.
"""
from __future__ import annotations

import jinja2
import pytest
from jinja2.sandbox import SandboxedEnvironment

from reyn.security.template_env import make_sandboxed_env


def test_strict_env_is_sandboxed():
    """Tier 1: undefined='strict' returns a SandboxedEnvironment, not a plain Environment."""
    env = make_sandboxed_env(undefined="strict")
    assert isinstance(env, SandboxedEnvironment)
    assert not (type(env) is jinja2.Environment)


def test_lenient_env_is_sandboxed():
    """Tier 1: undefined='lenient' returns a SandboxedEnvironment, not a plain Environment."""
    env = make_sandboxed_env(undefined="lenient")
    assert isinstance(env, SandboxedEnvironment)
    assert not (type(env) is jinja2.Environment)


def test_strict_undefined_raises_on_undefined_var():
    """Tier 1: undefined='strict' raises when a template references an undefined variable."""
    env = make_sandboxed_env(undefined="strict")
    with pytest.raises(jinja2.UndefinedError):
        env.from_string("{{ missing_var }}").render({})


def test_lenient_undefined_renders_empty_string():
    """Tier 1: undefined='lenient' renders an undefined variable as an empty string."""
    env = make_sandboxed_env(undefined="lenient")
    result = env.from_string("[{{ missing_var }}]").render({})
    assert result == "[]"


def test_sandbox_blocks_class_escape():
    """Tier 1: sandbox proof — attribute-traversal SSTI escape is blocked, regardless of policy."""
    env = make_sandboxed_env(undefined="lenient")
    with pytest.raises(jinja2.exceptions.SecurityError):
        env.from_string("{{ ().__class__.__bases__ }}").render({})


def test_invalid_undefined_policy_rejected():
    """Tier 1: an undefined policy outside {'strict', 'lenient'} raises ValueError."""
    with pytest.raises(ValueError):
        make_sandboxed_env(undefined="bogus")  # type: ignore[arg-type]
