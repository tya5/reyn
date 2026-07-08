"""reyn.security.template_env — the one Jinja2 environment factory.

``make_sandboxed_env`` is the **only** place in the codebase that constructs a
Jinja2 environment. It always returns a ``jinja2.sandbox.SandboxedEnvironment``
— never a plain ``jinja2.Environment`` — because templates in this codebase
may be LLM-authored or operator-supplied-but-untrusted, and an unsandboxed
Jinja2 environment allows attribute-traversal escapes (e.g.
``{{ ().__class__.__bases__ }}`` or ``{{ cycler.__init__.__globals__ }}``)
that amount to arbitrary code execution (SSTI). The sandbox is
non-negotiable; this module exists so that invariant lives in exactly one
place rather than being re-derived at every call site.

Sits beside ``reyn.security.content_fence`` / ``reyn.security.content_guard``
as the security-primitives home: those guard untrusted *input*; this guards
untrusted *template execution*.

Undefined policy
-----------------
The caller chooses how an undefined template variable behaves via the
``undefined`` parameter:

- ``"strict"`` — ``jinja2.StrictUndefined``: any undefined variable raises
  ``jinja2.UndefinedError`` at render time. Use this where a silently blank
  render would be misleading (e.g. a rendered message).
- ``"lenient"`` — ``jinja2.Undefined``: an undefined variable renders as the
  empty string rather than raising. Use this where a fail-safe empty result
  is preferable to a crash (e.g. a boolean condition field).

No other policy (autoescape, filters, extensions) is set here; callers that
need more should extend the environment this factory returns rather than
constructing their own.
"""
from __future__ import annotations

from typing import Literal

from jinja2 import StrictUndefined, Undefined
from jinja2.sandbox import SandboxedEnvironment

UndefinedPolicy = Literal["strict", "lenient"]


def make_sandboxed_env(undefined: UndefinedPolicy) -> SandboxedEnvironment:
    """Return a ``SandboxedEnvironment`` configured per ``undefined``.

    Parameters
    ----------
    undefined:
        ``"strict"`` → ``StrictUndefined`` (raise on undefined variables);
        ``"lenient"`` → ``Undefined`` (silent, renders as empty string).

    Always returns ``jinja2.sandbox.SandboxedEnvironment`` — never a plain
    ``jinja2.Environment`` — regardless of ``undefined``. See the module
    docstring for why the sandbox is non-negotiable.
    """
    if undefined == "strict":
        return SandboxedEnvironment(undefined=StrictUndefined)
    if undefined == "lenient":
        return SandboxedEnvironment(undefined=Undefined)
    raise ValueError(
        f"undefined must be 'strict' or 'lenient', got {undefined!r}."
    )
